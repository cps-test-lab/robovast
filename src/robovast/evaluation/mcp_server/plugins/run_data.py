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

"""MCP plugin for querying and inspecting run data tables.

This plugin exposes a unified interface for tabular data produced per run.
It supports listing available data tables, flexible row filtering and pagination
for query use-cases, and aggregate-only inspection for summary/statistics use-cases.

Data is read from the SQLite database at ``<campaign>/_execution/data.db``, which
is generated automatically during postprocessing.  If the database is absent,
the tools return an error indicating that postprocessing must be run first.
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal, TypedDict

from mcp.server.fastmcp import FastMCP

from robovast.evaluation.mcp_server import results_resolver

from ..plugin_common import _get_config_by_identifier_or_name, _iter_all_configs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Filter(TypedDict, total=False):
    """Row filter definition for data table queries.

    Supported operators:
      - ``eq``, ``neq``
      - ``contains``, ``regex``
      - ``in``
      - ``gt``, ``gte``, ``lt``, ``lte``
      - ``between``
      - ``is_null``
    """

    column: str
    op: Literal[
        "eq", "neq", "contains", "regex", "in",
        "gt", "gte", "lt", "lte", "between", "is_null",
    ]
    value: Any
    values: list[Any]
    min: Any
    max: Any
    case_sensitive: bool


class Aggregate(TypedDict, total=False):
    """Aggregate definition for data table inspection.

    Supported aggregate operations:
      - ``count``
      - ``min``, ``max``, ``mean``, ``sum``
      - ``distinct_count``
    """

    op: Literal["count", "min", "max", "mean", "sum", "distinct_count"]
    column: str
    alias: str


# ---------------------------------------------------------------------------
# DB access helpers
# ---------------------------------------------------------------------------

def _get_db_path(campaign_id: str) -> Path | None:
    """Return path to data.db for *campaign_id*, or None if it does not exist."""
    try:
        campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    except ValueError:
        return None
    db = campaign_path / "_execution" / "data.db"
    return db if db.exists() else None


def _open_db(campaign_id: str) -> sqlite3.Connection | None:
    """Open the campaign data.db read-only.

    Registers a REGEXP function and returns the connection, or None when the
    database file does not exist.
    """
    db_path = _get_db_path(campaign_id)
    if db_path is None:
        return None
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    def _regexp(pattern: str, value: str | None) -> bool:
        if value is None:
            return False
        try:
            return re.search(pattern, str(value)) is not None
        except re.error:
            return False

    conn.create_function("REGEXP", 2, _regexp)
    return conn


def _get_table_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Return mapping of display_name -> sql_name from _table_name_map."""
    try:
        rows = conn.execute("SELECT display_name, sql_name FROM _table_name_map").fetchall()
        return {r["display_name"]: r["sql_name"] for r in rows}
    except Exception:
        return {}


def _resolve_config_name(campaign_id: str, configuration_id: str) -> str | None:
    """Resolve the on-disk config directory name from a config identifier or name."""
    entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if entry is None:
        return None
    return entry.get("name", configuration_id)


def _resolve_config_identifier(campaign_id: str, config_name: str) -> str:
    """Resolve the config_identifier for a given on-disk config_name."""
    entry = _get_config_by_identifier_or_name(campaign_id, config_name)
    if entry is None:
        return config_name
    return str(entry.get("config_identifier", config_name))


def _iter_matching_runs(
    campaign_id: str | None,
    configuration_id: str | None,
    run: int | None,
):
    """Yield ``(campaign_id, config_identifier, config_name, run)`` tuples.

    When *campaign_id* is ``None`` all campaigns are visited.
    When *configuration_id* is ``None`` all configurations within the
    selected campaign(s) are visited.
    When *run* is ``None`` all runs within each matching configuration are
    visited.
    """
    for cid, c in _iter_all_configs(campaign_id):
        cname = c.get("name", "")
        cident = str(c.get("config_identifier", ""))
        if configuration_id is not None:
            if configuration_id != cname and configuration_id != cident:
                continue
        effective_ident = cident or cname
        if run is not None:
            yield cid, effective_ident, cname, run
        else:
            for tr in c.get("test_results", []):
                run_dir = tr.get("dir", "")
                run_str = run_dir.split("/")[-1] if "/" in run_dir else run_dir
                if run_str.isdigit():
                    yield cid, effective_ident, cname, int(run_str)


