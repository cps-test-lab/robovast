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

"""Reading the central index: one campaign's rows, or several campaigns at once.

The Postgres side of what ``data_query`` did against ``data.db``. The public contract is
unchanged -- ``{columns, rows, row_count, truncated}``, the same caps, the same error type
-- because the callers are the MCP tools, the service routes, the web UI's panels and every
notebook, and none of them should notice where the rows now live.

What does change, and is the point: **a campaign is a ``WHERE`` clause.** Comparing the
nine campaigns of a search arm used to mean attaching nine ``data.db`` files, each of which
had to be fetched into the pod first -- ~10 GB moved to answer one question. Now it is one
query against one database, and nothing is fetched at all.

**Read-only is enforced by the session, not by inspecting the SQL.** SQLite had a C-level
authorizer that vetted every action; Postgres has no equivalent hook, and a denylist of
statement kinds parsed out of the query string is exactly the shape that leaks -- it has to
enumerate what it forbids, and it goes stale the moment the grammar grows. So the
connection is opened with ``default_transaction_read_only``, which refuses writes at the
server regardless of how the statement is spelled, and ``statement_timeout`` bounds a query
that would otherwise hold a connection for the rest of the afternoon.

That is deliberately *not* the only line of defence. The role a reader connects as should
also lack write grants; this module cannot arrange that, and says so rather than implying
the session setting is sufficient on its own.
"""

import logging
from typing import TYPE_CHECKING

from robovast.common import index_db
from robovast.results_processing import index_dialect, index_functions, index_schema

if TYPE_CHECKING:  # pragma: no cover - for type checkers and linters only
    import psycopg

logger = logging.getLogger(__name__)

#: How long a single query may run. Long enough for an honest scan over a campaign's poses,
#: short enough that a cross join nobody meant to write does not hold a connection open
#: until someone notices. A caller that legitimately needs longer should aggregate in SQL.
STATEMENT_TIMEOUT_MS = 120_000

#: Tables that are bookkeeping rather than data, hidden from ``describe`` the way
#: ``data.db``'s were. They are queryable if someone knows the name; they are simply not
#: offered as an answer to "what is in this campaign?".
INTERNAL_TABLES = frozenset({
    index_schema.COLUMN_TYPES_TABLE,
    index_schema.COLUMN_NOTES_TABLE,
    index_functions.FUNCTIONS_VERSION_TABLE,
    index_schema.CAMPAIGNS_TABLE,
    "_table_name_map",
})


class IndexQueryError(RuntimeError):
    """A query the index refused, or could not run.

    Distinct from :class:`~robovast.common.errors.IndexUnreachableError`: the index
    answered, and what it answered -- an undefined column, a syntax error, a write
    attempted through a read-only session -- is the caller's to act on.
    """

    include_traceback = False


def open_index(*, readonly: bool = True,
               timeout_ms: int = STATEMENT_TIMEOUT_MS) -> "psycopg.Connection":
    """A connection to the index, ready to be queried.

    Ensures the query functions exist before handing the connection back, so a caller
    never has to know that ``PERCENTILE`` is something RoboVAST defines rather than
    something Postgres ships.
    """
    # The no-member disables below are pylint failing to infer psycopg's Connection through
    # index_db.connect's deliberately local driver import -- not a missing method.
    conn = index_db.connect(readonly=False)
    try:
        index_functions.install(conn)
        conn.execute(  # pylint: disable=no-member
            f"SET statement_timeout = {int(timeout_ms)}")
        if readonly:
            conn.execute(  # pylint: disable=no-member
                "SET default_transaction_read_only = on")
    except Exception:
        conn.close()  # pylint: disable=no-member
        raise
    return conn


def index_schemas(conn) -> list:
    """The schemas this index owns: the connection's own, plus ``campaign``.

    Scoped deliberately rather than enumerating the database. A deployment may keep other
    things in the same database, and a reader asking "what is in this campaign?" must not
    be handed a table that belongs to something else -- nor, when two schemas hold a table
    of the same name, the wrong one's row count.
    """
    current = conn.execute("SELECT current_schema()").fetchone()[0] or "public"
    return [current, index_schema.CAMPAIGN_SCHEMA]


def _campaign_exists(conn, campaign_id: str) -> bool:
    """Has anything ever been ingested for *campaign_id*?

    Read from the ingest registry rather than inferred from ``campaign.campaign``. A
    campaign whose ``campaign.db`` is missing still has its measurements -- ``ingest_campaign``
    tolerates that on purpose, because a campaign that ended badly is exactly the one worth
    reading -- so inferring from the record would report it absent while its rows sat there.
    """
    try:
        row = conn.execute(
            f'SELECT 1 FROM "{index_schema.CAMPAIGNS_TABLE}" WHERE campaign_id = %s LIMIT 1',
            (campaign_id,)).fetchone()
    except Exception:  # pylint: disable=broad-except
        return False
    return row is not None


