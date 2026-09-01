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

"""Read-only SQL over a campaign's results — a **directory-based** helper.

This is the single implementation of "describe / query a campaign's results",
parameterized by the campaign **directory** so it serves both callers:

* the MCP ``run_data`` plugin, which resolves a ``campaign_id`` → dir via
  ``results_resolver`` (or delegates to a configured service); and
* the ``robovast-service`` (``describe_campaign_data`` / ``query_campaign_data_sql``
  on :class:`~robovast.service.interface.RobovastInterface`), which resolves the
  dir per transport — local disk, or an object-store fetch on the cluster.

The rows themselves live in the central index now (:mod:`.index_query`), which is why the
directory is only a *name* here: it identifies the campaign, and that name is what the
session is scoped to. Scoping is **not** a predicate the caller writes -- the index
enforces it (:mod:`.index_scope`), because an index holding every campaign turns a
forgotten ``WHERE campaign_id`` into a silently larger answer rather than an error.
Spanning campaigns is possible and explicit: pass ``campaigns``. Read-only is enforced by
the index session rather than by a ``sqlite3`` authorizer. The campaign record is still written by
the per-campaign ``campaign.db`` (unchanged, still SQLite) and is mirrored into the index
under a schema literally named ``campaign``, so ``FROM campaign.unit`` -- the spelling
every existing query and every doc uses -- resolves as it always did.
"""

import json
import logging
import math
import re
import sqlite3
import statistics
from pathlib import Path

logger = logging.getLogger(__name__)

#: A single cell wider than this is masked/truncated so a stray ``SELECT
#: strategy_state`` (a pickled-optimizer BLOB) or a giant ``config_json`` cannot
#: dump megabytes of bytes into an LLM's context. Rows are already capped by
#: ``max_rows``; this bounds width, which rows alone do not.
_MAX_CELL_BYTES = 2048

#: Ceiling on the whole JSON reply, not one cell of it. ``max_rows`` and
#: :data:`_MAX_CELL_BYTES` bound the two axes separately and neither bounds their product:
#: 500 rows of a real campaign's ``poses`` -- the *default*, well inside every documented
#: cap -- serializes to ~270 KB, about 67,000 tokens, and the 5000-row clamp to roughly ten
#: times that. A reply nothing can read is not a reply, and an agent that spends its whole
#: context on one ``SELECT *`` cannot then do anything with the answer. Measured against
#: campaign basic-nav-gazebo-2026-08-16-20153470.
#:
#: 64 KB is ~16,000 tokens: bigger than any answer worth reading inline, and still larger
#: than the entire MCP tool surface's own budget. A caller who wants the data rather than
#: the answer has :func:`stream_query_csv`, which has no row cap at all.
#:
#: This is the *default*, not the only budget: it is a token budget, and it belongs to
#: callers who spend tokens. :func:`query_data_db` takes ``max_bytes`` so a caller that
#: renders the rows instead of reading them — the web UI's panels and data browser — is
#: bounded by what a browser can hold instead. That caller picks its own number (see
#: ``UI_RESULT_BYTES`` in ``frontend/ui/src/lib/robovastClient.ts``); this one stays as the
#: default so forgetting the parameter fails safe for an agent rather than for a chart.
_MAX_RESULT_BYTES = 64 * 1024


class DataQueryError(ValueError):
    """No queryable data, or a rejected/invalid query (maps to HTTP 400)."""


def _regexp(pattern: str, value) -> bool:
    if value is None:
        return False
    try:
        return re.search(pattern, str(value)) is not None
    except re.error:
        return False


# -- statistical aggregates --------------------------------------------------
# SQLite ships only AVG/SUM/MIN/MAX/COUNT, but campaign analysis is about
# variance across runs ("is this config flaky?", "p95 landing error"). Register
# the missing ones so the LLM can express those in one query rather than
# hand-rolling (and mis-rolling) medians via window functions.


def _floats(values):
    out = []
    for v in values:
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


class _Stddev:
    """Sample standard deviation (``NULL`` for fewer than two numeric values)."""

    def __init__(self):
        self._vals = []

    def step(self, value):
        self._vals.append(value)

    def finalize(self):
        xs = _floats(self._vals)
        return statistics.stdev(xs) if len(xs) >= 2 else None


class _Variance:
    """Sample variance (``NULL`` for fewer than two numeric values)."""

    def __init__(self):
        self._vals = []

    def step(self, value):
        self._vals.append(value)

    def finalize(self):
        xs = _floats(self._vals)
        return statistics.variance(xs) if len(xs) >= 2 else None


class _Median:
    """Median of the numeric values (``NULL`` when there are none)."""

    def __init__(self):
        self._vals = []

    def step(self, value):
        self._vals.append(value)

    def finalize(self):
        xs = _floats(self._vals)
        return statistics.median(xs) if xs else None