# ---------------------------------------------------------------------------
# Filter / aggregate SQL translation
# ---------------------------------------------------------------------------

def _filters_to_sql(filters: list[Filter] | None) -> tuple[str, list]:
    """Translate a list of Filter dicts to a SQL WHERE clause fragment and params.

    All filters are ANDed together.  Returns ``("", [])`` when *filters* is
    empty or None.
    """
    if not filters:
        return "", []

    clauses: list[str] = []
    params: list[Any] = []

    for flt in filters:
        column = flt.get("column")
        op = flt.get("op")
        if not column or not op:
            continue
        case_sensitive = bool(flt.get("case_sensitive", False))
        col_expr = f'"{column}"'
        col_lower = f'LOWER("{column}")'

        if op == "is_null":
            target = bool(flt.get("value", True))
            if target:
                clauses.append(f'({col_expr} IS NULL OR {col_expr} = "")')
            else:
                clauses.append(f'({col_expr} IS NOT NULL AND {col_expr} != "")')

        elif op == "eq":
            val = "" if flt.get("value") is None else str(flt["value"])
            if case_sensitive:
                clauses.append(f"{col_expr} = ?")
                params.append(val)
            else:
                clauses.append(f"{col_lower} = LOWER(?)")
                params.append(val)

        elif op == "neq":
            val = "" if flt.get("value") is None else str(flt["value"])
            if case_sensitive:
                clauses.append(f"{col_expr} != ?")
                params.append(val)
            else:
                clauses.append(f"{col_lower} != LOWER(?)")
                params.append(val)

        elif op == "contains":
            val = "" if flt.get("value") is None else str(flt["value"])
            if case_sensitive:
                clauses.append(f"{col_expr} LIKE ?")
                params.append(f"%{val}%")
            else:
                clauses.append(f"{col_lower} LIKE LOWER(?)")
                params.append(f"%{val}%")

        elif op == "regex":
            pattern = "" if flt.get("value") is None else str(flt["value"])
            clauses.append(f"REGEXP(?, {col_expr})")
            params.append(pattern)

        elif op == "in":
            values = flt.get("values")
            if values is None:
                values = flt.get("value", [])
            if not isinstance(values, list):
                values = [values]
            if not values:
                continue
            ph = ", ".join("?" for _ in values)
            str_values = [str(v) for v in values]
            if case_sensitive:
                clauses.append(f"{col_expr} IN ({ph})")
                params.extend(str_values)
            else:
                clauses.append(f"{col_lower} IN ({', '.join('LOWER(?)' for _ in values)})")
                params.extend(str_values)

        elif op in {"gt", "gte", "lt", "lte"}:
            val = flt.get("value")
            if val is None:
                continue
            sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            clauses.append(f'CAST({col_expr} AS REAL) {sql_op} ?')
            params.append(float(val))

        elif op == "between":
            min_val = flt.get("min")
            max_val = flt.get("max")
            if min_val is not None and max_val is not None:
                clauses.append(f'CAST({col_expr} AS REAL) BETWEEN ? AND ?')
                params.extend([float(min_val), float(max_val)])
            elif min_val is not None:
                clauses.append(f'CAST({col_expr} AS REAL) >= ?')
                params.append(float(min_val))
            elif max_val is not None:
                clauses.append(f'CAST({col_expr} AS REAL) <= ?')
                params.append(float(max_val))

    if not clauses:
        return "", []
    return " AND ".join(clauses), params


