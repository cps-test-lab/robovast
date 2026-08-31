# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tables in the central index: created per stem, widened in place, never narrowed.

These run against a real Postgres because the behaviour under test *is* the DDL --
``ALTER COLUMN ... TYPE`` replacing SQLite's rename-copy-drop rebuild is the reason the
module exists, and a mocked connection would assert the SQL string rather than that the
column ends up holding what it claims.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip, so the suite stays
runnable on a host with no database.
"""

import os

import pytest

from robovast.results_processing import index_schema
from robovast.results_processing.csv_types import INTEGER, REAL, TEXT, UNKNOWN

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")


@pytest.fixture(name="conn")
def _conn():
    """A connection to an empty schema, dropped afterwards.

    Each test gets its own schema rather than its own database: the ingest is
    schema-relative, so this isolates tables without a create-database round trip per
    test.
    """
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS idx_test CASCADE")
        conn.execute("CREATE SCHEMA idx_test")
        conn.execute("SET search_path TO idx_test")
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS idx_test CASCADE")


def _column_types(conn, table):
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'idx_test' AND table_name = %s ORDER BY ordinal_position",
        (table,)).fetchall()
    return dict(rows)


def test_a_new_stem_becomes_a_table_with_the_context_columns_first(conn):
    """The ingest contract: a stem appears as a table, scoped by campaign/config/run."""
    index_schema.ensure_table(conn, "poses", {"timestamp": REAL, "frame": TEXT})

    types = _column_types(conn, "poses")
    assert list(types)[:3] == ["campaign_id", "config_name", "run_id"]
    assert types["campaign_id"] == "text"
    assert types["run_id"] == "bigint"
    assert types["timestamp"] == "double precision"
    assert types["frame"] == "text"


def test_the_context_index_exists_because_every_read_is_scoped(conn):
    """A pose table without it is a sequential scan behind every plot."""
    index_schema.ensure_table(conn, "poses", {"timestamp": REAL})

    indexes = [r[0] for r in conn.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'idx_test' AND tablename = 'poses'").fetchall()]
    assert any("campaign_id" in d and "config_name" in d and "run_id" in d for d in indexes)


def test_ensuring_twice_changes_nothing(conn):
    """Idempotent, and the common case: a campaign's runs mostly agree."""
    index_schema.ensure_table(conn, "poses", {"timestamp": REAL})
    widened = index_schema.ensure_table(conn, "poses", {"timestamp": REAL})

    assert widened == []
    assert _column_types(conn, "poses")["timestamp"] == "double precision"


def test_a_later_run_adds_a_column_nobody_declared(conn):
    """The extension mechanism keeps working after the table exists."""
    index_schema.ensure_table(conn, "nav_metrics", {"duration_s": REAL})
    index_schema.ensure_table(conn, "nav_metrics", {"duration_s": REAL, "collided": INTEGER})

    types = _column_types(conn, "nav_metrics")
    assert types["collided"] == "bigint"


def test_an_integer_column_widens_to_real_in_place(conn):
    """The measured case: one run's counts, a later run's fractions.

    Asserted through a stored value, not through the declared type alone -- the point is
    that the row written under the narrower verdict survives the widening.
    """
    index_schema.ensure_table(conn, "metrics", {"value": INTEGER})
    conn.execute("INSERT INTO metrics (campaign_id, config_name, run_id, value) "
                 "VALUES ('c1', 'goal-1', 0, 3)")

    widened = index_schema.ensure_table(conn, "metrics", {"value": REAL}, source="run 1")

    assert widened == [("value", INTEGER, REAL)]
    assert _column_types(conn, "metrics")["value"] == "double precision"
    assert conn.execute("SELECT value FROM metrics").fetchone()[0] == 3.0


def test_one_stray_label_demotes_a_numeric_column_to_text(conn):
    """``csv_types``' strict rule, surviving into the index: numbers become text.

    The stored number must come across as its own text, because a query that was
    averaging the column will now see strings and must at least see the right ones.
    """
    index_schema.ensure_table(conn, "metrics", {"value": INTEGER})
    conn.execute("INSERT INTO metrics (campaign_id, config_name, run_id, value) "
                 "VALUES ('c1', 'goal-1', 0, 42)")

    index_schema.ensure_table(conn, "metrics", {"value": TEXT})

    assert _column_types(conn, "metrics")["value"] == "text"
    assert conn.execute("SELECT value FROM metrics").fetchone()[0] == "42"


