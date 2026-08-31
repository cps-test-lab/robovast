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

"""Tables in the central index, created and widened as runs arrive.

A table per data-file stem, exactly as ``generate_data_db`` created them in ``data.db``:
drop a ``poses.csv`` in a run directory and a ``poses`` table appears, with no
registration anywhere. What changes here is only *where* the table lives, and therefore
two things about its lifetime.

**The catalog is small, so tables can stay tables.** It grows with distinct stems, not
with runs -- measured across two real campaigns, ``funnel`` has 11 tables and
``noise-probe`` 16, heavily overlapping, so a corpus of 153 campaigns is on the order of
tens. That is why the arbitrary-schema ingest is a table per stem rather than narrow rows
plus a pivot view: the catalog explosion that would justify the pivot does not happen,
and a pivot over the ~6.7M pose rows a single campaign carries would sit in the path of
every plot the run view draws.

**Widening is an ``ALTER``, not a rebuild.** A column's type is declared by the first run
that writes it and the evidence is every run, so a later run can turn an ``INTEGER``
column real, or demote a numeric one to text with a single ``'n/a'``. In SQLite that
needed the rename-copy-drop dance in ``postprocessing_plugins._retype_table``, because
SQLite cannot change a column's type. Postgres can, so the same correction is one
statement and the data is never copied.

Two rules the widening obeys:

* **It only ever widens** (``UNKNOWN`` -> ``INTEGER`` -> ``REAL`` -> ``TEXT``, the order
  :mod:`robovast.results_processing.csv_types` already defines). A verdict never narrows,
  because the values already stored were written under the wider one.
* **A disagreement is recorded, never fatal.** Two campaigns can write the same stem with
  a column that is numeric in one and text in the other -- in ``data.db`` that was a
  per-campaign warning, and centrally it becomes a cross-campaign fact. The column widens
  to text and the reason is written to :data:`COLUMN_NOTES_TABLE`, because a silently
  widened column is how ``ORDER BY timestamp`` starts sorting ``'10.022'`` before
  ``'9.5'`` again -- the exact failure :mod:`csv_types` exists to prevent.

The logical verdict is tracked in :data:`COLUMN_TYPES_TABLE` rather than read back from
``information_schema``, because one verdict has no Postgres type: ``UNKNOWN`` means "seen,
but every value so far was empty", and SQLite could express that by declaring no type at
all. Here such a column is physically ``text`` and holds only NULLs, so when the first
real number arrives it is still safe to retype -- but only a reader that knows the column
is ``UNKNOWN`` rather than genuinely textual will do it.
"""

import logging

from robovast.results_processing.csv_types import (INTEGER, REAL, TEXT, UNKNOWN, widest)

logger = logging.getLogger(__name__)

#: What scopes a metric row: which campaign, which configuration, which run. Prepended to
#: every metric table so one table holds the whole corpus and a campaign is a ``WHERE``
#: clause rather than an attached database.
CONTEXT_COLUMNS = (("campaign_id", TEXT), ("config_name", TEXT), ("run_id", INTEGER))

#: What scopes a *dimension* row -- the campaign record mirrored from ``campaign.db``.
#: Only the campaign, because a batch or a node belongs to no configuration and no run;
#: prepending the metric context there would add two columns that are NULL forever and
#: read, to anyone browsing the schema, as data that failed to arrive.
CAMPAIGN_CONTEXT = (("campaign_id", TEXT),)

#: The logical verdict per column -- see the module docstring on why ``information_schema``
#: cannot answer this.
COLUMN_TYPES_TABLE = "_column_types"

#: Caveats a declared type cannot carry, shown by ``describe_campaign_data`` beside the
#: column -- where someone about to write ``AVG(...)`` is looking. Two kinds share it, and
#: the ``kind`` column is why: a curated note ("ARRIVAL time, and the join key ...") is
#: authored in code for a column that is always worth a warning, while a widening note is
#: observed at ingest. ``poses.timestamp`` has the former and could acquire the latter, so
#: a primary key of ``(table, column)`` alone would let one silently overwrite the other.
COLUMN_NOTES_TABLE = "_column_notes"

#: A note authored in code because the column always deserves the warning.
NOTE_DOC = "doc"

#: A note recorded because this ingest widened the column.
NOTE_WIDENING = "widening"

#: ``UNKNOWN`` has no Postgres spelling; it is stored as ``text`` holding only NULLs and
#: retyped once a value gives it a verdict.
_PG_TYPE = {UNKNOWN: "text", INTEGER: "bigint", REAL: "double precision", TEXT: "text"}


def _quote(identifier: str) -> str:
    """Quote an identifier for DDL, doubling any embedded quote.

    Column names come from a CSV header, so they are attacker-adjacent in the same sense
    every ingested filename is: nothing validates them upstream, and a stem or header is
    whatever a scenario wrote. Parameterisation is not available for identifiers, so this
    is the one place that has to be right.
    """
    return '"' + identifier.replace('"', '""') + '"'


