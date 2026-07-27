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
parameterised by the campaign **directory** so it serves both callers:

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


def _register_aggregates(conn: sqlite3.Connection) -> None:
    conn.create_aggregate("STDDEV", 1, _Stddev)
    conn.create_aggregate("VARIANCE", 1, _Variance)
    conn.create_aggregate("MEDIAN", 1, _Median)
    conn.create_aggregate("PERCENTILE", 2, _Percentile)


def _attach_ro(conn: sqlite3.Connection, db_path: Path, alias: str) -> None:
    """Attach *db_path* read-only under *alias*, best-effort (logs on failure)."""
    try:
        conn.execute(f'ATTACH DATABASE ? AS "{alias}"', (f"file:{db_path}?mode=ro",))
    except sqlite3.Error as e:
        logger.debug("could not attach %s as %s: %s", db_path, alias, e)


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
    for alias, other in (extra_dirs or {}).items():
        other = Path(other)
        other_data = other / "_execution" / "data.db"
        if other_data.exists():
            _attach_ro(conn, other_data, alias)
        other_campaign = other / "campaign.db"
        if other_campaign.exists():
            _attach_ro(conn, other_campaign, f"{alias}_campaign")
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


# Human-readable meaning for the non-obvious tables an LLM will otherwise see as
# bare names. Metric tables (one per CSV stem) are self-describing by their columns.
_TABLE_DESCRIPTIONS = {
    ("main", "runs"): (
        "Per-run dimension table: status/passed/duration_s/errors/failures, the "
        "scalar objective, and each scenario parameter as a param_* column "
        "(non-scalar params are JSON-encoded — use json_extract/json_each). Join to "
        "any metric table on (config_name, run_id)."),
    ("campaign", "campaign"): (
        "One row for the campaign. config_json holds the entire .vast (use "
        "json_extract, e.g. json_extract(config_json,'$.evaluation')); stop_kind/"
        "stop_reason/batches/elapsed_s explain why/when a search terminated. "
        "strategy_state is an opaque BLOB (masked in query results)."),
    ("campaign", "batch"): (
        "One row per search batch/iteration; idx is the iteration index — the "
        "search history over time."),
    ("campaign", "unit"): (
        "One row per evaluated configuration. objectives_json (all named "
        "objectives) and measures_json (quality-diversity measures) live ONLY here "
        "— runs.objective lifts just the single scalar objective. params_json holds "
        "the config's scenario parameters; n_samples/status are roll-ups of its "
        "'run' rows (join campaign.run on unit_id for the per-run breakdown)."),
    ("campaign", "run"): (
        "One row per individual run (repetition), child of unit via unit_id — the "
        "operational source of truth for run outcomes, written live during execution "
        "from each run's test.xml. status is passed/failed/error/unknown (unknown = "
        "test.xml missing or unparseable), passed is 0/1, with errors/failures/tests/"
        "duration_s/start_time/failure_message. run_id is the numeric run index within "
        "the config. Prefer this over walking test.xml: pass/fail counts are one "
        "GROUP BY status here, available even before postprocessing builds data.db. "
        "The main.runs table is the postprocessed wide VIEW over these rows (joining "
        "sysinfo + exploding params)."),
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
}

_DESCRIBE_NOTE = (
    "Join the 'runs' table (param_* columns + status/duration) to any metric table "
    "on (config_name, run_id). campaign.db is attached as schema 'campaign' (see "
    "the campaign/batch/unit/run table descriptions). For raw per-run pass/fail, "
    "campaign.run is the source of truth (main.runs is its postprocessed view). "
    "Each column is listed as 'name TYPE': numeric CSV columns are stored as "
    "INTEGER/REAL, so compare and ORDER BY them directly. A TEXT column holds text — "
    "ordering it is lexicographic ('10.022' < '9.5'), so CAST(col AS REAL) first, and "
    "note that a data.db built before typed ingest has TEXT everywhere (rerun "
    "postprocessing to retype it). A table's 'column_notes' flags a column whose type "
    "does not tell the whole story — read it before aggregating that column. "
    "Extra aggregate functions are "
    "available beyond SQLite's built-ins: STDDEV, VARIANCE, MEDIAN, and "
    "PERCENTILE(col, p) where p is 0..100. A REGEXP(pattern, col) function is also "
    "registered."
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


def _list_tables(conn: sqlite3.Connection) -> list[dict]:
    """Return ``[{schema, table, columns, rows, description}]`` across attached DBs.

    Each ``columns`` entry is ``"name TYPE"`` (bare ``"name"`` when the column was
    declared without a type). A table with recorded caveats also carries
    ``column_notes`` (see :func:`_column_notes`).
    """
    tables = []
    # Every attached schema except the transient `temp` one; keep `main`/`campaign`
    # first for readability, then any extra-campaign aliases in attach order.
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


def query_data_db(campaign_dir, sql: str, max_rows: int = 500,
                  extra_dirs: dict | None = None) -> dict:
    """Run a read-only ``SELECT``; return ``{columns, rows, row_count, truncated}``.

    *extra_dirs* (schema alias → campaign dir) attaches further campaigns so one
    query can span several (e.g. an A/B comparison); see :func:`_open_db`.

    Raises :class:`DataQueryError` for a rejected (non-read) or invalid query.
    """
    conn = _open_db(campaign_dir, extra_dirs=extra_dirs)
    max_rows = max(1, min(int(max_rows), 5000))
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
        result = {"columns": columns, "row_count": len(rows),
                  "truncated": truncated, "rows": rows}
        if not rows:
            conn.set_authorizer(None)  # _empty_result_note runs its own COUNT(*)s
            result["note"] = _empty_result_note(conn)
        return result
    finally:
        conn.set_authorizer(None)
        conn.close()


# NOTE: the costmap-frame reader lived here; it moved to ``robovast_nav`` as a
# package-provided service endpoint (``robovast_nav/service_endpoints.py:CostmapEndpoint``),
# which reads the ``costmaps`` table via :func:`open_data_db`. Core keeps only the generic
# read-only opener above.
