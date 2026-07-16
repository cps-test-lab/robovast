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

"""MCP plugin: query a campaign's results with read-only **SQL**.

A campaign's per-run metrics are consolidated into
``<campaign>/_execution/data.db`` during postprocessing — one table per CSV
stem (``poses``, ``behaviors``, …), a ``rosout`` log table, ``scenario_timestamps``,
and a ``runs`` **dimension table** (per-run ``status``/``duration_s`` + scenario
parameters as ``param_*`` columns). SQL is the generic query interface an LLM
wants, so this plugin exposes exactly two tools:

* :func:`describe_campaign_data` — the schema (tables, columns, row counts) to
  write queries against;
* :func:`query_campaign_data_sql` — a **read-only** ``SELECT`` (enforced by an
  authorizer + ``mode=ro``), with the campaign's ``campaign.db`` attached as
  schema ``campaign`` so structure/objectives join in the same query.

Joining ``runs`` to any metric table on ``(config_name, run_id)`` answers
"how does <param> affect <metric>" in one query.

The underlying ``data.db`` and the ``vast eval gui`` notebook path are untouched
by this plugin — it only reads.
"""

import logging
import re
import sqlite3
from pathlib import Path

from fastmcp import FastMCP

from robovast.mcp_server import results_resolver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB access helpers (shared with the eval GUI's data.db; do not change lightly)
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
    """Open the campaign ``data.db`` read-only, or None if it does not exist.

    Also attaches ``campaign.db`` (read-only) as schema ``campaign`` when present,
    and registers a REGEXP function.
    """
    db_path = _get_db_path(campaign_id)
    if db_path is None:
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def _regexp(pattern: str, value) -> bool:
        if value is None:
            return False
        try:
            return re.search(pattern, str(value)) is not None
        except re.error:
            return False

    conn.create_function("REGEXP", 2, _regexp)

    # Attach the campaign store read-only so params/objectives/status join in SQL.
    campaign_db = db_path.parent.parent / "campaign.db"
    if campaign_db.exists():
        try:
            conn.execute("ATTACH DATABASE ? AS campaign",
                         (f"file:{campaign_db}?mode=ro",))
        except sqlite3.Error as e:
            logger.debug("could not attach campaign.db for %s: %s", campaign_id, e)
    return conn


def _get_table_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Return mapping of display_name -> sql_name from _table_name_map."""
    try:
        rows = conn.execute("SELECT display_name, sql_name FROM _table_name_map").fetchall()
        return {r["display_name"]: r["sql_name"] for r in rows}
    except sqlite3.Error:
        return {}


# ---------------------------------------------------------------------------
# Read-only SQL enforcement
# ---------------------------------------------------------------------------

# Actions a pure read query needs; everything else (INSERT/UPDATE/DELETE/
# CREATE/DROP/ATTACH/DETACH/PRAGMA-writes/...) is denied by the authorizer.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _readonly_authorizer(action, _arg1, _arg2, _dbname, _trigger):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def describe_campaign_data(campaign_id: str) -> dict:
    """Describe a campaign's queryable data — the schema to write SQL against.

    Lists every table in ``data.db`` (metric tables, ``rosout``, the ``runs``
    dimension table with per-run status/duration + ``param_*`` columns) plus the
    attached ``campaign.db`` (schema ``campaign``: ``unit``/``batch``/``campaign``
    with params/objectives). Use this before :func:`query_campaign_data_sql`.

    Args:
        campaign_id: Campaign identifier (e.g. ``"campaign-2026-03-08-190000"``)
            or an absolute campaign path.

    Returns:
        ``{campaign_id, tables: [{schema, table, columns, rows}], note}`` or
        ``{error}`` if no ``data.db`` exists (run postprocessing first).
    """
    conn = _open_db(campaign_id)
    if conn is None:
        return {"error": "No data.db found for this campaign. Run postprocessing first."}
    try:
        tables = []
        # main schema (data.db) + attached campaign schema
        schemas = [("main", "data.db")]
        if conn.execute("PRAGMA database_list").fetchall():
            names = [r["name"] for r in conn.execute("PRAGMA database_list").fetchall()]
            if "campaign" in names:
                schemas.append(("campaign", "campaign.db"))
        for schema, _label in schemas:
            rows = conn.execute(
                f"SELECT name FROM {schema}.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name != '_table_name_map' ORDER BY name").fetchall()
            for tr in rows:
                name = tr["name"]
                cols = [r["name"] for r in conn.execute(
                    f'PRAGMA {schema}.table_info("{name}")').fetchall()]
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM {schema}."{name}"').fetchone()[0]
                except sqlite3.Error:
                    n = None
                tables.append({"schema": schema, "table": name,
                               "columns": cols, "rows": n})
        return {
            "campaign_id": campaign_id,
            "tables": tables,
            "note": "Query with query_campaign_data_sql. Join the 'runs' table "
                    "(param_* columns + status/duration) to any metric table on "
                    "(config_name, run_id). campaign.db is attached as schema "
                    "'campaign'.",
        }
    finally:
        conn.close()


def query_campaign_data_sql(campaign_id: str, sql: str, max_rows: int = 500) -> dict:
    """Run a **read-only** SQL query over a campaign's data.

    The query runs against ``data.db`` (metric tables + the ``runs`` dimension
    table) with ``campaign.db`` attached as schema ``campaign``. Only ``SELECT``
    is permitted — any write/DDL/ATTACH/PRAGMA-write is rejected. A ``REGEXP(pat,
    col)`` function is available.

    Discover the schema first with :func:`describe_campaign_data`.

    Args:
        campaign_id: Campaign identifier or absolute campaign path.
        sql: A single ``SELECT`` statement.
        max_rows: Maximum rows to return (clamped to ``1..5000``); ``truncated``
            marks when more rows matched.

    Returns:
        ``{columns, rows, row_count, truncated}`` or ``{error}``.

    Example — mean of a metric per parameter value::

        query_campaign_data_sql(
            campaign_id="campaign-...",
            sql='''SELECT r.param_wind_strength,
                          AVG(CAST(m.error AS REAL)) AS mean_error
                   FROM runs r JOIN landing_error m
                     ON r.config_name = m.config_name AND r.run_id = m.run_id
                   GROUP BY r.param_wind_strength ORDER BY r.param_wind_strength''')
    """
    conn = _open_db(campaign_id)
    if conn is None:
        return {"error": "No data.db found for this campaign. Run postprocessing first."}
    max_rows = max(1, min(int(max_rows), 5000))
    try:
        conn.set_authorizer(_readonly_authorizer)
        try:
            cursor = conn.execute(sql)
        except sqlite3.DatabaseError as e:
            # Authorizer denials surface as "not authorized"; make that actionable.
            msg = str(e)
            if "not authorized" in msg.lower():
                return {"error": "Only read-only SELECT queries are allowed "
                                 f"(rejected: {msg})."}
            return {"error": f"SQL error: {msg}"}
        if cursor.description is None:
            return {"error": "query returned no result set (only SELECT is supported)"}
        columns = [d[0] for d in cursor.description]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [dict(zip(columns, r)) for r in fetched[:max_rows]]
        return {
            "campaign_id": campaign_id,
            "columns": columns,
            "row_count": len(rows),
            "truncated": truncated,
            "rows": rows,
        }
    finally:
        conn.set_authorizer(None)
        conn.close()


_TOOLS = [
    describe_campaign_data,
    query_campaign_data_sql,
]


class RunDataPlugin:
    """Expose read-only SQL querying of a campaign's results as MCP tools."""

    name = "run_data"

    def register(self, mcp: FastMCP) -> None:
        for fn in _TOOLS:
            mcp.tool()(fn)
