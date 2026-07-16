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
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


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


def _open_db(campaign_dir) -> sqlite3.Connection:
    """Open ``<campaign_dir>/_execution/data.db`` read-only (raises if absent).

    Attaches ``<campaign_dir>/campaign.db`` read-only as schema ``campaign`` when
    present, and registers a ``REGEXP`` function.
    """
    db_path = Path(campaign_dir) / "_execution" / "data.db"
    if not db_path.exists():
        raise DataQueryError(
            "No data.db found for this campaign. Run postprocessing first.")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("REGEXP", 2, _regexp)
    campaign_db = Path(campaign_dir) / "campaign.db"
    if campaign_db.exists():
        try:
            conn.execute("ATTACH DATABASE ? AS campaign",
                         (f"file:{campaign_db}?mode=ro",))
        except sqlite3.Error as e:
            logger.debug("could not attach campaign.db at %s: %s", campaign_db, e)
    return conn


def describe_data_db(campaign_dir) -> dict:
    """Return ``{tables: [{schema, table, columns, rows}], note}`` for a campaign."""
    conn = _open_db(campaign_dir)
    try:
        tables = []
        names = [r["name"] for r in conn.execute("PRAGMA database_list").fetchall()]
        schemas = ["main"] + (["campaign"] if "campaign" in names else [])
        for schema in schemas:
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
                tables.append({"schema": schema, "table": name, "columns": cols, "rows": n})
        return {
            "tables": tables,
            "note": "Join the 'runs' table (param_* columns + status/duration) to any "
                    "metric table on (config_name, run_id). campaign.db is attached as "
                    "schema 'campaign'.",
        }
    finally:
        conn.close()


def query_data_db(campaign_dir, sql: str, max_rows: int = 500) -> dict:
    """Run a read-only ``SELECT``; return ``{columns, rows, row_count, truncated}``.

    Raises :class:`DataQueryError` for a rejected (non-read) or invalid query.
    """
    conn = _open_db(campaign_dir)
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
        rows = [dict(zip(columns, r)) for r in fetched[:max_rows]]
        return {"columns": columns, "row_count": len(rows),
                "truncated": truncated, "rows": rows}
    finally:
        conn.set_authorizer(None)
        conn.close()
