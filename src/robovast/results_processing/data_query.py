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

"""Read-only SQL over a campaign's ``data.db`` — a **directory-based** helper.

This is the single implementation of "describe / query a campaign's results",
parameterized by the campaign **directory** so it serves both callers:

* the MCP ``run_data`` plugin, which resolves a ``campaign_id`` → dir via
  ``results_resolver`` (or delegates to a configured service); and
* the ``robovast-service`` (``describe_campaign_data`` / ``query_campaign_data_sql``
  on :class:`~robovast.service.interface.RobovastInterface`), which resolves the
  dir per transport — local disk, or an object-store fetch on the cluster.

Only ``SELECT`` is allowed (a ``sqlite3`` authorizer + ``mode=ro``); the campaign's
``campaign.db`` is attached read-only as schema ``campaign`` so params/objectives
join in one query. The ``vast eval gui`` notebook path reads the same ``data.db``
directly and is unaffected.
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


# Actions a pure read query needs; everything else (INSERT/UPDATE/DELETE/CREATE/
# DROP/ATTACH/DETACH/PRAGMA-writes/...) is denied by the authorizer.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _readonly_authorizer(action, _arg1, _arg2, _dbname, _trigger):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


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


def _attach_ro(conn: sqlite3.Connection, db_path: Path, alias: str) -> None:
    """Attach *db_path* read-only under *alias*, best-effort (logs on failure)."""
    try:
        conn.execute(f'ATTACH DATABASE ? AS "{alias}"', (f"file:{db_path}?mode=ro",))
    except sqlite3.Error as e:
        logger.debug("could not attach %s as %s: %s", db_path, alias, e)


#: Flat views created per attached ``campaign.db``, as ``{name: SQL body}``.
#:
#: They exist because every per-run question needs ``run JOIN unit`` (``run_id`` is only
#: unique *within* a config) and now ``LEFT JOIN job`` for the host record. A forgotten
#: join does not raise — it silently returns rows from the wrong configs — so the join is
#: made a property of the schema rather than something the caller has to remember.
#:
#: They are **TEMP views on the connection**, not objects in the file, for three reasons:
#: the store is attached read-only so nothing may be written to it; an existing store
#: predating the ``job`` table would carry a view referencing a table it does not have;
#: and defining them here means a change to a view never needs a schema migration. The
#: cost is that they must be created for each attached alias, which
#: :func:`_create_campaign_views` does.
#: Names of the views, in listing order. The bodies are built per store by
#: :func:`_campaign_view_sql`, which adapts to the tables that store actually has.
_CAMPAIGN_VIEW_NAMES = ("run_view", "config_view", "container_failure_view")

#: The same treatment for the metrics store (``data.db``, schema ``main``) rather than the
#: campaign store. Separate because the two are attached independently -- a campaign that has
#: not been postprocessed has ``campaign`` and no ``main`` tables, and one imported without its
#: store has the reverse -- so a view over one must not fail to be created because the other
#: is absent.
_MAIN_VIEW_NAMES = ("run_validity_view",)


def _tables_in(conn: sqlite3.Connection, schema: str) -> set:
    """Table names present in an attached *schema*."""
    try:
        return {r["name"] for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _campaign_view_sql(schema: str, have: set) -> dict:
    """``{view_name: SELECT}`` for the tables *schema* actually has.

    A store on disk may predate the ``job`` table — ``data_query`` attaches read-only and
    so never migrates it — and ``CREATE TEMP VIEW`` does *not* resolve its body, so a view
    over a missing table is created happily and then fails at query time with a confusing
    ``no such table: campaign.job``. Hence the tables are checked here instead.

    When ``job`` or ``batch`` is absent its columns are selected as NULL rather than dropped,
    so ``run_view`` has the **same column set** on every store version: the caller writes one
    query, and a missing host record (or batch row) reads as NULL instead of as a different
    schema.
    """
    views = {}
    if {"run", "unit"} <= have:
        if "job" in have:
            host = "j.job_dir, j.sysinfo_json"
            join = f"LEFT JOIN {schema}.job j ON r.job_id = j.id"
        else:
            host = "NULL AS job_dir, NULL AS sysinfo_json"
            join = ""
        # Which search round proposed this configuration. LEFT JOIN rather than an inner one
        # even though `unit.batch_id` is NOT NULL: an orphan id would then silently *drop
        # runs*, the failure class this view exists to prevent. NULL reads as "not recorded",
        # the rule the host columns above already follow.
        if "batch" in have:
            batch = "b.idx AS batch"
            bjoin = f"LEFT JOIN {schema}.batch b ON u.batch_id = b.id"
        else:
            batch = "NULL AS batch"
            bjoin = ""
        # A composition-failed unit (a search draw whose parameters could not be
        # realized) has no `run` rows at all, so the join alone drops it -- and with
        # it the only record that the draw was ever attempted. It is added back as a
        # single run-less row: without it a search campaign silently reports itself as
        # if it had only ever proposed the draws that happened to work. It carries
        # `batch` too, so a round whose every draw failed to compose is still a round.
        views["run_view"] = f"""
            SELECT u.config_name, r.run_id, r.status, r.passed, r.duration_s,
                   r.errors, r.failures, r.tests, r.start_time, r.failure_message,
                   u.params_json, u.objective, u.paramset_id, {batch}, {host}
            FROM {schema}.run r
            JOIN {schema}.unit u ON r.unit_id = u.id
            {bjoin}
            {join}
            UNION ALL
            SELECT COALESCE(NULLIF(u.config_name, ''), u.paramset_id) AS config_name,
                   NULL AS run_id, u.status, 0 AS passed, NULL AS duration_s,
                   NULL AS errors, NULL AS failures, NULL AS tests,
                   NULL AS start_time, NULL AS failure_message,
                   u.params_json, u.objective, u.paramset_id, {batch},
                   NULL AS job_dir, NULL AS sysinfo_json
            FROM {schema}.unit u
            {bjoin}
            WHERE u.status = 'composition_failed'
        """
    if "container_failure" in have:
        # What a container died of, expanded to one row per RUN it took down, so the
        # question "which runs were invalidated, and by what?" is one query and joins
        # straight onto run_view on ``config_name || '/' || run_id``.
        #
        # The UNION ALL is the same guard run_view uses for composition_failed units, and
        # for the same reason: ``json_each('[]')`` yields NO rows, so a failure whose runs
        # could not be resolved would vanish entirely from the view -- silently, and
        # exactly in the case where something already went wrong enough that the runner
        # could not name them.
        views["container_failure_view"] = f"""
            SELECT cf.*, je.value AS run_key
            FROM {schema}.container_failure cf, json_each(cf.runs_json) je
            UNION ALL
            SELECT cf.*, NULL AS run_key
            FROM {schema}.container_failure cf
            WHERE cf.runs_json IS NULL OR json_array_length(cf.runs_json) = 0
        """
    if "campaign" in have:
        # The .vast as rows. ``atom`` (not ``value``) is load-bearing: it is NULL for
        # objects and arrays, so a container row cannot return a serialized subtree that
        # _cap_cell would truncate into a config looking complete but is not. A caller
        # descends by fullkey instead, and every row stays small.
        views["config_view"] = f"""
            SELECT t.fullkey, t.key, t.parent, t.type, t.atom AS value
            FROM {schema}.campaign c, json_tree(c.config_json) t
        """
    return views


def _main_view_sql(schema: str, have: set) -> dict:
    """Flat views over the metrics store, keyed by view name.

    One view so far: ``run_validity_view``, which turns the cgroup counters into the
    question a reader of a campaign actually has -- *was this run a clean observation of
    the system under test, or partly a measurement of its CPU quota?*

    It exists because the raw form is a trap in three ways, and every consumer was
    re-deriving it. ``nr_throttled`` and ``nr_periods`` are **monotonic counters**, so a
    ``SUM`` over the tick rows is meaningless and a plain ``MAX`` counts whatever the
    container did before the trial window; the honest reading is a delta within
    ``in_window``. The *ratio* is what carries meaning, not the count -- ``nr_periods = 0``
    means no quota was enforced at all, which is a different fact from a quota that was
    never hit. And the threshold that separates "binding" from "noise" is calibrated
    (:data:`~robovast.results_processing.advice.THROTTLE_WARN_RATIO`) rather than obvious:
    a measured 0.79% cost six runs of fifty, so a reader guessing at 1% would have called
    that campaign clean.

    **It flags, and never filters.** A starved run stays in the results with ``starved =
    1`` beside it, because a run silently dropped is worse than one labelled honestly --
    and because throttling is a *screen*, not a verdict: it says a resource explanation is
    available for a failure, not that the stack misbehaved. Pairing it with the stack's own
    health signals is what makes it conclusive, and those are per-stack and not known here.

    One row per (run, container), not per run: the SUT is the container whose starvation
    invalidates a functional result, but the simulator and scenario are visible in the same
    shape rather than hidden, since a reader comparing them is exactly how one learns that
    a squeezed simulator cost nothing (its realtime factor held) while a squeezed SUT cost
    runs.
    """
    from .advice import THROTTLE_WARN_RATIO  # noqa: PLC0415 - avoids an import cycle

    views = {}
    if "system_usage" in have:
        views["run_validity_view"] = f"""
            WITH per_run AS (
                SELECT config_name, run_id, container,
                       MAX(nr_periods) - MIN(nr_periods) AS periods,
                       MAX(nr_throttled) - MIN(nr_throttled) AS throttled,
                       MAX(throttled_usec) - MIN(throttled_usec) AS throttled_usec
                FROM {schema}.system_usage
                WHERE in_window = 1 AND nr_periods IS NOT NULL
                GROUP BY config_name, run_id, container)
            SELECT config_name, run_id, container, periods, throttled, throttled_usec,
                   CASE WHEN periods > 0
                        THEN CAST(throttled AS REAL) / periods END AS throttle_ratio,
                   CASE WHEN periods > 0
                             AND CAST(throttled AS REAL) / periods >= {THROTTLE_WARN_RATIO}
                        THEN 1 ELSE 0 END AS starved
            FROM per_run
        """
    return views


def _create_campaign_views(conn: sqlite3.Connection, schema: str,
                           prefix: str = "") -> None:
    """Create the flat views over *schema* as ``TEMP`` views on this connection.

    *prefix* namespaces them for an extra attached campaign (``c1_run_view``), since temp
    views share one namespace across the connection.

    A view whose tables are missing is not created at all, so ``describe_data_db`` does not
    list it and a query naming it fails with ``no such table: run_view`` — the honest
    report that this store cannot answer that question.
    """
    for name, body in _campaign_view_sql(schema, _tables_in(conn, schema)).items():
        try:
            conn.execute(f"CREATE TEMP VIEW {prefix}{name} AS {body}")
        except sqlite3.Error as e:
            logger.debug("could not create view %s%s over %s: %s", prefix, name, schema, e)


def _create_main_views(conn: sqlite3.Connection, schema: str, prefix: str = "") -> None:
    """Create the flat views over the metrics store *schema*, same contract as above.

    Split from :func:`_create_campaign_views` because the two stores are attached
    independently: a campaign that has not been postprocessed has no ``main`` tables at all,
    and one whose ``data.db`` is present but whose ``campaign.db`` is not has the reverse.
    Creating both from one call would tie a view over either store to the presence of the
    other, and the missing one is exactly when a reader most needs what is there.
    """
    for name, body in _main_view_sql(schema, _tables_in(conn, schema)).items():
        try:
            conn.execute(f"CREATE TEMP VIEW {prefix}{name} AS {body}")
        except sqlite3.Error as e:
            logger.debug("could not create view %s%s over %s: %s", prefix, name, schema, e)


def _open_db(campaign_dir, extra_dirs: dict | None = None) -> sqlite3.Connection:
    """Open a campaign's queryable databases read-only.

    Prefers ``<campaign_dir>/_execution/data.db`` (the postprocessed metrics). When
    it is absent — postprocessing has not run, or the campaign is still live — falls
    back to an empty in-memory ``main`` so ``campaign.db`` (the live store: config,
    objectives, batch progress) is still reachable rather than raising. Either way
    ``<campaign_dir>/campaign.db`` is attached read-only as schema ``campaign`` when
    present, and ``REGEXP`` + the statistical aggregates are registered.

    *extra_dirs* maps a SQL schema alias → another campaign directory; each such
    campaign's ``data.db`` is attached under that alias (and its ``campaign.db`` as
    ``<alias>_campaign``), so several campaigns can be compared in one query.

    Raises :class:`DataQueryError` only when the primary campaign has neither database.
    """
    campaign_dir = Path(campaign_dir)
    data_db = campaign_dir / "_execution" / "data.db"
    campaign_db = campaign_dir / "campaign.db"

    if data_db.exists():
        conn = sqlite3.connect(f"file:{data_db}?mode=ro", uri=True)
    elif campaign_db.exists():
        # No metrics yet, but the live store is queryable via schema `campaign`.
        conn = sqlite3.connect("file::memory:", uri=True)
    else:
        raise DataQueryError(
            "No data.db or campaign.db found for this campaign. "
            "Run postprocessing first, or start the campaign.")

    conn.row_factory = sqlite3.Row
    conn.create_function("REGEXP", 2, _regexp)
    _register_aggregates(conn)
    if campaign_db.exists():
        _attach_ro(conn, campaign_db, "campaign")
        _create_campaign_views(conn, "campaign")
    # Unconditional: `main` is either the real data.db or the empty in-memory stand-in above,
    # and the view is simply not created when the store has no system_usage table.
    _create_main_views(conn, "main")
    for alias, other in (extra_dirs or {}).items():
        other = Path(other)
        other_data = other / "_execution" / "data.db"
        if other_data.exists():
            _attach_ro(conn, other_data, alias)
            _create_main_views(conn, alias, prefix=f"{alias}_")
        other_campaign = other / "campaign.db"
        if other_campaign.exists():
            _attach_ro(conn, other_campaign, f"{alias}_campaign")
            _create_campaign_views(conn, f"{alias}_campaign", prefix=f"{alias}_")
    return conn


def open_data_db(campaign_dir, extra_dirs: dict | None = None) -> sqlite3.Connection:
    """Open a campaign's queryable databases **read-only** — the public seam.

    Thin public wrapper over the internal opener, for package-provided service endpoints
    (``robovast.service_endpoints``) that read a campaign's ``data.db``/``campaign.db``
    directly (e.g. to serve a postprocessed table untruncated). The caller must ``close()``
    the returned connection (or use :meth:`RunDataContext.open_db`, which does). Raises
    :class:`DataQueryError` when the campaign has neither database.
    """
    return _open_db(campaign_dir, extra_dirs)


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
    ("temp", "run_validity_view"): (
        "WAS THIS RUN A CLEAN OBSERVATION? One row per (run, container) saying whether the "
        "kernel denied it CPU it asked for: config_name, run_id, container, periods, "
        "throttled, throttled_usec, throttle_ratio, starved. Query unqualified: FROM "
        "run_validity_view. Needs postprocessing (it reads system_usage). "
        "Read this INSTEAD of computing deltas over system_usage yourself -- the counters "
        "there are monotonic, so SUM is meaningless and a bare MAX includes whatever "
        "happened before the trial window; this view already takes the in-window delta. "
        "The container that decides validity is 'sut': it is the system under test, so a "
        "run where it was starved cannot separate 'the stack failed' from 'the stack was "
        "cut off mid-plan'. The simulator and scenario are expected to burst and be "
        "clipped, and whether that cost anything is answered by runs.clock_map_* instead. "
        "Clean functional runs: SELECT config_name, run_id FROM run_validity_view WHERE "
        "container='sut' AND starved=0. "
        "How bad, per config: SELECT config_name, MAX(throttle_ratio) FROM "
        "run_validity_view WHERE container='sut' GROUP BY 1. "
        "starved is a SCREEN, not a verdict: it marks runs where a resource explanation is "
        "AVAILABLE for a failure, not runs that failed. Never drop a run because of it -- "
        "report it alongside. Pair it with the stack's own health signals (control-loop "
        "warnings in run_log, nav2_behaviors) to decide whether the clipping cost anything. "
        "periods=0 means no CPU quota was enforced at all, which is not the same as a quota "
        "that was never hit; throttle_ratio is NULL there rather than 0. "
        "COVERAGE IS NOT UNIFORM, so check it before reading a clean result as campaign-wide. "
        "A run appears here only if its node could answer; one that could not is ABSENT, not "
        "starved=0. Absence tracks the NODE, and the node that cannot answer is not a random "
        "one -- measured on this cluster, the single node running an older kernel was also the "
        "largest, so it took the most pods and contributed none of the measurements. "
        "What is missing: SELECT r.node_label, COUNT(*) FROM runs r LEFT JOIN "
        "run_validity_view v ON v.config_name=r.config_name AND v.run_id=r.run_id AND "
        "v.container='sut' WHERE v.run_id IS NULL GROUP BY 1. "
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
        "GROUP BY 1,2. A run's CPU: json_extract(sysinfo_json,'$.cpu_name'). "
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
        "when the path is known, json_extract(campaign.config_json,'$.execution.containers.scenario.image') is "
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
        "How each table in this data.db was produced. One row per step: plugin, output, "
        "table_name (the data.db table it became; NULL when the output was not a CSV that "
        "became a table), sources_json, params_json. "
        "SELECT DISTINCT plugin, params_json FROM postprocessing_steps WHERE "
        "table_name='poses'. Use DISTINCT: a step is recorded once per run. A table with "
        "no row here was produced by a step that recorded no provenance."),
    ("campaign", "job"): (
        "One row per execution job, holding that job's host record. Several runs can share "
        "one job, so this answers 'did these runs run on the same machine?'. "
        "sysinfo_json: json_extract(sysinfo_json,'$.cpu_name'), '$.available_cpus', "
        "'$.platform'. job_dir is campaign-relative. Join campaign.run on job_id — or use "
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
        "params are JSON-encoded — use json_extract/json_each), and the host it ran on "
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
        "(json_extract(execution_json,'$.cluster_info'), '$.env'). These are NULL until "
        "the campaign has executed. Attach another campaign with extra_campaign_ids to ask "
        "whether two runs used the same image. "
        "stop_kind/stop_reason/batches explain why a search terminated. strategy_state is "
        "an opaque BLOB (masked in results). "
        "config_json is the whole .vast: json_extract(config_json,'$.execution.containers.scenario.image') for "
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
        "place a starved run says so — throttling does not fail a run, it just makes it "
        "slower, so its results quietly become partly a measurement of the allocation "
        "rather than of the system under test. "
        "They are MONOTONIC COUNTERS, so read a delta (MAX-MIN) or the last value, never a "
        "SUM. nr_periods=0 means no CPU quota was enforced at all, which is different from "
        "a quota that was never hit — read the ratio nr_throttled/nr_periods, not the raw "
        "count. Was this run starved: SELECT container, MAX(nr_throttled)-MIN(nr_throttled) "
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
    "ordering it is lexicographic ('10.022' < '9.5'), so CAST(col AS REAL) first, and "
    "note that a data.db built before typed ingest has TEXT everywhere (rerun "
    "postprocessing to retype it). A table's 'column_notes' flags a column whose type "
    "does not tell the whole story — read it before aggregating that column. "
    "Extra aggregate functions are available beyond SQLite's built-ins: STDDEV, VARIANCE, "
    "MEDIAN, and PERCENTILE(col, p) where p is 0..100. REGEXP(pattern, col) and SQRT(x) "
    "are also registered — SQRT is always present here, whereas SQLite's own is a "
    "compile-time option, so use it for distances rather than assuming."
)


#: Internal bookkeeping tables in ``data.db``: not results, so not listed as tables —
#: ``_column_notes`` is folded into the owning table's entry instead.
_INTERNAL_TABLES = ("_table_name_map", "_column_notes")


def _column_notes(conn: sqlite3.Connection, schema: str) -> dict:
    """``{table: {column: note}}`` from *schema*'s ``_column_notes``.

    Caveats the declared type cannot carry — currently a column that is numeric in
    some runs and text in others, where an aggregate silently reads the text rows as
    0. Absent in a ``data.db`` built before typed ingest, which is not an error.
    """
    notes: dict[str, dict[str, str]] = {}
    try:
        rows = conn.execute(
            f"SELECT table_name, column_name, note FROM {schema}._column_notes").fetchall()
    except sqlite3.Error:
        return notes
    for r in rows:
        notes.setdefault(r["table_name"], {})[r["column_name"]] = r["note"]
    return notes


def _list_campaign_views(conn: sqlite3.Connection) -> list[dict]:
    """The flat views (:func:`_create_campaign_views`), listed **first**.

    They come first deliberately: they are where a caller should start, and a schema dump
    is read top-down. Reported with schema ``temp`` because that is where they live and
    what makes ``temp.run_view`` a valid name — they are also reachable unqualified as
    ``run_view``, which the descriptions say.
    """
    views = []
    try:
        rows = conn.execute(
            "SELECT name FROM temp.sqlite_master WHERE type='view' ORDER BY name").fetchall()
    except sqlite3.Error:
        return views
    # Listing order: run_view before config_view, extra-campaign aliases after both.
    def _order(name: str) -> tuple:
        for i, base in enumerate(_CAMPAIGN_VIEW_NAMES + _MAIN_VIEW_NAMES):
            if name == base:
                return (0, i, name)
            if name.endswith(f"_{base}"):
                return (1, i, name)
        return (2, 0, name)

    for r in sorted((r["name"] for r in rows), key=_order):
        cols = [f'{c["name"]} {c["type"]}'.strip()
                for c in conn.execute(f'PRAGMA table_info("{r}")').fetchall()]
        try:
            n = conn.execute(f'SELECT COUNT(*) FROM temp."{r}"').fetchone()[0]
        except sqlite3.Error:
            n = None
        entry = {"schema": "temp", "table": r, "columns": cols, "rows": n}
        desc = _TABLE_DESCRIPTIONS.get(("temp", r))
        if desc:
            entry["description"] = desc
        views.append(entry)
    return views


def _list_tables(conn: sqlite3.Connection) -> list[dict]:
    """Return ``[{schema, table, columns, rows, description}]`` across attached DBs.

    Each ``columns`` entry is ``"name TYPE"`` (bare ``"name"`` when the column was
    declared without a type). A table with recorded caveats also carries
    ``column_notes`` (see :func:`_column_notes`).
    """
    tables = _list_campaign_views(conn)
    # Every attached schema except `temp`, whose views are listed above; keep
    # `main`/`campaign` first for readability, then extra-campaign aliases in attach order.
    names = [r["name"] for r in conn.execute("PRAGMA database_list").fetchall()
             if r["name"] != "temp"]
    ordered = [s for s in ("main", "campaign") if s in names]
    schemas = ordered + [s for s in names if s not in ordered]
    for schema in schemas:
        internal = ", ".join(f"'{t}'" for t in _INTERNAL_TABLES)
        rows = conn.execute(
            f"SELECT name FROM {schema}.sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            f"AND name NOT IN ({internal}) ORDER BY name").fetchall()
        notes = _column_notes(conn, schema)
        for tr in rows:
            name = tr["name"]
            # "name TYPE" per column: the type is what tells a caller whether a
            # column can be compared/ordered directly or is text needing a CAST.
            cols = [f'{r["name"]} {r["type"]}'.strip() for r in conn.execute(
                f'PRAGMA {schema}.table_info("{name}")').fetchall()]
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM {schema}."{name}"').fetchone()[0]
            except sqlite3.Error:
                n = None
            entry = {"schema": schema, "table": name, "columns": cols, "rows": n}
            desc = _TABLE_DESCRIPTIONS.get((schema, name))
            if desc:
                entry["description"] = desc
            if name in notes:
                entry["column_notes"] = notes[name]
            tables.append(entry)
    return tables


def describe_data_db(campaign_dir) -> dict:
    """Return ``{tables: [{schema, table, columns, rows, description}], note}``.

    Works before postprocessing: when ``data.db`` is absent, the attached
    ``campaign`` schema (config/objectives/batch progress) is still described.
    """
    conn = _open_db(campaign_dir)
    try:
        return {"tables": _list_tables(conn), "note": _DESCRIBE_NOTE}
    finally:
        conn.close()


def _cap_cell(value):
    """Bound a single cell's width. BLOBs are masked; oversized text is truncated."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<BLOB {len(bytes(value))} bytes>"
    if isinstance(value, str) and len(value.encode("utf-8", "replace")) > _MAX_CELL_BYTES:
        return value.encode("utf-8", "replace")[:_MAX_CELL_BYTES].decode(
            "utf-8", "ignore") + f"…<truncated, {len(value)} chars total>"
    return value


