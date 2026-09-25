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

"""Read-only SQL over a campaign's results -- the service's and the MCP tools' one seam.

Parameterised by the campaign **directory**, because the directory is the database: a
query is answered by :mod:`robovast_data`'s engine over the tables the campaign's
``.cache/`` holds, building on first use what a query names and has not been built for the
runs it asks about. There is no server to reach and nothing to ingest, so the same answer
comes back for a campaign still running, a finished one, and one imported from an archive.

**Scope is the file set.** A connection defines views over the named campaigns' files and
nothing else, so a query that forgets ``WHERE campaign_id = ...`` answers about the campaign
it was asked about rather than a corpus. Spanning campaigns is explicit: pass ``campaigns``.

**Read-only is the engine's**: only a single ``SELECT`` is answered, the connection may read
the campaigns' table files and nothing else, and a query that runs past its time is stopped.
"""

import csv
import io
import json
import logging
from contextlib import ExitStack
from pathlib import Path

from robovast_data import Engine, QueryError, Scope, scope_of
from robovast_data.notes import notes_for

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


class Row(tuple):
    """One result row, readable by position and by column name.

    A plugin reading ``row["timestamp"]`` and a health check reading ``row[0]`` are both served,
    which is the contract each was written against.
    """

    def __new__(cls, values, columns):
        row = super().__new__(cls, values)
        row._columns = columns
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._columns[key])
            except KeyError:
                raise KeyError(key) from None
        return tuple.__getitem__(self, key)

    def get(self, key, default=None):
        index = self._columns.get(key)
        return default if index is None else tuple.__getitem__(self, index)

    def keys(self):
        return list(self._columns)


class Cursor:
    """The result of :meth:`CampaignConnection.execute`: ``fetchone``, ``fetchall``, ``description``."""

    def __init__(self, rows, columns):
        self._rows = rows
        self._index = {name: i for i, name in enumerate(columns)}
        self.description = [(name,) for name in columns]
        self._position = 0

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        self._position += 1
        return Row(self._rows[self._position - 1], self._index)

    def fetchall(self):
        rest = [Row(r, self._index) for r in self._rows[self._position:]]
        self._position = len(self._rows)
        return rest


class CampaignConnection:
    """A read-only connection to one campaign's tables, for a plugin: an endpoint, a check.

    ``execute(sql, params)`` builds what the statement names, runs it on a fresh sandboxed
    connection, and returns a :class:`Cursor`. Placeholders are ``?`` (or ``$1``); the
    campaign's record is the ``campaign`` schema; every table carries ``campaign_id``,
    ``config_name`` and ``run_id``, and only this campaign's rows are there to read.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def execute(self, sql: str, params=None) -> Cursor:
        try:
            with self._engine.execute(sql, params) as (con, _problems):
                columns = [d[0] for d in con.description]
                return Cursor(con.fetchall(), columns)
        except QueryError as exc:
            raise DataQueryError(str(exc)) from exc

    def close(self) -> None:
        """Nothing is held open between statements; here for the connection contract."""


def _scopes(campaign_dir, campaign_id=None, campaigns=None) -> list:
    """The campaigns a query may see: *campaign_dir*'s, and the ones *campaigns* names."""
    root = Path(campaign_dir)
    primary = campaign_id or campaign_id_of(root)
    base = root if root.name == primary else root.parent / primary
    names = [primary] + [c for c in (campaigns or []) if c != primary]
    scopes = []
    for name in names:
        path = base.parent / name
        if not (path / "campaign.db").is_file():
            raise DataQueryError(f"no campaign {name!r}: {path} holds no campaign.db")
        scopes.append(Scope(str(path)))
    return scopes


def _engine(campaign_dir, campaign_id=None, campaigns=None) -> Engine:
    return Engine(_scopes(campaign_dir, campaign_id, campaigns))


def open_data_db(campaign_dir, campaign_id: str | None = None) -> CampaignConnection:
    """A read-only :class:`CampaignConnection` to one campaign -- the seam for plugins.

    Tables a statement names are built on first use; the connection reads this campaign's
    rows and nothing else.
    """
    return CampaignConnection(_engine(campaign_dir, campaign_id))


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