class _Percentile:
    """``PERCENTILE(col, p)`` — the ``p``-th percentile (0..100), linear interp."""

    def __init__(self):
        self._vals = []
        self._p = None

    def step(self, value, p):
        self._vals.append(value)
        self._p = p  # same for every row; last one wins

    def finalize(self):
        xs = sorted(_floats(self._vals))
        if not xs or self._p is None:
            return None
        try:
            p = max(0.0, min(100.0, float(self._p)))
        except (TypeError, ValueError):
            return None
        k = (len(xs) - 1) * (p / 100.0)
        lo, hi = math.floor(k), math.ceil(k)
        if lo == hi:
            return xs[int(k)]
        return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def _sqrt(value):
    """``SQRT(x)`` — ``None`` for a non-numeric or negative input, never an error.

    Registered rather than relied upon: SQLite's own ``sqrt`` needs
    ``SQLITE_ENABLE_MATH_FUNCTIONS`` at compile time, so whether a query works would
    otherwise depend on how the *answering process* was built — the MCP host and the
    service can be different machines. A query that computes a distance must not
    succeed here and fail there.

    Returning ``None`` rather than raising follows the aggregates above: one bad row in
    a large scan should leave a NULL in that row, not abort the whole result.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return math.sqrt(f) if f >= 0 else None


def _register_aggregates(conn: sqlite3.Connection) -> None:
    conn.create_aggregate("STDDEV", 1, _Stddev)
    conn.create_aggregate("VARIANCE", 1, _Variance)
    conn.create_aggregate("MEDIAN", 1, _Median)
    conn.create_aggregate("PERCENTILE", 2, _Percentile)
    conn.create_function("SQRT", 1, _sqrt)


def open_data_db(campaign_dir, campaign_id: str | None = None):
    """Open the index **read-only** — the public seam for package-provided endpoints.

    Returns a live connection, as before, so a plugin can read a table untruncated. The
    caller must ``close()`` it (or use :meth:`RunDataContext.open_db`, which does).

    **This is the one contract the move to a central index could not preserve, and it is
    better to say so than to fake it.** What comes back is a Postgres connection, not a
    ``sqlite3.Connection``, and the two differ in ways a shim would only paper over
    briefly: parameters are ``%s`` rather than ``?``, there is no ``sqlite_master`` to
    probe for a table, and ``CAST(x AS REAL)`` means a 4-byte float here (see
    :mod:`robovast.results_processing.index_dialect`). An adapter translating those would
    be a second dialect nobody documented, failing in new ways at the edges.

    So a plugin carrying SQLite SQL breaks **loudly**, with a syntax error naming the
    problem, rather than quietly returning something plausible. Rows come back as dicts,
    which is what ``row["timestamp"]`` already assumed.

