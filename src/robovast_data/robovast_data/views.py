# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""The views a query can name beside the tables: ``run_view``, ``run_validity_view``,
``pose_track_view``.

Each exists because the raw form is a trap every consumer would otherwise walk into on its own.

* ``run_view`` is one row per run from the campaign's record, with its unit's parameters, batch
  and host, plus one run-less row per unit that produced no run -- a join alone drops those, and
  with them the only record that the cell was part of the design.
* ``run_validity_view`` answers *was this run a clean observation of the system under test, or
  partly a measurement of its CPU quota?* ``nr_throttled``/``nr_periods`` are monotonic
  counters, so a ``SUM`` means nothing and a bare ``MAX`` includes what happened before the
  trial window; the ratio carries the meaning; and the threshold separating binding from noise
  is calibrated (:data:`THROTTLE_WARN_RATIO`). It flags and never filters.
* ``pose_track_view`` is one row per recorded track -- one entity (``frame``) of one run, as one
  pose-contract table recorded it -- summarised over every pose, on the table's measurement
  clock (:func:`pose_clock`). A speed differenced on an arrival clock measures the transport,
  and a length summed across two campaigns joins their tracks with a jump across the map; the
  window partitions by the whole key so neither can happen.

Every view here is SQL over the relations a query connection defines
(:mod:`robovast_data.engine`); which tables each reads is declared beside it, so a query naming
a view builds what the view needs.
"""

from __future__ import annotations

from typing import Iterable, Optional

from robovast_decode.runs import RUNLESS_UNIT_STATUSES

#: Fraction of a container's CPU enforcement periods that may be throttled before it is worth
#: reporting. Not zero: a handful of throttled periods during bring-up is normal, and saying so
#: every time would train a reader to ignore the finding.
#:
#: **Calibrated, not guessed** -- an earlier 1% was chosen by intuition and would have stayed
#: silent on a configuration that lost 6 runs of 50. A CFS period is 100 ms and a nav2 control
#: loop runs at 20 Hz, so ONE throttled period is two missed deadlines: the scale that matters
#: is far below a percent. Measured across a five-point sweep of the same campaign, varying
#: only the SUT's limit:
#:
#: ===============  ======  ========  =======
#: throttled         misses  failures  verdict
#: ===============  ======  ========  =======
#: 0.018%                1         0  fine
#: 0.385%                0         1  fine
#: 0.580%                5         2  marginal
#: 0.629%                2         0  marginal
#: 0.790%               58         6  broken
#: ===============  ======  ========  =======
#:
#: Note it is **not monotone**: throttling varies 1.4x across that range while the stack's own
#: miss count varies 12x, and 0.580% did more damage than 0.629%. This counter is a blunt
#: screen, not a predictor -- which is exactly why the finding it raises says "inconclusive,
#: go and look at the stack's own health". 0.5% sits below the cliff and above the two
#: configurations that were demonstrably fine.
#:
#: Calibrated for a 20 Hz control loop. A stack with a slower loop tolerates proportionally
#: more, so this is a default rather than a law.
THROTTLE_WARN_RATIO = 0.005

#: Fraction of a trial window in which EVERY task in a container was runnable and none was
#: running -- PSI ``cpu.pressure`` ``full`` -- before it is worth reporting as contention.
#:
#: **Not calibrated, unlike :data:`THROTTLE_WARN_RATIO`, and the difference is deliberate.**
#: That one comes from a five-point sweep in which the stack's own miss count was counted at
#: each level; nothing equivalent has been run for this counter, because it did not exist to
#: measure. What is written here is a floor derived from the control loop rather than from
#: observed damage: a 20 Hz loop has a 50 ms budget, so 1% of a 150 s run is 1.5 s of total
#: blackout, which is 30 missed deadlines if it arrives in one burst and none if it is spread
#: a microsecond at a time. That range is exactly why this is a SCREEN and its finding says
#: "go and look at the stack's own health" rather than asserting harm.
#:
#: To calibrate it the way the throttle threshold was: run one configuration at a fixed
#: allocation against varying co-tenancy, and count control-loop misses per stall level. Until
#: that exists, treat a crossing as a question rather than an answer, and treat the number as
#: provisional -- it is placed to be crossed rarely on a healthy node, not to mark a cliff
#: anybody has seen.
STALL_WARN_RATIO = 0.01


#: The measurement tables each view reads, beside the campaign record.
VIEW_TABLES = {"run_view": (), "run_validity_view": ("system_usage",), "pose_track_view": ()}

#: Every view, in the order a catalog lists them.
VIEWS = ("run_view", "config_view", "container_failure_view", "run_validity_view",
         "pose_track_view")


def run_view_sql(have: set, unit_columns: set) -> Optional[str]:
    """``run_view`` over the ``campaign`` schema tables in *have*; ``None`` without runs.

    A missing ``job`` or ``batch`` table, or a store without ``unit.channels_json``, gives NULL
    columns rather than a different column set, so one query reads every campaign.
    """
    if not {"run", "unit"} <= have:
        return None
    host = ("j.job_dir, j.sysinfo_json" if "job" in have
            else "NULL AS job_dir, NULL AS sysinfo_json")
    join = ("LEFT JOIN campaign.job j ON r.job_id = j.id AND j.campaign_id = r.campaign_id"
            if "job" in have else "")
    batch = "b.idx AS batch" if "batch" in have else "NULL AS batch"
    bjoin = ("LEFT JOIN campaign.batch b ON u.batch_id = b.id AND b.campaign_id = u.campaign_id"
             if "batch" in have else "")
    channels = "u.channels_json" if "channels_json" in unit_columns else "NULL AS channels_json"
    runless = ", ".join(f"'{status}'" for status in RUNLESS_UNIT_STATUSES)
    return f"""
        SELECT r.campaign_id, u.config_name, r.run_id, r.status, r.passed, r.duration_s,
               r.errors, r.failures, r.tests, r.start_time, r.failure_message,
               u.params_json, {channels}, u.objective, u.paramset_id, {batch}, {host}
        FROM campaign.run r
        JOIN campaign.unit u ON r.unit_id = u.id AND u.campaign_id = r.campaign_id
        {bjoin}
        {join}
        UNION ALL
        SELECT u.campaign_id, COALESCE(NULLIF(u.config_name, ''), u.paramset_id) AS config_name,
               NULL AS run_id, u.status, 0 AS passed, NULL AS duration_s, NULL AS errors,
               NULL AS failures, NULL AS tests, NULL AS start_time, NULL AS failure_message,
               u.params_json, {channels}, u.objective, u.paramset_id, {batch},
               NULL AS job_dir, NULL AS sysinfo_json
        FROM campaign.unit u
        {bjoin}
        WHERE u.status IN ({runless})
    """


def run_validity_sql(columns: set) -> str:
    """``run_validity_view`` over ``system_usage`` holding *columns*.

    A sampler without a PSI probe gives NULL stall columns: "not measured", never "no
    contention".
    """
    stall_full = ("MAX(cpu_stall_full_usec) - MIN(cpu_stall_full_usec)"
                  if "cpu_stall_full_usec" in columns else "CAST(NULL AS BIGINT)")
    stall_some = ("MAX(cpu_stall_some_usec) - MIN(cpu_stall_some_usec)"
                  if "cpu_stall_some_usec" in columns else "CAST(NULL AS BIGINT)")
    throttled = "CAST(throttled AS DOUBLE) / periods"
    return f"""
        WITH per_run AS (
            SELECT campaign_id, config_name, run_id, container,
                   MAX(nr_periods) - MIN(nr_periods) AS periods,
                   MAX(nr_throttled) - MIN(nr_throttled) AS throttled,
                   MAX(throttled_usec) - MIN(throttled_usec) AS throttled_usec,
                   {stall_some} AS stalled_some_usec,
                   {stall_full} AS stalled_full_usec,
                   -- The window's own wall span: a stall total means nothing without it.
                   (MAX(CAST(wall_ts AS DOUBLE)) - MIN(CAST(wall_ts AS DOUBLE))) * 1000000.0
                       AS span_usec
            FROM system_usage
            WHERE in_window = 1 AND nr_periods IS NOT NULL
            GROUP BY campaign_id, config_name, run_id, container)
        SELECT campaign_id, config_name, run_id, container, periods, throttled,
               throttled_usec, stalled_some_usec, stalled_full_usec,
               CASE WHEN periods > 0 THEN {throttled} END AS throttle_ratio,
               CASE WHEN span_usec > 0 AND stalled_full_usec IS NOT NULL
                    THEN stalled_full_usec / span_usec END AS stall_ratio,
               CASE WHEN periods > 0 AND {throttled} >= {THROTTLE_WARN_RATIO}
                    THEN 1 ELSE 0 END AS quota_bound,
               -- Contention is what is left once the container's own ceiling is ruled out;
               -- NULL, not 0, where the probe is absent: silence is not a pass.
               CASE WHEN stalled_full_usec IS NULL OR span_usec <= 0 THEN NULL
                    WHEN stalled_full_usec / span_usec >= {STALL_WARN_RATIO}
                         AND NOT (periods > 0 AND {throttled} >= {THROTTLE_WARN_RATIO})
                    THEN 1 ELSE 0 END AS contended
        FROM per_run
    """


def pose_clock(columns: Iterable[str]) -> Optional[str]:
    """The measurement-time column of a pose-contract table holding *columns*; ``None`` if
    it holds no pose.

    ``stamp`` for a table converted from a transport, whose ``timestamp`` is arrival; else
    ``timestamp``, which a table the simulator wrote takes inside the simulator.
    """
    columns = set(columns)
    if "position.x" not in columns:
        return None
    return "stamp" if "stamp" in columns else "timestamp"


#: What a table needs beside a pose clock to be summarised as tracks.
POSE_COLUMNS = frozenset({"campaign_id", "config_name", "run_id", "frame", "position.x",
                          "position.y"})

_TRACK_KEY = "source, campaign_id, config_name, run_id, frame"


def is_pose_table(columns: Iterable[str]) -> bool:
    columns = set(columns)
    return pose_clock(columns) is not None and POSE_COLUMNS <= columns


def pose_track_sql(tables: dict) -> Optional[str]:
    """``pose_track_view`` over *tables* (``{name: columns}``) that follow the pose contract.

    A sample with no measurement time (a latched ``/tf_static`` transform) is not a point on a
    track. ``position.z`` is part of the length where a table has it. A reposition is travel:
    ``max_step_m`` makes it visible rather than a threshold hiding it.
    """
    branches = []
    for table in sorted(tables):
        columns = set(tables[table])
        if not is_pose_table(columns):
            continue
        clock = pose_clock(columns)
        z = 'CAST("position.z" AS DOUBLE)' if "position.z" in columns else "0.0"
        yaw = ('CAST("orientation.yaw" AS DOUBLE)' if "orientation.yaw" in columns
               else "CAST(NULL AS DOUBLE)")
        branches.append(f"""
            SELECT '{table}' AS source, campaign_id, config_name,
                   CAST(run_id AS BIGINT) AS run_id, frame,
                   CAST("{clock}" AS DOUBLE) AS t,
                   CAST("position.x" AS DOUBLE) AS x, CAST("position.y" AS DOUBLE) AS y,
                   {z} AS z, {yaw} AS yaw
            FROM "{table}"
            WHERE "{clock}" IS NOT NULL""")
    if not branches:
        return None
    return f"""
        WITH p AS ({" UNION ALL ".join(branches)}),
             d AS (SELECT {_TRACK_KEY}, t, x, y, z,
                          SQRT(POWER(x - LAG(x) OVER w, 2) + POWER(y - LAG(y) OVER w, 2)
                               + POWER(z - LAG(z) OVER w, 2)) AS step_m,
                          t - LAG(t) OVER w AS dt,
                          FIRST_VALUE(x) OVER w AS start_x, FIRST_VALUE(y) OVER w AS start_y,
                          FIRST_VALUE(z) OVER w AS start_z,
                          FIRST_VALUE(yaw) OVER w AS start_yaw,
                          LAST_VALUE(x) OVER wf AS end_x, LAST_VALUE(y) OVER wf AS end_y,
                          LAST_VALUE(z) OVER wf AS end_z, LAST_VALUE(yaw) OVER wf AS end_yaw
                   FROM p
                   WINDOW w AS (PARTITION BY {_TRACK_KEY} ORDER BY t),
                          wf AS (PARTITION BY {_TRACK_KEY} ORDER BY t
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING))
        SELECT {_TRACK_KEY},
               COUNT(*) AS points,
               COALESCE(SUM(step_m), 0) AS length_m,
               MAX(t) - MIN(t) AS duration_s,
               CASE WHEN MAX(t) > MIN(t)
                    THEN COALESCE(SUM(step_m), 0) / (MAX(t) - MIN(t)) END AS avg_speed_m_s,
               MAX(CASE WHEN dt > 0 THEN step_m / dt END) AS max_speed_m_s,
               MAX(step_m) AS max_step_m,
               MIN(start_x) AS start_x, MIN(start_y) AS start_y, MIN(start_z) AS start_z,
               MIN(start_yaw) AS start_yaw,
               MIN(end_x) AS end_x, MIN(end_y) AS end_y, MIN(end_z) AS end_z,
               MIN(end_yaw) AS end_yaw,
               MIN(x) AS min_x, MAX(x) AS max_x, MIN(y) AS min_y, MAX(y) AS max_y,
               MIN(z) AS min_z, MAX(z) AS max_z
        FROM d
        GROUP BY {_TRACK_KEY}
    """


__all__ = ["POSE_COLUMNS", "STALL_WARN_RATIO", "THROTTLE_WARN_RATIO", "VIEWS", "VIEW_TABLES",
           "is_pose_table", "pose_clock", "pose_track_sql", "run_validity_sql", "run_view_sql"]