def _empty_result_note(conn: sqlite3.Connection) -> str:
    """Explain a 0-row result: list non-empty base tables so a broken JOIN/filter
    is distinguishable from a genuinely empty dataset."""
    non_empty = []
    for t in _list_tables(conn):
        if t.get("rows"):
            qualified = t["table"] if t["schema"] == "main" else f'{t["schema"]}.{t["table"]}'
            non_empty.append(f"{qualified}: {t['rows']}")
    if not non_empty:
        return "Query matched 0 rows, and no base table has any rows."
    return (
        "Query matched 0 rows. This may be correct, or a filter/JOIN-key mismatch. "
        "Non-empty tables (rows): " + ", ".join(non_empty) + "."
    )


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


def query_data_db(campaign_dir, sql: str, max_rows: int = 500,
                  extra_dirs: dict | None = None,
                  max_bytes: int | None = None) -> dict:
    """Run a read-only ``SELECT``; return ``{columns, rows, row_count, truncated}``.

    *extra_dirs* (schema alias → campaign dir) attaches further campaigns so one
    query can span several (e.g. an A/B comparison); see :func:`_open_db`.

    *max_bytes* overrides :data:`_MAX_RESULT_BYTES`. That default is sized for a caller
    who has to *read* the reply into a context window; a caller that renders it — the run
    view's panels, the data browser's table — is bounded by a browser rather than by a
    token budget, and clamping it to 16k tokens truncates a chart at ~120 rows of ``poses``
    while the row cap it reports still says 5000. Callers who plot ask for more.

    Raises :class:`DataQueryError` for a rejected (non-read) or invalid query.
    """
    conn = _open_db(campaign_dir, extra_dirs=extra_dirs)
    max_rows = max(1, min(int(max_rows), 5000))
    max_bytes = _MAX_RESULT_BYTES if max_bytes is None else max(1024, int(max_bytes))
    try:
        conn.set_authorizer(_readonly_authorizer)
        try:
            cursor = conn.execute(sql)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "not authorized" in msg.lower():
                raise DataQueryError(
                    f"Only read-only SELECT queries are allowed (rejected: {msg}).") from e
            raise DataQueryError(f"SQL error: {msg}") from e
        if cursor.description is None:
            raise DataQueryError("query returned no result set (only SELECT is supported)")
        columns = [d[0] for d in cursor.description]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [{c: _cap_cell(v) for c, v in zip(columns, r)}
                for r in fetched[:max_rows]]
        rows, size_capped = _cap_result_size(rows, max_bytes)
        result = {"columns": columns, "row_count": len(rows),
                  "truncated": truncated or size_capped, "rows": rows}
        if size_capped:
            result["note"] = (
                f"stopped at {len(rows)} rows: the reply reached the "
                f"{max_bytes // 1024} KB ceiling. Rows are capped separately from "
                f"size, and a wide table reaches this long before max_rows. Aggregate in "
                f"SQL (COUNT/AVG/MIN/MAX, GROUP BY) or select the columns you need — or "
                f"export the whole result as CSV instead of reading it here.")
        if not rows:
            conn.set_authorizer(None)  # _empty_result_note runs its own COUNT(*)s
            result["note"] = _empty_result_note(conn)
        return result
    finally:
        conn.set_authorizer(None)
        conn.close()