One index holds every campaign, so spanning them is a ``WHERE`` clause rather than an
    attach -- there is nothing left for a caller to open beyond this connection.
    """
    from robovast.results_processing import \
        index_query  # pylint: disable=import-outside-toplevel

    # The scope the directory carried is real again, and enforced by the index rather
    # than by the plugin remembering it: this connection sees only one campaign's rows,
    # whatever SQL the plugin writes. *campaign_id* is for the caller that already knows
    # which campaign it serves and whose directory is not the campaign root -- the
    # endpoint-plugin seam is handed both, and the id it was routed with is the truth.
    return index_query.open_index(readonly=True, row_factory=True,
                                  campaigns=[campaign_id or campaign_id_of(campaign_dir)])


# What an LLM needs to write a correct query against a table it cannot see: what one row
# is, which column to filter on, and the mistakes that return wrong rows instead of an
# error. Deliberately no history and no design rationale — a caller cannot act on either,
# and every word here is spent on every request. Metric tables (one per CSV stem) are
# self-describing by their columns and are not listed.
#: For a pose table fed by a TRANSPORT, where arrival and measurement are different moments.
#: Choosing the wrong one is the commonest mistake against these tables -- it does not error, it
#: just answers a different question -- so it is spelled out where an agent reads the schema rather
#: than left to a column note it may skip.
#:
#: Not shared with a simulator-written table: there is only one clock there, and telling a reader
#: to use a `stamp` column that does not exist would be worse than saying nothing.
_POSE_CLOCKS_TRANSPORT = (
    "TWO CLOCKS. `timestamp` is ARRIVAL time and the join key shared with costmaps, behaviors "
    "and run_log -- join and scrub on it, and never difference it: it is quantized to the "
    "simulator's /clock grid and jittered by delivery, so a rate derived from it measures the "
    "transport (a constant 0.24 m/s has read as an alternating 0.21/0.43 this way). `stamp` is "
    "MEASUREMENT time -- when the pose was true -- and is the only correct base for a derivative; "
    "ORDER BY it too, since `timestamp` has ties within one arrival tick. Speed for one run: "
    "SELECT stamp, SQRT(POWER(x-px,2)+POWER(y-py,2))/(stamp-ps) AS speed FROM (SELECT stamp, "
    "\"position.x\" x, \"position.y\" y, LAG(stamp) OVER w ps, LAG(\"position.x\") OVER w px, "
    "LAG(\"position.y\") OVER w py FROM <table> WHERE config_name=? AND run_id=? AND frame=? "
    "WINDOW w AS (ORDER BY stamp)) WHERE ps IS NOT NULL AND stamp > ps. "
)

#: For a pose table the SIMULATOR wrote itself. One clock, and it is the true one -- so the warning
#: above does not apply here and would be actively wrong: there is no `stamp` column to point at,
#: and this `timestamp` is exactly the quantity the other table's `stamp` is.
_POSE_CLOCKS_NATIVE = (
    "ONE CLOCK, and it is the honest one: `timestamp` is exact simulated seconds, taken inside the "
    "simulator when the pose was true, so unlike the `poses` table there is no arrival/measurement "
    "split and no `stamp` column -- difference this one freely. Better still, do not difference at "
    "all: twist.linear.* / twist.angular.* are the TRUE world-frame velocities read straight from "
    "the physics solver, so a speed is SQRT(POWER(\"twist.linear.x\",2)+POWER(\"twist.linear.y\",2)) "
    "with no window function and no interval to get wrong. "
)

#: Also shared: what the orientation columns are, and which one is a projection.
_POSE_ORIENTATION = (
    "Orientation is a QUATERNION (orientation.x/y/z/w) -- that is what the producer emitted. "
    "`orientation.yaw` is derived from it at ingest and is a PLANAR projection: fine for a robot "
    "on a floor, wrong for anything that pitches or rolls. "
    "`frame` is the entity's name in the producer's own vocabulary (a TF child frame, a MuJoCo "
    "body). Every row is in the run's single world frame."
)

_TABLE_DESCRIPTIONS = {
    ("main", "run_health"): (
        "HOW WELL did each run go, graded by the stack under test rather than by RoboVAST: "
        "campaign_id, config_name, run_id, check_name, level (ok|warn|error), value, unit, detail, source. "
        "The scenario's pass/fail says WHETHER; this says HOW WELL, which is what a resource "
        "floor or a reproduction-fidelity question actually needs. "
        "ABSENCE IS NOT A PASS. A run with no row for a check was not checked -- no plugin "
        "installed, the check did not apply to this stack, or its input tables were not "
        "produced. Never read a missing row as healthy. An EMPTY table means checks ran and "
        "had nothing to say; a MISSING table means the campaign predates them. "
        "level is the only word RoboVAST interprets; check_name and detail belong to the "
        "stack that wrote them. value/unit are the measure -- re-threshold them yourself "
        "rather than trusting level if you disagree with the plugin's cutoff. "
        "It NEVER decides pass/fail: run_view.status is the verdict, this grades it. A run "
        "can be level='error' here and passed=1 there, and that is not a contradiction. "
        "Worst level per run: SELECT config_name, run_id, MAX(CASE level WHEN 'error' THEN 2 "
        "WHEN 'warn' THEN 1 ELSE 0 END) FROM run_health WHERE campaign_id = <id> "
        "GROUP BY 1,2. "
        "Pair with run_validity_view to tell a resource artifact from a real fault: "
        "quota_bound=1 AND health degraded means the CPU limit is a live explanation; "
        "quota_bound=1 AND health clean means the clipping cost nothing. "
        "ONE INDEX HOLDS EVERY CAMPAIGN: always scope with WHERE campaign_id = <id> unless "
        "you mean to compare campaigns. An unscoped aggregate here reads as this campaign's "
        "grades and is not. "
        "Join on (config_name, run_id)."),
    ("temp", "run_validity_view"): (
        "WAS THIS RUN A CLEAN OBSERVATION? One row per (run, container) saying whether the "
        "kernel capped it at its OWN CPU limit, and whether it was crowded out by other "
        "work: config_name, run_id, container, periods, throttled, throttled_usec, "
        "stalled_some_usec, stalled_full_usec, throttle_ratio, stall_ratio, quota_bound, "
        "contended. Query unqualified: FROM "
        "run_validity_view. Needs postprocessing (it reads system_usage). "
        "quota_bound=1 means the container exhausted the quota its limits.cpu buys, inside a "
        "~100ms enforcement period. It does NOT mean other campaigns crowded it out: a busy "
        "neighbour causes scheduling latency, not throttling, and the two point opposite "
        "ways -- a container that cannot get CPU never reaches its quota, so it throttles "
        "LESS while running worse. The remedy is a bigger limit, not a quieter cluster. "
        "Read this INSTEAD of computing deltas over system_usage yourself -- the counters "
        "there are monotonic, so SUM is meaningless and a bare MAX includes whatever "
        "happened before the trial window; this view already takes the in-window delta. "
        "The container that decides validity is 'sut': it is the system under test, so a "
        "run where it was capped cannot separate 'the stack failed' from 'the stack was "
        "cut off mid-plan'. The simulator and scenario are expected to burst and be "
        "clipped, and whether that cost anything is answered by runs.clock_map_* instead. "
        "Clean functional runs: SELECT config_name, run_id FROM run_validity_view WHERE "
        "container='sut' AND quota_bound=0. "
        "How bad, per config: SELECT config_name, MAX(throttle_ratio) FROM "
        "run_validity_view WHERE container='sut' GROUP BY 1. "
        "quota_bound is a SCREEN, not a verdict: it marks runs where a resource explanation "
        "is AVAILABLE for a failure, not runs that failed. Never drop a run because of it -- "
        "report it alongside. Pair it with the stack's own health signals (control-loop "
        "warnings in run_log, nav2_behaviors) to decide whether the clipping cost anything. "
        "UNDER execution.sizing: calibrated IT SATURATES, and that is expected rather than a "
        "finding: the system under test is sized AT its own measured maximum with request == "
        "limit, so it sits against that ceiling and is quota_bound in essentially every run "
        "(measured: 150 of 150, against 2 of 45 for a declared figure 2-3x larger). The "
        "column is still true there but no longer discriminates, so read the MAGNITUDE of "
        "throttle_ratio and the stack's health instead of the boolean -- over that same pair "
        "the realtime factor was better calibrated and the verdict rate did not move. What "
        "does still fail loudly is a PROBE clipped while measuring, which is refused before "
        "any figure is stored. "
        "periods=0 means no CPU quota was enforced at all, which is not the same as a quota "
        "that was never hit; throttle_ratio is NULL there rather than 0. "
        "COVERAGE IS NOT UNIFORM, so check it before reading a clean result as campaign-wide. "
        "A run appears here only if its node could answer; one that could not is ABSENT, not "
        "quota_bound=0. Absence tracks the NODE, and the node that cannot answer is not a "
        "random "
        "one -- measured on this cluster, the single node running an older kernel was also the "
        "largest, so it took the most pods and contributed none of the measurements. "
        "What is missing: SELECT r.node_label, COUNT(*) FROM runs r LEFT JOIN "
        "run_validity_view v ON v.config_name=r.config_name AND v.run_id=r.run_id AND "
        "v.container='sut' WHERE v.run_id IS NULL GROUP BY 1. "
        "contended=1 is the OPPOSITE diagnosis and the one quota_bound cannot make: the "
        "container was runnable and got no CPU, without having hit its own ceiling -- other "
        "work on the node crowded it out, and the remedy is a bigger request or a less full "
        "node, not a bigger limit. It reads PSI cpu.pressure: stall_ratio is the fraction of "
        "the trial window in which EVERY task in the cgroup was waiting for CPU. Throttling "
        "raises that counter too, so contended is the residue -- high stall while NOT "
        "quota_bound -- and a container can genuinely be both, in which case the ceiling is "
        "reported because that remedy is in the .vast. "
        "Whether a request-below-limit split cost anything: SELECT container, "
        "AVG(stall_ratio) FROM run_validity_view GROUP BY 1 -- the sut is the one that "
        "matters, since simulation and scenario are expected to lose their burst. "
        "stall_ratio and contended are NULL, never 0, where the sampler had no PSI (cgroup "
        "v1, or a kernel without CONFIG_PSI / cgroup-level full) -- silence, not a pass, and "
        "it does not track the same nodes that lack the throttle counters. "
        "Empty for a campaign recorded before the probe existed, or on a host exposing "
        "neither cgroup layout -- which is silence, not a pass. "
        "Join on (config_name, run_id)."),
    ("temp", "run_view"): (
        "START HERE for per-run and per-configuration questions. One row per run, joined: "
        "config_name, run_id, status, passed, duration_s, errors, failures, tests, "
        "start_time, failure_message, params_json, objective, paramset_id, batch, job_dir, "
        "sysinfo_json. Query unqualified: FROM run_view. Works before postprocessing. "
        "ALWAYS filter with config_name, not run_id alone: run_id restarts at 0 in every "
        "configuration, so run_id alone matches one run per config and returns rows you "
        "did not ask for. "
        "One run: WHERE config_name='goal-1' AND run_id=0. "
        "Pass/fail per config: SELECT config_name, status, COUNT(*) FROM run_view "
        "GROUP BY 1,2. A run's CPU: sysinfo_json::jsonb ->> 'cpu_name' (the index is Postgres: "
        "->> for a JSON field, and the ::jsonb because the column is TEXT; a missing key is "
        "NULL, not an error). "
        "Per-run metrics: join a metric table on (config_name, run_id). "
        "params_json holds each parameter as the scenario received it, so a file-valued "
        "parameter resolves under /results/<campaign>/<config_name>/_config/<value>. "
        "job_dir and sysinfo_json are NULL when the campaign has no recorded host info. "
        "batch is the ask/tell round that proposed the configuration: 0 for every row of a "
        "batch-mode campaign (which has exactly one), the search iteration for a search "
        "campaign, NULL on a store predating the batch table. It is a search's history over "
        "time: SELECT batch, COUNT(*), AVG(objective) FROM run_view GROUP BY 1 ORDER BY 1. "
        "Whether batch means anything is campaign.campaign.mode ('search' | 'batch'). "
        "status='killed' marks a run whose job an operator stopped by hand (stop_job): it "
        "delivered no result and is NOT a trial failure, so exclude it from pass-rate "
        "statistics rather than counting it against the system under test — "
        "WHERE status <> 'killed'. failure_message says which surface stopped it and why. "
        "status='composition_failed' marks a SEARCH parameter set whose configuration "
        "could not be built at all (an unrealizable draw, e.g. no valid obstacle "
        "placement): it never ran, so run_id and every run column are NULL and "
        "config_name falls back to paramset_id. Exclude those rows (WHERE run_id IS NOT "
        "NULL) for run statistics; count them to see how much of the search space is "
        "infeasible. "
        "status='invalid' marks a run the RUNNER threw away because a container the trial "
        "ran against crashed and was restarted under it: the trial carried on against a "
        "process that had lost its state, so its result means nothing. It is the one "
        "status that overrides a written verdict — such a run may well have recorded "
        "'passed', and that is exactly why it is excluded rather than trusted. Like "
        "'killed' it is not a verdict on the system under test, so for pass-rate "
        "statistics use WHERE status NOT IN ('killed','invalid'). What died, on which "
        "node, of what signal, and the dead container's own last log lines are in "
        "container_failure_view, joined on config_name || '/' || run_id = run_key."),
    ("temp", "config_view"): (
        "The campaign's .vast configuration as rows, one per key. Query unqualified: "
        "FROM config_view. Columns: fullkey (JSON path, e.g. '$.execution.containers.scenario.image'), key, "
        "parent, type, value. value is NULL on 'object' and 'array' rows — descend with "
        "fullkey LIKE '$.execution%' instead of expecting a subtree. Use this to explore; "
        "when the path is known, campaign.campaign.config_json::jsonb -> 'execution' -> 'containers' "
        "-> 'scenario' ->> 'image' is "
        "cheaper. "
        "This is the config AS RUN, with defaults filled in — a defaulted key is "
        "indistinguishable from one the author wrote, and comments and anchors are gone. "
        "For what the author actually wrote, read /results/<campaign>/_config/*.vast."),
    ("temp", "container_failure_view"): (
        "What a container DIED of, when the kubelet restarted it under a running trial — "
        "one row per RUN the dead container took down. Query unqualified: FROM "
        "container_failure_view. This is the post-mortem for status='invalid' runs in "
        "run_view, joined on run_key = config_name || '/' || run_id. "
        "It is written by the runner at the moment of the restart and lives in "
        "campaign.db, so it is READABLE ON A CAMPAIGN THAT FAILED AND NEVER "
        "POSTPROCESSED — the campaign that most needs it. "
        "signal_name is the answer most questions want: exit_code 135 is 128+7, i.e. "
        "SIGBUS, and 137 is SIGKILL (an OOM kill). memory_limit/cpu_limit are what the "
        "container DECLARED — NULL means no limit was set at all, which is itself a "
        "finding: such a container is told by the downward API that it has the whole "
        "node. log_tail is the dead instance's own final output (log_status says whether "
        "it could be captured: captured / gone / empty / unavailable). run_key is NULL "
        "when the runner could not resolve which runs the job was carrying; those rows "
        "are kept rather than dropped. "
        "The whole story of one incident: SELECT run_key, node_label, container, role, "
        "exit_code, signal_name, reason, memory_limit FROM container_failure_view "
        "ORDER BY run_key."),
    ("campaign", "container_failure"): (
        "One row per (job, container) that died and was restarted — the base table behind "
        "container_failure_view, which expands runs_json into one row per run. Prefer the "
        "view unless you want incidents rather than affected runs."),
    ("main", "poses"): (
        "One row per entity per sample: where a thing was, and when. Follows the POSE CONTRACT, "
        "so this and 'sim_poses' share these columns and UNION ALL cleanly. This one is derived "
        "from /tf in the rosbag, so its twist.* columns are EMPTY (TF carries no velocity) -- "
        "read sim_poses when you want a true velocity. " + _POSE_CLOCKS_TRANSPORT + _POSE_ORIENTATION),
    ("main", "sim_poses"): (
        "One row per entity per sample, written by the SIMULATOR itself during the run rather "
        "than derived from a bag -- so it exists even for a non-ROS run, which has no rosbag and "
        "therefore no 'poses' table at all. Same POSE CONTRACT columns as 'poses', and it holds "
        "every free-standing body in the world, not only what TF happened to publish. " +
        _POSE_CLOCKS_NATIVE + _POSE_ORIENTATION),
    ("main", "run_log"): (
        "One row per log EVENT, every container joined with /rosout, on the run's playback "
        "clock. ORDER BY wall_ts: sim_time is empty wherever the clock map cannot place a "
        "line (before /clock started, after it stopped), so ordering by it silently reorders "
        "the run. A packed job (execution.runs_per_job > 1) runs several configurations in "
        "sequence into ONE log, and that log is SPLIT between its runs -- a run's rows are "
        "its own, so no run shows another configuration's trial. in_window=0 is this run's "
        "bring-up, verdict and teardown, NOT another run's work; it is not the trial "
        "boundary either -- a failing run's verdict is stamped ~1ms after the window closes, "
        "so 'WHERE in_window=1' drops it. For where the trial ended join "
        "scenario_timestamps. A run with no rows either had no locatable job artifacts or "
        "shares a job and never wrote test.xml."),
    ("main", "postprocessing_steps"): (
        "How each of this campaign's tables was produced. One row per step: plugin, output, "
        "table_name (the index table it became; NULL when the output was not a CSV that "
        "became a table), sources_json, params_json. "
        "SELECT DISTINCT plugin, params_json FROM postprocessing_steps WHERE "
        "table_name='poses'. Use DISTINCT: a step is recorded once per run. A table with "
        "no row here was produced by a step that recorded no provenance."),
    ("campaign", "job"): (
        "One row per execution job, holding that job's host record. Several runs can share "
        "one job, so this answers 'did these runs run on the same machine?'. "
        "sysinfo_json is TEXT holding JSON: sysinfo_json::jsonb ->> 'cpu_name', ->> 'available_cpus', "
        "->> 'platform'. ->> yields TEXT, so cast before comparing a number: "
        "(sysinfo_json::jsonb ->> 'available_cpus')::int. job_dir is campaign-relative. "
        "Join campaign.run on job_id — or use "
        "run_view, which already has. NULL sysinfo_json means the job recorded none."),
    ("main", "scenario_timestamps"): (
        "One row per run: when its scenario reached a terminal state, from the first "
        "scenario-end entry in run_log. timestamp is rosbag time in seconds; wall_ts is "
        "the same moment on the wall clock, which is what run_log is ordered by and is "
        "often the only one present (the clock map does not extrapolate past the end of "
        "/clock). Everything after wall_ts is shutdown, not the trial. status and "
        "message are that entry's verdict. This is the SCENARIO's verdict, which can "
        "disagree with the run's test.xml verdict in run_view.status — comparing the two "
        "finds a scenario that reported success while the harness failed, or the reverse. "
        "Join on (config_name, run_id)."),
    ("main", "runs"): (
        "Per-run dimension table: status/passed/duration_s/errors/failures, the "
        "scalar objective, each scenario parameter as a param_* column (non-scalar "
        "params are JSON-encoded TEXT — read a field with param_x::jsonb ->> 'key' or an element "
        "with param_x::jsonb -> 0, and fan a list out with "
        "jsonb_array_elements(param_x::jsonb)), and the host it ran on "
        "(node_label — which machine, NULL for a local run; instance_type, cpu_name, "
        "available_cpus, available_mem_bytes — bytes, so "
        "divide by 1024*1024*1024 for GiB). shm_peak_bytes/shm_limit_bytes are the run's "
        "shared-memory pool: what /dev/shm held at its fullest, and the size that was in "
        "force — the pair that sizes execution.shm_size, and that explains an exit_code 135 "
        "(SIGBUS) in container_failure_view. Both NULL means unmeasured (a campaign recorded "
        "before the monitor sampled it), which is not 'used none'. "
        "Join to any metric table on (config_name, "
        "run_id). Exists only after postprocessing; run_view answers the same per-run "
        "questions before it. "
        "clock_map_sim_span_s / clock_map_wall_span_s is the run's realtime factor — "
        "simulated seconds bought per wall second — over the window the clock map covers; "
        "GROUP BY node_label to compare machines. Guard the division: both are 0 when "
        "clock_map_source='none'. "
        "node_label identifies a machine without naming it: it is a hash of the "
        "node's name, so runs group by it exactly as they would by hostname, and a "
        "reader holding the real name can recompute the label to find its runs. "
        "status='composition_failed' marks a SEARCH parameter set whose configuration "
        "could not be built at all (an unrealizable draw): it never ran, so run_id and "
        "every run column are NULL, config_name falls back to paramset_id, and only the "
        "param_* columns are meaningful. Add WHERE run_id IS NOT NULL for run "
        "statistics."),
    ("campaign", "campaign"): (
        "One row for the campaign. Execution provenance, and what to compare across "
        "campaigns: robovast_version, execution_type (local|cluster), image, "
        "image_revision (the repo@sha256 the runs used), execution_started_at, elapsed_s. "
        "execution_json holds the rest of the execution record "
        "(execution_json::jsonb -> 'cluster_info', -> 'env'; -> keeps the subobject as JSON, "
        "->> renders it as text). These are NULL until "
        "the campaign has executed. One row per campaign, so WHERE campaign_id IN (...) "
        "asks whether two campaigns' runs used the same image. "
        "stop_kind/stop_reason/batches explain why a search terminated. strategy_state is "
        "an opaque BLOB (masked in results). "
        "config_json is the whole .vast, as TEXT: config_json::jsonb -> 'execution' -> 'containers' "
        "-> 'scenario' ->> 'image' for "
        "a known path, but do NOT 'SELECT config_json' — it exceeds the per-cell limit and "
        "returns truncated. Use config_view to explore it."),
    ("campaign", "batch"): (
        "One row per search batch/iteration; idx is the iteration index — the "
        "search history over time. You rarely need this table: run_view already "
        "carries idx as its `batch` column, so no join is required."),
    ("campaign", "unit"): (
        "One row per evaluated configuration. objectives_json (all named "
        "objectives) and measures_json (quality-diversity measures) live ONLY here "
        "— runs.objective lifts just the single scalar objective. params_json holds "
        "the config's scenario parameters; n_samples/status are roll-ups of its "
        "'run' rows. For per-run detail use run_view, which joins this to run. "
        "n_reps is what the cell was ALLOCATED, as opposed to n_samples, what came "
        "back: under search.repetitions they differ per cell, and n_reps is what the "
        "campaign SPENT on it. NULL means the campaign's execution.runs. "
        "status='evaluated' is the normal case. status='no_sample' marks a cell that RAN "
        "but produced nothing measurable — every run lost to infrastructure rather than to "
        "the system under test — so it carries n_samples=0 and EMPTY objectives_json, and "
        "the search recorded it and continued rather than scoring a fabricated value. Its "
        "runs ARE present in run/run_view with their real statuses, so exclude the unit "
        "(WHERE u.status='evaluated') when averaging objectives, and count "
        "status='no_sample' to see how much of the search space went unmeasured — that is "
        "a coverage loss, not a result. status='composition_failed' is the sibling case "
        "where the draw could not be built at all and never ran."),
    ("campaign", "run"): (
        "One row per individual run, child of unit via unit_id and of a job via job_id. "
        "status is passed/failed/error/killed/invalid/unknown (unknown = test.xml missing "
        "or unparseable; killed = an operator stopped the job by hand; invalid = the "
        "runner discarded the trial after a container restarted under it), passed is 0/1, "
        "with "
        "errors/failures/tests/duration_s/start_time/failure_message. "
        "Available before postprocessing. "
        "run_id is the index WITHIN its config and is not unique on its own; config_name "
        "is on campaign.unit. Prefer run_view, which joins unit and job for you."),
    ("main", "costmaps"): (
        "nav2 OccupancyGrid frames (costmaps / the static map) recorded over the run, "
        "one row per message, written by the rosbags_costmap_to_csv postprocessing step. "
        "topic distinguishes the layers (e.g. /global_costmap/costmap, /local_costmap/"
        "costmap, /map); timestamp is rosbag time in seconds. Grid geometry (use for "
        "spatial reasoning): resolution is meters/cell, width/height are in cells, so the "
        "map covers width*resolution by height*resolution METERS; origin_x/origin_y/"
        "origin_yaw is the pose of cell (0,0)'s corner in frame_id. The occupancy cells "
        "themselves are in 'data' as zlib-compressed, base64-encoded int8 (-1=unknown, "
        "0=free, 1..100=cost, row-major) — masked/truncated in ordinary query results "
        "because they are large; the web run-view fetches a full decoded frame nearest a "
        "time via the campaign 'costmap' endpoint, not via SQL."),
    ("main", "resource_usage"): (
        "What a run COST: CPU and memory sampled every ~1s in each container, one row per "
        "container per process name per tick. Not the get_resource_usage tool, which "
        "reports a lane's free capacity now. "
        "container joins run_log.container ('robovast' is the main container; a simulator "
        "stepped in-process has none of its own, so its processes are in the 'robovast' "
        "rows). timestamp is sim seconds and is empty outside the clock map's range (boot, "
        "bring-up, after /clock stops), so ORDER BY wall_ts — epoch seconds, what the "
        "monitor stamped. in_window=0 is bring-up and teardown, not the trial; every tick "
        "belongs to exactly one run, so SUM over a job's runs is what that job consumed. "
        "Load per container over time: SELECT container, wall_ts, SUM(cpu_percent) FROM "
        "resource_usage WHERE config_name=? AND run_id=? AND in_window=1 GROUP BY 1,2. "
        "shm_used_bytes/shm_total_bytes are the exception to the row grain: /dev/shm is ONE "
        "pool for the whole run, so the same value repeats across a tick's process rows and "
        "across containers — MAX, never SUM. For the run's high-water mark read "
        "runs.shm_peak_bytes instead; these columns are for seeing when it grew. "
        "Join on (config_name, run_id)."),
    ("main", "system_usage"): (
        "What the CONTAINER as a whole reported, one row per container per ~1s tick — the "
        "sibling of resource_usage, which is per PROCESS. Columns beyond the four keys are "
        "whatever the sampler could read on that runtime, so a column may be absent "
        "entirely rather than empty. "
        "The one to know: nr_throttled / nr_periods / throttled_usec, cgroup v2's record of "
        "the kernel STOPPING the container because it hit its CPU quota. This is the only "
        "place a capped run says so — throttling does not fail a run, it just makes it "
        "slower, so its results quietly become partly a measurement of the allocation "
        "rather than of the system under test. "
        "Beside them, where the node could answer: cpu_usage_usec (the CPU time the kernel "
        "billed the cgroup -- exact, where summing resource_usage.cpu_percent is an "
        "estimate); cpu/memory/io_stall_some_usec and _full_usec (PSI: time tasks were "
        "runnable but not running, i.e. CROWDED OUT, which the throttle counters cannot "
        "show); node_cpu_stall_some_usec (the whole MACHINE's pressure -- a node fact "
        "repeated on every row of every container, so never sum it across a pod); "
        "memory_anon / _file / _shmem / _slab (what the memory is MADE OF -- anon+shmem+slab "
        "survives reclaim, file is page cache, so sizing a limit from memory_current "
        "reserves cache the container does not need); "
        "memory_events_max / _oom (allocations the kernel refused) and _oom_kill "
        "(processes it killed for it, the only place a mid-trial death names its cause). "
        "They are MONOTONIC COUNTERS, so read a delta (MAX-MIN) or the last value, never a "
        "SUM. nr_periods=0 means no CPU quota was enforced at all, which is different from "
        "a quota that was never hit — read the ratio nr_throttled/nr_periods, not the raw "
        "count. Prefer run_validity_view, which already takes the in-window delta and "
        "applies the calibrated threshold. Raw: SELECT container, "
        "MAX(nr_throttled)-MIN(nr_throttled) "
        "FROM system_usage WHERE config_name=? AND run_id=? AND in_window=1 GROUP BY 1. "
        "timestamp, wall_ts, in_window and container mean exactly what they do in "
        "resource_usage. Join on (config_name, run_id)."),
}

_DESCRIBE_NOTE = (
    "Start with the views, queried unqualified: run_view for per-run and "
    "per-configuration questions (works before postprocessing), config_view to explore "
    "the campaign's .vast. Filter run_view by config_name — run_id restarts at 0 in every "
    "configuration, so run_id alone silently matches runs in other configs. "
    "Ready-made queries: one run -> SELECT * FROM run_view WHERE config_name=? AND "
    "run_id=?; a config's parameters -> SELECT DISTINCT params_json FROM run_view WHERE "
    "config_name=?; a run's host -> SELECT sysinfo_json FROM run_view WHERE ...; configs "
    "that produced runs -> SELECT DISTINCT config_name FROM run_view (for ALL configs, "
    "including any that never ran, list the campaign's directories instead); a search's "
    "rounds -> SELECT batch, COUNT(*), AVG(objective) FROM run_view GROUP BY 1 ORDER BY 1 "
    "(batch is meaningful only when campaign.campaign.mode is 'search'); how a metric "
    "was produced -> main.postprocessing_steps; what the campaign ran on -> "
    "campaign.campaign. "
    "Join the 'runs' table (param_* columns + status/duration) to any metric table "
    "on (config_name, run_id). campaign.db is attached as schema 'campaign'. "
    "Each column is listed as 'name TYPE': numeric CSV columns are stored as "
    "INTEGER/REAL, so compare and ORDER BY them directly. A TEXT column holds text — "
    "ordering it is lexicographic ('10.022' < '9.5'), so CAST(col AS REAL) first. A table's 'column_notes' flags a column whose type "
    "does not tell the whole story — read it before aggregating that column. "
    "JSON columns (config_json, execution_json, sysinfo_json, params_json, a non-scalar "
    "param_* column) are TEXT holding JSON, and this is Postgres — not SQLite, so there is "
    "no json_extract/json_each/json_tree. Read a field with col::jsonb -> 'a' -> 'b' ->> 'c' "
    "(-> descends and keeps JSON, ->> ends the path and yields TEXT), index an array 0-based "
    "with -> 0, and fan one out with jsonb_array_elements(col::jsonb). A missing key is NULL "
    "rather than an error, and ->> is TEXT — cast it ((col::jsonb ->> 'n')::double precision) "
    "before comparing or ordering numerically. "
    "Extra aggregate functions are available: STDDEV, VARIANCE, MEDIAN, and "
    "PERCENTILE(col, p) where p is 0..100. REGEXP(pattern, col) and SQRT(x) are also "
    "registered."
)


#: Internal bookkeeping tables: not results, so not listed as tables —
#: ``_column_notes`` is folded into the owning table's entry instead.
_INTERNAL_TABLES = ("_table_name_map", "_column_notes")


def describe_data_db(campaign_dir, campaign_id: str | None = None) -> dict:
    """Return ``{tables: [{schema, table, columns, rows, description}], note}``.

    Works before postprocessing: when the campaign has no rows in the index, the
    attached ``campaign`` schema (config/objectives/batch progress) is still described.
    """
    from robovast.results_processing import \
        index_query  # pylint: disable=import-outside-toplevel

    try:
        return index_query.describe_index(
            campaign_id or campaign_id_of(campaign_dir))
    except index_query.IndexQueryError as exc:
        raise DataQueryError(str(exc)) from exc


def _cap_cell(value):
    """Bound a single cell's width. BLOBs are masked; oversized text is truncated."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<BLOB {len(bytes(value))} bytes>"
    if isinstance(value, str) and len(value.encode("utf-8", "replace")) > _MAX_CELL_BYTES:
        return value.encode("utf-8", "replace")[:_MAX_CELL_BYTES].decode(
            "utf-8", "ignore") + f"…<truncated, {len(value)} chars total>"
    return value