def ensure_metadata_tables(conn) -> None:
    """Create the two bookkeeping tables if they are absent."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote(COLUMN_TYPES_TABLE)} ("
        "table_name text NOT NULL, column_name text NOT NULL, verdict text NOT NULL, "
        "PRIMARY KEY (table_name, column_name))")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote(COLUMN_NOTES_TABLE)} ("
        "table_name text NOT NULL, column_name text NOT NULL, kind text NOT NULL, "
        "note text NOT NULL, PRIMARY KEY (table_name, column_name, kind))")


def read_verdicts(conn, table: str) -> dict:
    """The recorded logical type per column of *table*, empty if it is new."""
    rows = conn.execute(
        f"SELECT column_name, verdict FROM {_quote(COLUMN_TYPES_TABLE)} WHERE table_name = %s",
        (table,)).fetchall()
    return dict(rows)


def _record_verdict(conn, table: str, column: str, verdict: str) -> None:
    conn.execute(
        f"INSERT INTO {_quote(COLUMN_TYPES_TABLE)} (table_name, column_name, verdict) "
        "VALUES (%s, %s, %s) ON CONFLICT (table_name, column_name) DO UPDATE "
        "SET verdict = EXCLUDED.verdict",
        (table, column, verdict))


def record_note(conn, table: str, column: str, note: str, kind: str = NOTE_DOC) -> None:
    """Attach a note to a column, replacing only a previous note *of the same kind*."""
    conn.execute(
        f"INSERT INTO {_quote(COLUMN_NOTES_TABLE)} (table_name, column_name, kind, note) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (table_name, column_name, kind) DO UPDATE "
        "SET note = EXCLUDED.note",
        (table, column, kind, note))


def known_tables(conn) -> list:
    """Every table the index has ingested into, from the verdict registry."""
    ensure_metadata_tables(conn)
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT table_name FROM {_quote(COLUMN_TYPES_TABLE)} ORDER BY 1").fetchall()]


def clear_campaign(conn, campaign_id: str) -> dict:
    """Remove one campaign's rows from every table; return rows deleted per table.

    What makes re-ingest idempotent, and therefore what makes the reproducibility invariant
    checkable: a campaign re-postprocessed, or re-read after the index was dropped, must
    land the same rows rather than a second copy of them.

    Scoped to the one campaign on purpose. Truncating a table would be faster and would take
    every other campaign with it -- the difference between re-running one postprocess and
    re-ingesting the whole corpus.
    """
    deleted = {}
    for table in known_tables(conn):
        cursor = conn.execute(
            f"DELETE FROM {_quote(table)} WHERE campaign_id = %s", (campaign_id,))
        if cursor.rowcount:
            deleted[table] = cursor.rowcount
    return deleted


def ensure_table(conn, table: str, types: dict, *, source: str = "",
                 context=CONTEXT_COLUMNS) -> list:
    """Make *table* able to hold columns *types*; return the widenings that happened.

    *types* maps column name to a :mod:`csv_types` verdict, as
    :func:`~robovast.results_processing.csv_types.infer_column_types` returns. The table
    is created on first sight with *context* first (:data:`CONTEXT_COLUMNS` for a metric
    table, :data:`CAMPAIGN_CONTEXT` for a dimension one); later calls add columns
    and widen existing ones. Idempotent, and cheap when nothing changed -- the common
    case, since a campaign's runs mostly agree.

    *source* names what produced this batch (a run directory, a topic) and appears in a
    column note when it is the batch that forced a widening, so the note says which run
    disagreed rather than only that some run did.

    Returns ``[(column, before, after), ...]``, empty when the table already fitted.
    """
    ensure_metadata_tables(conn)
    known = read_verdicts(conn, table)
    widened = []

    if not known:
        columns = list(context) + [(c, types[c]) for c in types if c not in dict(context)]
        defs = ", ".join(f"{_quote(name)} {_PG_TYPE[verdict]}" for name, verdict in columns)
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_quote(table)} ({defs})")
        # The one index data.db also built: every read is scoped to a run or a campaign,
        # and a sequential scan of a pose table is the difference between a plot and a
        # timeout.
        index_cols = ", ".join(_quote(name) for name, _ in context)
        conn.execute(f"CREATE INDEX IF NOT EXISTS {_quote('idx_' + table + '_ctx')} "
                     f"ON {_quote(table)} ({index_cols})")
        for name, verdict in columns:
            _record_verdict(conn, table, name, verdict)
        return widened

    for column, verdict in types.items():
        if column not in known:
            conn.execute(f"ALTER TABLE {_quote(table)} "
                         f"ADD COLUMN IF NOT EXISTS {_quote(column)} {_PG_TYPE[verdict]}")
            _record_verdict(conn, table, column, verdict)
            continue
        current = known[column]
        target = widest(current, verdict)
        if target == current:
            continue
        # Safe in every direction this can take: UNKNOWN is all-NULL, INTEGER -> REAL is
        # lossless, and anything -> text has a cast. Never narrows, so a stored value is
        # never re-interpreted under a tighter type than the one it was written for.
        using = f" USING {_quote(column)}::{_PG_TYPE[target]}"
        conn.execute(f"ALTER TABLE {_quote(table)} ALTER COLUMN {_quote(column)} "
                     f"TYPE {_PG_TYPE[target]}{using}")
        _record_verdict(conn, table, column, target)
        widened.append((column, current, target))
        note = (f"widened {current} -> {target}"
                + (f" by {source}" if source else "")
                + "; earlier rows were written under the narrower type")
        record_note(conn, table, column, note, kind=NOTE_WIDENING)
        logger.info("index: %s.%s widened %s -> %s%s",
                    table, column, current, target, f" by {source}" if source else "")

    return widened