#: For a pose table the SIMULATOR wrote itself. One MEASUREMENT clock, and it is the true one -- so
#: the warning above does not apply here and would be actively wrong: there is no `stamp` column to
#: point at, and this `timestamp` is exactly the quantity the other table's `stamp` is. `wall_time`
#: rides along and is named here rather than left out: a reader who sees two time columns and is
#: told the table has one clock concludes the description is stale, not that the second is a bridge.
_POSE_CLOCKS_NATIVE = (
    "ONE MEASUREMENT CLOCK, and it is the honest one: `timestamp` is exact simulated seconds, taken "
    "inside the simulator when the pose was true, so unlike the `poses` table there is no "
    "arrival/measurement split and no `stamp` column -- difference and ORDER BY this one freely. "
    "Better still, do not difference at all: twist.linear.* / twist.angular.* are the TRUE "
    "world-frame velocities read straight from the physics solver, so a speed is "
    "SQRT(POWER(\"twist.linear.x\",2)+POWER(\"twist.linear.y\",2)) with no window function and no "
    "interval to get wrong. The second time column, `wall_time`, is NOT a second measurement of the "
    "pose: it is Unix epoch seconds for the same sample, there to join this table to whatever is "
    "stamped in wall time (run_log, resource_usage) on a run that has no rosbag to relate them "
    "otherwise. Never difference it -- it advances with the host, not with the simulation. "
)