def _cap_result_size(rows: list, max_bytes: int = _MAX_RESULT_BYTES) -> tuple:
    """Trim *rows* until the reply fits *max_bytes*; say whether it was.

    Measured cumulatively rather than by serializing the whole list and bisecting: the
    payload being bounded is the one that would otherwise be built in full first, and
    building it to discover it is too big spends exactly the memory the cap exists to
    avoid.
    """
    total = 0
    for i, row in enumerate(rows):
        total += len(json.dumps(row, default=str).encode("utf-8", "replace"))
        if total > max_bytes:
            # At least one row, always: an empty result would read as "no data" rather
            # than "your query was too wide", which are different answers.
            return rows[:max(1, i)], True
    return rows, False


def campaign_id_of(campaign_dir) -> str:
    """The campaign a path belongs to, from anywhere inside it.

    The directory name *is* the campaign id, and a caller may hand in the campaign root,
    one configuration, or one run -- the notebook surface routinely does, and has nothing
    else to go on.

    **A caller that already knows the id should pass it instead**, and the service does.
    Deriving it from a path asks the filesystem a question that ingestion has already
    answered: the rows are in the index, so the campaign needs no directory here at all,
    and on the cluster lane the "directory" is a cache the service deliberately never
    fills. Measured: an imported campaign left an EMPTY cache dir, which exists -- so the
    guard below does not fire -- and carries no ``campaign.db`` to walk up to, so every
    query against it was refused while its 38838 rows sat in the index.
    """
    from robovast.common.analysis.db import (  # pylint: disable=import-outside-toplevel
        campaign_root)

    path = Path(campaign_dir)
    if not path.exists():
        # The cluster lane resolves a query to the campaign's cache dir *without fetching
        # it* -- there is nothing left to fetch, the rows are in the index -- so the path
        # names a campaign that has no directory on this machine at all. Walking up for
        # campaign.db would refuse every such query. A path that does not exist carries no
        # structure to walk, so its name is the id; an existing path that is not a campaign
        # still raises below.
        return path.name

    return campaign_root(path).name