def test_an_unknown_column_still_becomes_numeric_when_evidence_arrives(conn):
    """Why the logical verdict is tracked separately from the Postgres type.

    ``UNKNOWN`` means "seen, but every value so far was empty". It is physically ``text``
    holding only NULLs, so a later run's numbers must still land as numbers -- reading
    ``information_schema`` alone would see ``text`` and keep them as strings, which is the
    premature-declaration bug ``UNKNOWN`` exists to prevent.
    """
    index_schema.ensure_table(conn, "metrics", {"value": UNKNOWN})
    assert _column_types(conn, "metrics")["value"] == "text"

    index_schema.ensure_table(conn, "metrics", {"value": REAL})

    assert _column_types(conn, "metrics")["value"] == "double precision"


def test_a_verdict_never_narrows(conn):
    """Values already stored were written under the wider type."""
    index_schema.ensure_table(conn, "metrics", {"value": TEXT})
    conn.execute("INSERT INTO metrics (campaign_id, config_name, run_id, value) "
                 "VALUES ('c1', 'goal-1', 0, 'n/a')")

    widened = index_schema.ensure_table(conn, "metrics", {"value": INTEGER})

    assert widened == []
    assert _column_types(conn, "metrics")["value"] == "text"
    assert conn.execute("SELECT value FROM metrics").fetchone()[0] == "n/a"


def test_a_cross_campaign_disagreement_is_recorded_not_fatal(conn):
    """Centrally, one campaign's stray label widens a column another campaign filled.

    In ``data.db`` this was a per-campaign warning nobody could query afterwards. It has
    to stay non-fatal -- refusing the ingest would lose a whole campaign over one cell --
    so the widening is written down instead, and names the batch that caused it.
    """
    index_schema.ensure_table(conn, "metrics", {"value": INTEGER}, source="campaign-a run 0")
    index_schema.ensure_table(conn, "metrics", {"value": TEXT}, source="campaign-b run 3")

    note = conn.execute(
        f"SELECT note FROM {index_schema.COLUMN_NOTES_TABLE} "
        "WHERE table_name = 'metrics' AND column_name = 'value' AND kind = %s",
        (index_schema.NOTE_WIDENING,)).fetchone()[0]
    assert "INTEGER -> TEXT" in note
    assert "campaign-b run 3" in note


def test_a_widening_note_does_not_clobber_the_curated_one(conn):
    """``poses.timestamp`` carries an authored warning *and* could be widened.

    Both notes are shown beside the column by ``describe_campaign_data``, and the curated
    one is the more valuable of the two -- it is what stops someone differencing an
    arrival-time column. With a primary key of ``(table, column)`` alone the ingest would
    have silently replaced it.
    """
    index_schema.ensure_metadata_tables(conn)
    index_schema.record_note(conn, "poses", "timestamp",
                             "ARRIVAL time, and the join key every other table shares")
    index_schema.ensure_table(conn, "poses", {"timestamp": INTEGER})
    index_schema.ensure_table(conn, "poses", {"timestamp": TEXT}, source="campaign-b run 3")

    notes = dict(conn.execute(
        f"SELECT kind, note FROM {index_schema.COLUMN_NOTES_TABLE} "
        "WHERE table_name = 'poses' AND column_name = 'timestamp'").fetchall())
    assert set(notes) == {index_schema.NOTE_DOC, index_schema.NOTE_WIDENING}
    assert "ARRIVAL time" in notes[index_schema.NOTE_DOC]
    assert "INTEGER -> TEXT" in notes[index_schema.NOTE_WIDENING]


def test_a_quote_in_a_column_name_cannot_break_out_of_the_ddl(conn):
    """Column names come from a CSV header, which nothing upstream validates."""
    index_schema.ensure_table(conn, "odd", {'we"ird': TEXT})

    assert 'we"ird' in _column_types(conn, "odd")