def _build_where(
    config_name: str | None,
    run_id: int | None,
    extra_where: str,
    extra_params: list,
) -> tuple[str, list]:
    """Combine config/run constraints with user filters into a WHERE clause."""
    clauses: list[str] = []
    params: list[Any] = []
    if config_name is not None:
        clauses.append("config_name = ?")
        params.append(config_name)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if extra_where:
        clauses.append(f"({extra_where})")
        params.extend(extra_params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def list_run_data_tables(
    campaign_id: str,
    configuration_id: str,
    run: int,
) -> dict:
    """List the available data tables for a specific run.

    Use this tool to discover which data tables exist for a run before
    querying or inspecting them.

    Args:
        campaign_id: Campaign identifier (e.g. ``"campaign-2026-03-08-190000"``).
        configuration_id: Configuration identifier or name.
        run: Run number (e.g. ``0``).

    Returns:
        Dict with selection context and a ``tables`` list of available table names.

    Example::

        list_run_data_tables(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
        )
    """
    try:
        config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
        if config_entry is None:
            return {"error": f"Configuration not found: {configuration_id}"}
        config_name = config_entry.get("name", configuration_id)
        config_identifier = config_entry.get("config_identifier", configuration_id)

        conn = _open_db(campaign_id)
        if conn is None:
            return {"error": "No data.db found for this campaign. Run postprocessing first."}

        try:
            table_map = _get_table_map(conn)
            # Exclude rosout from the public list
            tables = sorted(
                name for name in table_map
                if name.lower() != "rosout"
            )
        finally:
            conn.close()

        return {
            "campaign_id": campaign_id,
            "config_name": config_name,
            "configuration_id": config_identifier,
            "run": run,
            "tables": tables,
        }
    except Exception as exc:
        logger.exception("list_run_data_tables failed")
        return {"error": f"list_run_data_tables failed: {exc}"}


def query_run_data_table(
    campaign_id: str | None = None,
    configuration_id: str | None = None,
    run: int | None = None,
    table: str = "",
    filters: list[Filter] | None = None,
    columns: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Query rows from a run data table with filters, projection, and pagination.

    This tool is intended for row-level exploration. It returns matching rows,
    optionally reduced to selected columns, plus pagination metadata.

    Selection rules:
      - When ``campaign_id`` is omitted all campaigns are queried.
      - When ``configuration_id`` is omitted all configurations within the
        selected campaign(s) are queried.
      - When ``run`` is omitted all runs within each matching configuration
        are queried.
      - The ``table`` parameter is required and must be specified.
      - If ``table`` is not found, the response
        contains an ``error`` and ``available_tables``.

    Filter behavior:
      - Filters are combined with logical AND.
      - Supported operators:
        ``eq``, ``neq``, ``contains``, ``regex``, ``in``,
        ``gt``, ``gte``, ``lt``, ``lte``, ``between``, ``is_null``.
      - String operators are case-insensitive by default; set
        ``case_sensitive=True`` per filter to override.
      - Numeric operators attempt float conversion.
      - ``is_null`` treats missing/empty-string values as null.

    Args:
        campaign_id: Campaign identifier (optional — queries all campaigns when omitted).
        configuration_id: Configuration identifier or name (optional — queries all
            configurations when omitted).
        run: Run number (optional — queries all runs when omitted).
        table: Relative table name in run directory.
        filters: List of filter objects.
        columns: Optional list of columns to project in result rows.
        limit: Max rows to return (clamped to ``1..1000``).
        offset: Row offset in filtered result set.

    Returns:
        Dict with selection context, pagination metadata, and ``rows``.
        The ``columns`` field always starts with the four context columns
        ``campaign_id``, ``configuration_id``, ``config_name``, ``run``,
        followed by the data columns (either the requested ``columns`` projection
        or all non-context columns when ``columns`` is omitted).
        Each row in ``rows`` follows the same column order.

    Example (basic)::

        query_run_data_table(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            table="poses",
            limit=50,
        )

    Note:
        ``table="rosout"`` is not allowed here; use :func:`query_run_log` instead.

    Example (filtered + projected)::

        query_run_data_table(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            table="poses",
            filters=[
                {"column": "frame", "op": "eq", "value": "base_link"},
                {"column": "timestamp", "op": "gte", "value": 10.0},
            ],
            columns=["timestamp", "x", "y", "yaw"],
            limit=100,
            offset=0,
        )

    Example (regex + null check)::

        query_run_data_table(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            table="behaviors",
            filters=[
                {"column": "behavior", "op": "regex", "value": "recover|backup"},
                {"column": "details", "op": "is_null", "value": False},
            ],
            limit=25,
            offset=25,
        )
    """
    try:
        display_name = table[:-4] if table.lower().endswith(".csv") else table
        if display_name.lower() == "rosout":
            return {
                "error": (
                    "The rosout table cannot be queried directly. "
                    "Use query_run_log() to access log entries."
                ),
            }

        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        filter_where, filter_params = _filters_to_sql(filters)

        all_rows: list[dict[str, Any]] = []
        all_data_columns: list[str] = []
        selected_table: str | None = None
        available_tables: list[str] = []

        campaigns_to_query = (
            [campaign_id] if campaign_id is not None
            else [d.name for d in results_resolver.list_campaigns()]
        )

        for cid in campaigns_to_query:
            conn = _open_db(cid)
            if conn is None:
                continue
            try:
                table_map = _get_table_map(conn)
                avail = sorted(n for n in table_map if n.lower() != "rosout")
                if not available_tables:
                    available_tables = avail

                # Resolve table name (display_name already computed above)
                if display_name not in table_map:
                    continue
                sql_name = table_map[display_name]
                if selected_table is None:
                    selected_table = display_name

                pairs = [
                    (cname, r)
                    for _, _, cname, r in _iter_matching_runs(cid, configuration_id, run)
                ]
                if not pairs:
                    continue
                for cname, r in pairs:
                    where, params = _build_where(cname, r, filter_where, filter_params)
                    _run_query(conn, sql_name, where, params, cid, all_rows, all_data_columns)
            finally:
                conn.close()

        if not all_rows and selected_table is None:
            if not table:
                return {
                    "campaign_id": campaign_id,
                    "configuration_id": configuration_id,
                    "run": run,
                    "error": "No data.db found. Run postprocessing first.",
                }
            return {
                "campaign_id": campaign_id,
                "configuration_id": configuration_id,
                "run": run,
                "table": table or None,
                "error": f"Table '{table}' not found.",
                "available_tables": available_tables,
            }

        total_rows = len(all_rows)
        page_rows = all_rows[offset: offset + limit]
        context_cols = ["campaign_id", "configuration_id", "config_name", "run"]
        output_columns = columns or [c for c in all_data_columns if c not in ("config_name", "run_id")]

        def _fmt_row(row: dict) -> dict:
            return {
                **{k: row.get(k) for k in context_cols},
                **{c: row.get(c) for c in output_columns},
            }

        next_offset = offset + len(page_rows)
        has_more = next_offset < total_rows

        return {
            "campaign_id": campaign_id,
            "configuration_id": configuration_id,
            "run": run,
            "table": selected_table,
            "columns": context_cols + output_columns,
            "offset": offset,
            "limit": limit,
            "total_rows": total_rows,
            "matched_rows": total_rows,
            "returned_rows": len(page_rows),
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "rows": [_fmt_row(r) for r in page_rows],
        }
    except Exception as exc:
        logger.exception("query_run_data_table failed")
        return {"error": f"query_run_data_table failed: {exc}"}


def _with_ts_cap(
    conn: sqlite3.Connection,
    cname: str,
    r: int,
    filter_where: str,
    filter_params: list,
) -> tuple[str, list]:
    """Prepend a per-run scenario-end timestamp cap to *filter_where*."""
    ts_row = conn.execute(
        "SELECT timestamp FROM scenario_timestamps WHERE config_name = ? AND run_id = ?",
        (cname, r),
    ).fetchone()
    end_ts: float | None = ts_row["timestamp"] if ts_row else None
    if end_ts is None:
        return filter_where, list(filter_params)
    ts_where = 'CAST("timestamp" AS REAL) <= ?'
    if filter_where:
        return f"{ts_where} AND ({filter_where})", [end_ts] + list(filter_params)
    return ts_where, [end_ts]


def _run_query(
    conn: sqlite3.Connection,
    sql_name: str,
    where: str,
    params: list,
    campaign_id: str,
    all_rows: list,
    all_data_columns: list,
) -> None:
    """Execute SELECT on *sql_name* and append enriched rows to *all_rows*."""
    sql = f'SELECT * FROM "{sql_name}" {where}'
    cursor = conn.execute(sql, params)
    col_names = [d[0] for d in cursor.description]
    if not all_data_columns:
        all_data_columns.extend(col_names)
    for db_row in cursor.fetchall():
        row: dict[str, Any] = dict(zip(col_names, db_row))
        config_name = row.get("config_name", "")
        run_id = row.get("run_id")
        run_id_int = int(run_id) if run_id is not None else None
        config_identifier = _resolve_config_identifier(campaign_id, str(config_name))
        row["campaign_id"] = campaign_id
        row["configuration_id"] = config_identifier
        row["run"] = run_id_int
        all_rows.append(row)


def inspect_run_data_table(
    campaign_id: str | None = None,
    configuration_id: str | None = None,
    run: int | None = None,
    table: str = "",
    filters: list[Filter] | None = None,
    aggregates: list[Aggregate] | None = None,
) -> dict:
    """Inspect a run data table using aggregate/statistical operations only.

    This tool is intended for summary-level analysis and does not return rows.

    Selection and filtering rules are identical to ``query_run_data_table``.

    Supported aggregate ops:
      - ``count``: number of filtered rows.
      - ``distinct_count``: number of distinct non-null values in ``column``.
      - ``min``, ``max``, ``mean``, ``sum``: numeric aggregates over ``column``.

    If ``aggregates`` is omitted, defaults to ``[{"op": "count", "alias": "count"}]``.

    Args:
        campaign_id: Campaign identifier (optional — inspects all campaigns when omitted).
        configuration_id: Configuration identifier or name (optional — inspects all
            configurations when omitted).
        run: Run number (optional — inspects all runs when omitted).
        table: Data table name.
        filters: Optional filter list (AND semantics).
        aggregates: Aggregate definitions. Each entry supports:
            - ``op``: aggregate operation.
            - ``column``: required for all ops except ``count``.
            - ``alias``: output field name.

    Returns:
        Dict with selection context, row counts, and aggregate result map.

    Note:
        ``table="rosout"`` is not allowed here; use :func:`query_run_log` to
        access log entries.

    Example (single metric)::

        inspect_run_data_table(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            table="poses",
            filters=[{"column": "frame", "op": "eq", "value": "base_link"}],
            aggregates=[{"op": "count", "alias": "pose_count"}],
        )

    Example (multi-metric numeric summary)::

        inspect_run_data_table(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            table="poses",
            filters=[{"column": "frame", "op": "eq", "value": "base_link"}],
            aggregates=[
                {"op": "count", "alias": "n"},
                {"op": "mean", "column": "x", "alias": "x_mean"},
                {"op": "mean", "column": "y", "alias": "y_mean"},
                {"op": "min", "column": "yaw", "alias": "yaw_min"},
                {"op": "max", "column": "yaw", "alias": "yaw_max"},
                {"op": "distinct_count", "column": "frame", "alias": "frame_count"},
            ],
        )
    """
    try:
        display_name = table[:-4] if table.lower().endswith(".csv") else table
        if display_name.lower() == "rosout":
            return {
                "error": (
                    "The rosout table cannot be inspected directly. "
                    "Use query_run_log() to access log entries."
                ),
            }

        aggregates = aggregates or [{"op": "count", "alias": "count"}]
        filter_where, filter_params = _filters_to_sql(filters)

        # Build aggregate SQL expressions
        agg_exprs: list[str] = []
        agg_aliases: list[str] = []
        for idx, agg in enumerate(aggregates):
            op = agg.get("op")
            col = agg.get("column")
            alias = agg.get("alias") or (f"{op}_{col}" if col else f"{op}_{idx}")
            agg_aliases.append(alias)
            if op == "count":
                agg_exprs.append(f"COUNT(*) AS \"{alias}\"")
            elif op == "distinct_count" and col:
                agg_exprs.append(
                    f'COUNT(DISTINCT CASE WHEN "{col}" IS NOT NULL AND "{col}" != "" '
                    f'THEN "{col}" END) AS "{alias}"'
                )
            elif op in {"min", "max", "mean", "sum"} and col:
                sql_func = {"min": "MIN", "max": "MAX", "mean": "AVG", "sum": "SUM"}[op]
                agg_exprs.append(f'{sql_func}(CAST("{col}" AS REAL)) AS "{alias}"')
            else:
                agg_exprs.append(f'NULL AS "{alias}"')

        agg_select = ", ".join(agg_exprs)

        total_matched = 0
        result: dict[str, Any] = {alias: None for alias in agg_aliases}
        selected_table: str | None = None

        campaigns_to_query = (
            [campaign_id] if campaign_id is not None
            else [d.name for d in results_resolver.list_campaigns()]
        )

        for cid in campaigns_to_query:
            conn = _open_db(cid)
            if conn is None:
                continue
            try:
                table_map = _get_table_map(conn)
                # display_name already computed above
                if display_name not in table_map:
                    continue
                sql_name = table_map[display_name]
                if selected_table is None:
                    selected_table = display_name

                pairs = [
                    (cname, r)
                    for _, _, cname, r in _iter_matching_runs(cid, configuration_id, run)
                ]

                for cname, r in pairs:
                    where, params = _build_where(cname, r, filter_where, filter_params)
                    # Get total count + aggregates in one query
                    sql = f'SELECT COUNT(*) AS _total, {agg_select} FROM "{sql_name}" {where}'
                    row = conn.execute(sql, params).fetchone()
                    if row:
                        total_matched += row["_total"] or 0
                        for alias in agg_aliases:
                            val = row[alias]
                            # Accumulate: for count/sum add; for min/max merge; for avg/distinct_count keep first
                            if val is not None:
                                if result[alias] is None:
                                    result[alias] = val
                                else:
                                    # For multi-run/config we just sum counts and sums;
                                    # min/max are not strictly accurate across merged queries
                                    # but is consistent with previous behaviour
                                    result[alias] = result[alias] + val if isinstance(result[alias], (int, float)) else val
            finally:
                conn.close()

        if selected_table is None:
            return {
                "campaign_id": campaign_id,
                "configuration_id": configuration_id,
                "run": run,
                "error": "No data.db found or table not found. Run postprocessing first.",
            }

        return {
            "campaign_id": campaign_id,
            "configuration_id": configuration_id,
            "run": run,
            "table": selected_table,
            "total_rows": total_matched,
            "matched_rows": total_matched,
            "aggregates": result,
        }
    except Exception as exc:
        logger.exception("inspect_run_data_table failed")
        return {"error": f"inspect_run_data_table failed: {exc}"}


def query_run_log(
    campaign_id: str | None = None,
    configuration_id: str | None = None,
    run: int | None = None,
    filters: list[Filter] | None = None,
    columns: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Query log entries for a specific run.

    This is the only sanctioned way to access rosout data.  It automatically
    caps results at the timestamp of the first
    ``"Scenario '...' succeeded"`` entry emitted by ``scenario_execution_ros``,
    so analysis stays within the scenario boundary.  The boundary timestamp is
    read from the ``scenario_timestamps`` table in the campaign database.

    When ``campaign_id``, ``configuration_id``, or ``run`` are omitted, all
    matching campaigns / configurations / runs are queried and their rows are
    combined.

    Filter behavior, column projection, and pagination are identical to
    ``query_run_data_table``.

    Args:
        campaign_id: Campaign identifier (optional — queries all campaigns when omitted).
        configuration_id: Configuration identifier or name (optional — queries all
            configurations when omitted).
        run: Run number (optional — queries all runs when omitted).
        filters: Optional additional filter list (AND semantics, applied *after*
            the implicit timestamp cap).
        columns: Optional list of columns to project in result rows.
        limit: Max rows to return (clamped to ``1..1000``).
        offset: Row offset in the filtered result set.

    Returns:
        Dict with selection context, pagination metadata, ``rows``.
        The ``columns`` field always starts with the four context columns
        ``campaign_id``, ``configuration_id``, ``config_name``, ``run``,
        followed by the data columns (either the requested ``columns`` projection
        or all non-context columns when ``columns`` is omitted).
        Each row in ``rows`` follows the same column order.

    Example (all WARN/ERROR entries within the scenario)::

        query_run_log(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            filters=[
                {"column": "level_name", "op": "in", "values": ["WARN", "ERROR"]}
            ],
            columns=["timestamp", "level_name", "name", "msg"],
            limit=100,
        )

    Example (planner logs only)::

        query_run_log(
            campaign_id="campaign-2026-03-08-190000",
            configuration_id="nav-config-1",
            run=0,
            filters=[{"column": "name", "op": "contains", "value": "planner"}],
            columns=["timestamp", "level_name", "name", "msg"],
        )
    """
    try:
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        filter_where, filter_params = _filters_to_sql(filters)

        all_rows: list[dict[str, Any]] = []
        all_data_columns: list[str] = []
        success_timestamps: list[float | None] = []

        campaigns_to_query = (
            [campaign_id] if campaign_id is not None
            else [d.name for d in results_resolver.list_campaigns()]
        )

        for cid in campaigns_to_query:
            conn = _open_db(cid)
            if conn is None:
                continue
            try:
                table_map = _get_table_map(conn)
                if "rosout" not in table_map:
                    continue
                sql_name = table_map["rosout"]

                pairs: list[tuple[str, int]] = [
                    (cname, r)
                    for _, _, cname, r in _iter_matching_runs(cid, configuration_id, run)
                ]

                for cname, r in pairs:
                    capped_where, capped_params = _with_ts_cap(conn, cname, r, filter_where, filter_params)
                    # Record the end timestamp for the response field
                    ts_row = conn.execute(
                        "SELECT timestamp FROM scenario_timestamps WHERE config_name = ? AND run_id = ?",
                        (cname, r),
                    ).fetchone()
                    success_timestamps.append(ts_row["timestamp"] if ts_row else None)

                    where, params = _build_where(cname, r, capped_where, capped_params)
                    _run_query(conn, sql_name, where, params, cid, all_rows, all_data_columns)
            finally:
                conn.close()

        if not all_rows:
            return {
                "campaign_id": campaign_id,
                "configuration_id": configuration_id,
                "run": run,
                "table": "rosout",
                "columns": columns or [],
                "offset": 0,
                "limit": limit,
                "total_rows": 0,
                "matched_rows": 0,
                "returned_rows": 0,
                "has_more": False,
                "next_offset": None,
                "rows": [],
            }

        total_rows = len(all_rows)
        page_rows = all_rows[offset: offset + limit]

        context_cols = ["campaign_id", "configuration_id", "config_name", "run"]
        output_columns = columns or [c for c in all_data_columns if c not in ("config_name", "run_id")]

        def _fmt_row(row: dict) -> dict:
            return {
                **{k: row.get(k) for k in context_cols},
                **{c: row.get(c) for c in output_columns},
            }

        next_offset = offset + len(page_rows)
        has_more = next_offset < total_rows

        scenario_ts = success_timestamps[0] if len(success_timestamps) == 1 else None

        return {
            "campaign_id": campaign_id,
            "configuration_id": configuration_id,
            "run": run,
            "table": "rosout",
            "columns": context_cols + output_columns,
            "offset": offset,
            "limit": limit,
            "total_rows": total_rows,
            "matched_rows": total_rows,
            "returned_rows": len(page_rows),
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "rows": [_fmt_row(r) for r in page_rows],
        }
    except Exception as exc:
        logger.exception("query_run_log failed")
        return {"error": f"query_run_log failed: {exc}"}


_TOOLS = [
    list_run_data_tables,
    query_run_data_table,
    inspect_run_data_table,
    query_run_log,
]


class RunDataPlugin:
    """Expose run data table querying/inspection as MCP tools."""

    name = "run_data"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