def query_data_db(campaign_dir, sql: str, max_rows: int = 500,
                  max_bytes: int | None = None, campaigns=None,
                  campaign_id: str | None = None) -> dict:
    """Run a read-only ``SELECT``; return ``{columns, rows, row_count, truncated}``.

    A query spanning campaigns (an A/B comparison, a whole search arm) needs no second
    handle: every campaign is in the one index. It does need to *say so* -- *campaigns*
    names the ids the session may see. Without it the session is confined to
    *campaign_dir*'s campaign by the index itself, because a cross-campaign read reached
    by forgetting a predicate is indistinguishable from the answer that was wanted.

    *max_bytes* overrides :data:`_MAX_RESULT_BYTES`. That default is sized for a caller
    who has to *read* the reply into a context window; a caller that renders it — the run
    view's panels, the data browser's table — is bounded by a browser rather than by a
    token budget, and clamping it to 16k tokens truncates a chart at ~120 rows of ``poses``
    while the row cap it reports still says 5000. Callers who plot ask for more.

    Raises :class:`DataQueryError` for a rejected (non-read) or invalid query.
    """
    # The rows live in the central index now. The signature is unchanged because the
    # scoping a campaign_dir carried is still real -- it is simply a WHERE clause the
    # caller writes rather than a file that has to be fetched and opened.
    from robovast.results_processing import \
        index_query  # pylint: disable=import-outside-toplevel

    try:
        return index_query.query_index(
            sql, max_rows=max_rows, max_bytes=max_bytes,
            campaign_id=campaign_id or campaign_id_of(campaign_dir),
            campaigns=campaigns)
    except index_query.IndexQueryError as exc:
        raise DataQueryError(str(exc)) from exc