def missing_campaign_note(campaign_id: str) -> str:
    """What to say when a campaign has no rows at all.

    "Not ingested" and "ingested and empty" are different answers, and an empty result set
    claims the second. The corpus that predates the index is not carried across, so this is
    the expected answer for an old campaign rather than a fault -- and saying which it is
    saves the reader from re-running a query that will never return anything.
    """
    return (f"No rows for {campaign_id}: this campaign is not in the index. Either its "
            "postprocessing has not run, or it predates the index and was not carried "
            "across. Re-running postprocessing ingests it.")


def query_index(sql: str, *, max_rows: int = 500, max_bytes: int | None = None,
                campaign_id: str | None = None) -> dict:
    """Run one read-only ``SELECT``; return ``{columns, rows, row_count, truncated}``.

    *campaign_id*, when given, is used only to explain an empty result -- the scoping
    itself belongs in the caller's ``WHERE``, which is what lets one query span campaigns.
    """
    # Imported lazily for the reason in common.index_db: `results_processing` is reachable
    # from the container-side scripts, which have no driver and never talk to an index.
    import psycopg  # pylint: disable=import-outside-toplevel

    from robovast.results_processing.data_query import (  # pylint: disable=import-outside-toplevel
        _MAX_RESULT_BYTES, _cap_cell, _cap_result_size)

    max_rows = max(1, min(int(max_rows), 5000))
    max_bytes = _MAX_RESULT_BYTES if max_bytes is None else max(1024, int(max_bytes))

    # Two SQLite spellings Postgres reads differently and silently -- see index_dialect.
    sql = index_dialect.translate(sql)

    conn = open_index(readonly=True)
    try:
        try:
            cursor = conn.execute(sql)  # pylint: disable=no-member
        except psycopg.Error as exc:
            message = str(exc).strip()
            if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
                raise IndexQueryError(
                    f"Only read-only SELECT queries are allowed (rejected: {message}).") from exc
            raise IndexQueryError(f"SQL error: {message}") from exc

        if cursor.description is None:
            raise IndexQueryError(
                "query returned no result set (only SELECT is supported)")

        columns = [d.name for d in cursor.description]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [{c: _cap_cell(v) for c, v in zip(columns, r)} for r in fetched[:max_rows]]
        rows, size_capped = _cap_result_size(rows, max_bytes)

        result = {"columns": columns, "row_count": len(rows),
                  "truncated": truncated or size_capped, "rows": rows}
        if size_capped:
            result["note"] = (
                f"stopped at {len(rows)} rows: the reply reached the "
                f"{max_bytes // 1024} KB ceiling. Rows are capped separately from size, "
                "and a wide table reaches this long before max_rows. Aggregate in SQL "
                "(COUNT/AVG/MIN/MAX, GROUP BY) or select the columns you need -- or export "
                "the whole result as CSV instead of reading it here.")
        elif not rows and campaign_id and not _campaign_exists(conn, campaign_id):
            result["note"] = missing_campaign_note(campaign_id)
        return result
    finally:
        conn.close()  # pylint: disable=no-member


def list_tables(conn, campaign_id: str | None = None) -> list:
    """``[{schema, table, rows, columns}]`` for what a caller may query.

    Row counts are scoped to *campaign_id* when given, because "how much is in this
    campaign" is the question being asked -- a corpus-wide count would report the index's
    size and read as the campaign's.
    """
    rows = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s) "
        "ORDER BY table_schema, table_name", (index_schemas(conn),)).fetchall()

    out = []
    for schema, table in rows:
        if table in INTERNAL_TABLES:
            continue
        columns = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table)).fetchall()]
        name = index_schema.qualified(table, schema)
        if campaign_id and "campaign_id" in columns:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {name} WHERE campaign_id = %s",
                (campaign_id,)).fetchone()[0]
        else:
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        out.append({"schema_": schema or "main", "table": table,
                    "columns": columns, "rows": count})
    return out


def column_notes(conn) -> dict:
    """``{table: {column: note}}``, both kinds folded together.

    A curated warning and an observed widening are different facts about the same column
    and a reader needs both -- which is why they are separate rows rather than one that
    overwrites the other (see :data:`index_schema.COLUMN_NOTES_TABLE`).
    """
    notes: dict = {}
    table = index_schema.qualified(index_schema.COLUMN_NOTES_TABLE)
    try:
        rows = conn.execute(
            f"SELECT table_name, column_name, kind, note FROM {table} "
            "ORDER BY table_name, column_name, kind").fetchall()
    except Exception:  # pylint: disable=broad-except
        return notes
    for table_name, column_name, _kind, note in rows:
        existing = notes.setdefault(table_name, {}).get(column_name)
        notes[table_name][column_name] = f"{existing} {note}" if existing else note
    return notes