#: Also shared: what the orientation columns are, and which one is a projection.
_POSE_ORIENTATION = (
    "Orientation is a QUATERNION (orientation.x/y/z/w) -- that is what the producer emitted. "
    "`orientation.yaw` is derived from it when the table is built and is a PLANAR projection: fine for a robot "
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
        "Join on (config_name, run_id)."),
    ("main", "run_validity_view"): (
        "WAS THIS RUN A CLEAN OBSERVATION? One row per (run, container) saying whether the "
        "kernel capped it at its OWN CPU limit, and whether it was crowded out by other "
        "work: config_name, run_id, container, periods, throttled, throttled_usec, "
        "stalled_some_usec, stalled_full_usec, throttle_ratio, stall_ratio, quota_bound, "
        "contended. Query unqualified: FROM "
        "run_validity_view. It reads system_usage, built for the runs asked about on first use. "
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
        "clipped, and whether that cost anything is answered by run_clock instead. "
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
    ("main", "pose_track_view"): (
        "WHERE DID IT GO? One row per TRACK -- one entity (frame) of one run as one pose table "
        "recorded it -- summarised over EVERY recorded pose: source, config_name, run_id, frame, "
        "points, length_m, duration_s, avg_speed_m_s, max_speed_m_s, max_step_m, start_x/y/z/yaw, "
        "end_x/y/z/yaw, min_/max_x/y/z. Query unqualified: FROM pose_track_view. "
        "Read this INSTEAD of differencing a pose table yourself: it takes each table's "
        "MEASUREMENT clock (`stamp` on a bag-derived table, `timestamp` on a simulator-written "
        "one), never the arrival clock, and sums in 3D. "
        "`source` is the table the track came from ('poses', 'sim_poses', ...). The same "
        "entity can appear once per source -- pick one; they are two recordings, not two "
        "entities. A sample with no measurement time (a latched /tf_static transform) is not "
        "a point here. "
        "A REPOSITION COUNTS AS TRAVEL: a body the simulator spawns at the origin and then "
        "places at its start pose adds that jump to length_m and max_speed_m_s. max_step_m is "
        "the largest single step -- when it is metres while the body drives centimetres per "
        "sample, the track contains a teleport; compare against the bag-derived ground-truth "
        "frame or cut the track at its start pose. "
        "One run's tracks: SELECT source, frame, points, length_m, duration_s FROM "
        "pose_track_view WHERE campaign_id = <id> AND config_name = ? AND run_id = ?. "
        "Distance driven per config: SELECT config_name, AVG(length_m) FROM pose_track_view "
        "WHERE campaign_id = <id> AND source = 'poses' AND frame = 'base_link' GROUP BY 1. "
        "Join on (config_name, run_id)."),
    ("main", "run_view"): (
        "START HERE for per-run and per-configuration questions. One row per run, joined: "
        "config_name, run_id, status, passed, duration_s, errors, failures, tests, "
        "start_time, failure_message, params_json, channels_json, objective, paramset_id, "
        "batch, job_dir, sysinfo_json. Query unqualified: FROM run_view. Answers while the "
        "campaign runs. "
        "ALWAYS filter with config_name, not run_id alone: run_id restarts at 0 in every "
        "configuration, so run_id alone matches one run per config and returns rows you "
        "did not ask for. "
        "One run: WHERE config_name='goal-1' AND run_id=0. "
        "Pass/fail per config: SELECT config_name, status, COUNT(*) FROM run_view "
        "GROUP BY 1,2. A run's CPU: sysinfo_json::JSON ->> 'cpu_name' (->> for a JSON field, "
        "and the ::JSON because the column is TEXT; a missing key is NULL, not an error). "
        "Per-run metrics: join a metric table on (config_name, run_id). "
        "params_json holds each parameter as the scenario received it, so a file-valued "
        "parameter resolves under /results/<campaign>/<config_name>/_config/<value>. "
        "channels_json holds what EVERY variation channel resolved to for the "
        "configuration -- {scenario, sim, sut}, the keys a .vast writes destinations on "
        "-- so a sim: or sut: factor is readable there verbatim, including a destination "
        "too long or too XPath-shaped to have become a param_ column: "
        "channels_json::JSON -> 'sut'. NULL on a store predating it. "
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
    ("main", "config_view"): (
        "The campaign's .vast configuration as rows, one per key. Query unqualified: "
        "FROM config_view. Columns: fullkey (JSON path, e.g. '$.execution.containers.scenario.image'), key, "
        "parent, type, value. value is NULL on 'object' and 'array' rows — descend with "
        "fullkey LIKE '$.execution%' instead of expecting a subtree. Use this to explore; "
        "when the path is known, campaign.campaign.config_json::JSON -> 'execution' -> 'containers' "
        "-> 'scenario' ->> 'image' is "
        "cheaper. "
        "This is the config AS RUN, with defaults filled in — a defaulted key is "
        "indistinguishable from one the author wrote, and comments and anchors are gone. "
        "For what the author actually wrote, read /results/<campaign>/_config/*.vast."),
    ("main", "container_failure_view"): (
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
        "every named body in the world -- links, wheels and attached tools included -- not only "
        "what TF happened to publish. " +
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
        "table_name (the table it became; NULL when the output is not a table), sources_json, "
        "params_json. "
        "SELECT DISTINCT plugin, params_json FROM postprocessing_steps WHERE "
        "table_name='poses'. Use DISTINCT: a step is recorded once per run. A table with "
        "no row here was produced by a step that recorded no provenance."),
    ("campaign", "job"): (
        "One row per execution job, holding that job's host record. Several runs can share "
        "one job, so this answers 'did these runs run on the same machine?'. "
        "sysinfo_json is TEXT holding JSON: sysinfo_json::JSON ->> 'cpu_name', ->> "
        "'available_cpus', ->> 'platform'. ->> yields TEXT, so cast before comparing a number: "
        "CAST(sysinfo_json::JSON ->> 'available_cpus' AS DOUBLE). job_dir is campaign-relative. "
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
        "scalar objective, each varied parameter as a param_* column (non-scalar "
        "params are JSON-encoded TEXT — read a field with param_x::JSON ->> 'key' or an "
        "element with param_x::JSON -> 0, and fan a list out with "
        "unnest(from_json(param_x, '[\"JSON\"]'))), and the host it ran on "
        "(node_label — which machine, NULL for a local run; instance_type, cpu_name, "
        "available_cpus, available_mem_bytes — bytes, so divide by 1024*1024*1024 for GiB). "
        "probed=1 marks a run a person read into while it ran: exclude it from anything a "
        "published number rests on; it is never folded into status. "
        "Built from campaign.db, so it answers while the campaign runs; a run still going "
        "has NULL outcome columns. Join to any metric table on (config_name, run_id). "
        "node_label identifies a machine without naming it: it is a hash of the "
        "node's name, so runs group by it exactly as they would by hostname, and a "
        "reader holding the real name can recompute the label to find its runs. "
        "status='composition_failed' marks a SEARCH parameter set whose configuration "
        "could not be built at all (an unrealizable draw): it never ran, so run_id and "
        "every run column are NULL, config_name falls back to paramset_id, and only the "
        "param_* columns are meaningful. Add WHERE run_id IS NOT NULL for run "
        "statistics. "
        "A scenario: factor keeps its own name (param_speed); a sim: or sut: factor is "
        "prefixed by its channel and named by the END of its destination, extended "
        "leftwards only as far as it must be to stay unique in the campaign — "
        "sut.nav2.local_costmap.local_costmap.ros__parameters.inflation_layer."
        "inflation_radius is param_sut_inflation_radius. Do not guess these names: read "
        "them from the table's columns, and read run_view.channels_json for a "
        "destination that got no column."),
    ("main", "run_clock"): (
        "One row per run: what relates its wall-stamped log to sim time, and how well. "
        "clock_map_source names the producer ('ros_clock_bag' from /clock, "
        "'roqsim_run_npz' from the simulator's own record); 'none' means the run's log lines "
        "have no sim_time at all. clock_map_samples is how many decimated samples the map "
        "holds. clock_map_sim_span_s / clock_map_wall_span_s is the run's realtime factor -- "
        "simulated seconds bought per wall second -- over the window the map covers; GROUP "
        "BY runs.node_label to compare machines. Guard the division: both spans are 0 when "
        "the source is 'none'. Join on (config_name, run_id)."),
    ("main", "_recording"): (
        "What each run's recordings hold, one row per recorded topic: recording, topic, "
        "type, messages, bytes, and the table it became -- or, when it became none, the "
        "reason (bulk data such as images, a type with no definition anywhere). Read it to "
        "see what a large campaign recorded and what to leave out. "
        "Join on (config_name, run_id)."),
    ("campaign", "campaign"): (
        "One row for the campaign. Execution provenance, and what to compare across "
        "campaigns: robovast_version, execution_type (local|cluster), image, "
        "image_revision (the repo@sha256 the runs used), execution_started_at, elapsed_s. "
        "execution_json holds the rest of the execution record "
        "(execution_json::JSON -> 'cluster_info', -> 'env'; -> keeps the subobject as JSON, "
        "->> renders it as text). These are NULL until "
        "the campaign has executed. One row per campaign, so WHERE campaign_id IN (...) "
        "asks whether two campaigns' runs used the same image. "
        "stop_kind/stop_reason/batches explain why a search terminated. strategy_state is "
        "an opaque BLOB (masked in results). "
        "config_json is the whole .vast, as TEXT: config_json::JSON -> 'execution' -> 'containers' "
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
        "Available while the campaign runs. "
        "run_id is the index WITHIN its config and is not unique on its own; config_name "
        "is on campaign.unit. Prefer run_view, which joins unit and job for you."),
    ("main", "costmaps"): (
        "nav2 OccupancyGrid frames (costmaps / the static map) recorded over the run, "
        "one row per message, decoded from the recording's OccupancyGrid topics. "
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
        "reports the cluster's free capacity now. "
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
        "across containers — MAX, never SUM. A run's high-water mark, bring-up included, and "
        "the limit in force: SELECT MAX(shm_used_bytes), MAX(shm_total_bytes) FROM "
        "resource_usage WHERE config_name=? AND run_id=? -- the pair that sizes "
        "execution.shm_size and explains an exit_code 135 (SIGBUS) in container_failure_view. "
        "NULL means unmeasured, not 'used none'. "
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
    "per-configuration questions (answers while the campaign runs), config_view to explore "
    "the campaign's .vast. Filter run_view by config_name — run_id restarts at 0 in every "
    "configuration, so run_id alone silently matches runs in other configs. "
    "Ready-made queries: one run -> SELECT * FROM run_view WHERE config_name=? AND "
    "run_id=?; a config's parameters -> SELECT DISTINCT params_json FROM run_view WHERE "
    "config_name=?; a run's host -> SELECT sysinfo_json FROM run_view WHERE ...; configs "
    "that produced runs -> SELECT DISTINCT config_name FROM run_view (for ALL configs, "
    "including any that never ran, list the campaign's directories instead); a search's "
    "rounds -> SELECT batch, COUNT(*), AVG(objective) FROM run_view GROUP BY 1 ORDER BY 1 "
    "(batch is meaningful only when campaign.campaign.mode is 'search'); how a table "
    "was produced -> postprocessing_steps; what the campaign ran on -> campaign.campaign. "
    "Join the 'runs' table (param_* columns + status/duration) to any metric table "
    "on (config_name, run_id). The campaign's record is the schema 'campaign'. "
    "A TABLE IS BUILT THE FIRST TIME A QUERY NAMES IT: 'built' says for how many of its "
    "runs it already is, and a query naming it builds the rest -- only the runs its WHERE "
    "restricts it to with config_name = / run_id = / IN (...), so narrow a first look at a "
    "large table to one run. 'columns' is empty until a table is built for some run. "
    "Each column is listed as 'name TYPE': numeric columns are INTEGER/REAL, so compare and "
    "ORDER BY them directly. A TEXT column holds text — ordering it is lexicographic "
    "('10.022' < '9.5'), so CAST(col AS DOUBLE) first. A table's 'column_notes' flags a "
    "column whose type does not tell the whole story — read it before aggregating that "
    "column. "
    "The engine is DuckDB. JSON columns (config_json, execution_json, sysinfo_json, "
    "params_json, a non-scalar param_* column) are TEXT holding JSON: read a field with "
    "col::JSON -> 'a' -> 'b' ->> 'c' (-> descends and keeps JSON, ->> ends the path and "
    "yields TEXT), index an array 0-based with -> 0, and fan one out with "
    "unnest(from_json(col, '[\"JSON\"]')). A missing key is NULL rather than an error, and "
    "->> is TEXT — CAST(... AS DOUBLE) before comparing or ordering numerically. "
    "CAST(x AS REAL) means a double and CAST(x AS INTEGER) truncates, as they did in SQLite. "
    "Aggregates: STDDEV, VARIANCE, MEDIAN, and PERCENTILE(col, p) where p is 0..100. "
    "REGEXP(pattern, col) searches a string."
)

#: DuckDB's type names in the vocabulary the note documents, so its instructions read
#: against its own output.
_TYPE_NAMES = {
    "BIGINT": "INTEGER", "INTEGER": "INTEGER", "SMALLINT": "INTEGER", "TINYINT": "INTEGER",
    "UBIGINT": "INTEGER", "HUGEINT": "INTEGER", "BOOLEAN": "INTEGER",
    "DOUBLE": "REAL", "FLOAT": "REAL", "DECIMAL": "REAL",
    "VARCHAR": "TEXT", "NULL": "TEXT", "string": "TEXT", "large_string": "TEXT",
    "int64": "INTEGER", "int32": "INTEGER", "bool": "INTEGER", "double": "REAL",
    "float": "REAL", "null": "TEXT",
}


def _described_type(name: str) -> str:
    return _TYPE_NAMES.get(name.split("(")[0], name)


def _description(schema: str, table: str):
    return _TABLE_DESCRIPTIONS.get((schema, table)) or _TABLE_DESCRIPTIONS.get(("main", table))


def describe_data_db(campaign_dir, campaign_id: str | None = None) -> dict:
    """``{tables: [{schema, table, columns, rows, built, runs, description, ...}], note}``.

    Nothing is built: a table is listed with how many of its runs it is built for, and its
    columns once it is built for one. The campaign's record and the views over it answer
    whatever has been built.
    """
    try:
        catalog = _engine(campaign_dir, campaign_id).catalog()
    except (QueryError, FileNotFoundError) as exc:
        raise DataQueryError(str(exc)) from exc
    order = {"view": 0, "table": 1, "record": 2}
    entries = []
    for name, entry in sorted(catalog.items(), key=lambda kv: (order[kv[1]["kind"]], kv[0])):
        schema, _, table = name.rpartition(".")
        schema = schema or "main"
        columns = entry["columns"] or []
        item = {"schema": schema, "table": table,
                "columns": [f"{c} {_described_type(t)}" for c, t in columns],
                "rows": entry.get("rows"), "kind": entry["kind"]}
        if entry["runs"] is not None:
            item["runs"] = entry["runs"]
            item["built"] = entry["built"]
            if entry["failed"]:
                item["failed"] = dict(list(entry["failed"].items())[:20])
        description = _description(schema, table)
        if description:
            item["description"] = description
        notes = notes_for(table, [c for c, _ in columns])
        if notes:
            item["column_notes"] = notes
        entries.append(item)
    return {"tables": entries, "note": _DESCRIBE_NOTE}


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
    """The campaign a path belongs to, from anywhere inside it: its directory's name."""
    path = Path(campaign_dir)
    if not path.exists():
        return path.name
    try:
        return scope_of(str(path)).campaign_id
    except FileNotFoundError as exc:
        raise DataQueryError(str(exc)) from exc


def query_data_db(campaign_dir, sql: str, max_rows: int = 500,
                  max_bytes: int | None = None, campaigns=None,
                  campaign_id: str | None = None) -> dict:
    """Run a read-only ``SELECT``; return ``{columns, rows, row_count, truncated, note?}``.

    *campaigns* names further campaigns the query may see, beside *campaign_dir*'s.
    *max_bytes* overrides :data:`_MAX_RESULT_BYTES`: that default is sized for a caller who
    reads the reply into a context window; one that renders it (the run view's panels, the
    data browser) is bounded by a browser instead.

    Raises :class:`DataQueryError` for a rejected or invalid query.
    """
    max_rows = max(1, min(int(max_rows), 5000))
    max_bytes = _MAX_RESULT_BYTES if max_bytes is None else max(1024, int(max_bytes))
    try:
        with _engine(campaign_dir, campaign_id, campaigns).execute(sql) as (con, problems):
            columns = [d[0] for d in con.description]
            fetched = con.fetchmany(max_rows + 1)
    except (QueryError, FileNotFoundError) as exc:
        raise DataQueryError(str(exc)) from exc
    truncated = len(fetched) > max_rows
    rows = [{c: _cap_cell(v) for c, v in zip(columns, r)} for r in fetched[:max_rows]]
    rows, size_capped = _cap_result_size(rows, max_bytes)
    result = {"columns": columns, "row_count": len(rows),
              "truncated": truncated or size_capped, "rows": rows}
    notes = []
    if size_capped:
        notes.append(
            f"stopped at {len(rows)} rows: the reply reached the {max_bytes // 1024} KB "
            "ceiling. Rows are capped separately from size, and a wide table reaches this "
            "long before max_rows. Aggregate in SQL (COUNT/AVG/MIN/MAX, GROUP BY) or select "
            "the columns you need -- or export the whole result as CSV instead of reading "
            "it here.")
    if problems:
        shown = "; ".join(str(p) for p in problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        notes.append(f"the answer leaves out what could not be built: {shown}{more}")
    if notes:
        result["note"] = " ".join(notes)
    return result


def stream_query_csv(campaign_dir, sql: str, campaign_id: str | None = None):
    """Yield the same ``SELECT`` as CSV text, batch by batch and with **no row cap**.

    :func:`query_data_db` clamps rows because its result is a JSON payload someone has to
    hold; this is the way out for a caller who wants the data. Same engine, same fence, so
    it is exactly as read-only. Cells are **not** width-capped: that cap keeps a JSON reply
    readable, and truncating an exported value would corrupt the export.
    """
    engine = _engine(campaign_dir, campaign_id)
    stack = ExitStack()
    try:
        con, _problems = stack.enter_context(engine.execute(sql))
    except (QueryError, FileNotFoundError) as exc:
        stack.close()
        raise DataQueryError(str(exc)) from exc
    with stack:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def _flush() -> str:
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return text

        writer.writerow([d[0] for d in con.description])
        yield _flush()
        while True:
            batch = con.fetchmany(1000)
            if not batch:
                return
            writer.writerows(batch)
            yield _flush()

__all__ = ["CampaignConnection", "DataQueryError", "Row", "campaign_id_of", "describe_data_db",
           "open_data_db", "query_data_db", "stream_query_csv"]