def stream_query_csv(campaign_dir, sql: str, campaign_id: str | None = None):
    """Yield the same ``SELECT`` as CSV text, row by row and with **no row cap**.

    :func:`query_data_db` clamps to 5000 rows because its result is a JSON payload someone
    has to hold — reasonable for a caller reading the answer, useless for a caller who
    wants the data. This is the way out: the HTTP layer streams it, so a result larger
    than memory is fine at both ends, and an MCP tool can hand over the URL instead of
    reporting ``truncated`` and leaving the rest unreachable.

    Same read-only index session as the JSON path, so it is exactly as read-only — a second
    query entry point must not be a second security decision. Cells are **not** width-capped here: the
    cap exists to keep a JSON reply readable, and truncating an exported value would
    corrupt the export.
    """
    import csv
    import io

    from robovast.results_processing import (  # pylint: disable=import-outside-toplevel
        index_dialect, index_query)

    import psycopg  # pylint: disable=import-outside-toplevel

    # Scoped exactly like the JSON path -- a second query entry point must not be a second
    # scoping decision any more than it is a second read-only decision.
    conn = index_query.open_index(readonly=True,
                                  campaigns=[campaign_id
                                             or campaign_id_of(campaign_dir)])
    try:
        try:
            cursor = conn.execute(index_dialect.translate(sql))  # pylint: disable=no-member
        except psycopg.Error as exc:
            message = str(exc).strip()
            if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
                raise DataQueryError(
                    f"Only read-only SELECT queries are allowed (rejected: {message}).") from exc
            raise DataQueryError(f"SQL error: {message}") from exc
        if cursor.description is None:
            raise DataQueryError("query returned no result set (only SELECT is supported)")

        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def _flush() -> str:
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return text

        writer.writerow([d.name for d in cursor.description])
        yield _flush()
        while True:
            batch = cursor.fetchmany(1000)
            if not batch:
                return
            writer.writerows(batch)
            yield _flush()
    finally:
        conn.close()  # pylint: disable=no-member


# NOTE: the costmap-frame reader lived here; it moved to ``robovast_nav`` as a
# package-provided service endpoint (``robovast_nav/service_endpoints.py:CostmapEndpoint``),
# which reads the ``costmaps`` table via :func:`open_data_db`. Core keeps only the generic
# read-only opener above.