def stream_query_csv(campaign_dir, sql: str, extra_dirs: dict | None = None):
    """Yield the same ``SELECT`` as CSV text, row by row and with **no row cap**.

    :func:`query_data_db` clamps to 5000 rows because its result is a JSON payload someone
    has to hold — reasonable for a caller reading the answer, useless for a caller who
    wants the data. This is the way out: the HTTP layer streams it, so a result larger
    than memory is fine at both ends, and an MCP tool can hand over the URL instead of
    reporting ``truncated`` and leaving the rest unreachable.

    Same authorizer, so it is exactly as read-only as the JSON path — a second query entry
    point must not be a second security decision. Cells are **not** width-capped here: the
    cap exists to keep a JSON reply readable, and truncating an exported value would
    corrupt the export.
    """
    import csv
    import io

    conn = _open_db(campaign_dir, extra_dirs=extra_dirs)
    try:
        conn.set_authorizer(_readonly_authorizer)
        try:
            cursor = conn.execute(sql)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "not authorized" in msg.lower():
                raise DataQueryError(
                    f"Only read-only SELECT queries are allowed (rejected: {msg}).") from e
            raise DataQueryError(f"SQL error: {msg}") from e
        if cursor.description is None:
            raise DataQueryError("query returned no result set (only SELECT is supported)")

        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def _flush() -> str:
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return text

        writer.writerow([d[0] for d in cursor.description])
        yield _flush()
        while True:
            batch = cursor.fetchmany(1000)
            if not batch:
                return
            writer.writerows(batch)
            yield _flush()
    finally:
        conn.set_authorizer(None)
        conn.close()


# NOTE: the costmap-frame reader lived here; it moved to ``robovast_nav`` as a
# package-provided service endpoint (``robovast_nav/service_endpoints.py:CostmapEndpoint``),
# which reads the ``costmaps`` table via :func:`open_data_db`. Core keeps only the generic
# read-only opener above.
