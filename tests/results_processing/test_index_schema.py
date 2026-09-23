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
import threading
import time

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
        # ``campaign`` is a fixed top-level schema an ingest creates, so it is dropped
        # here too -- otherwise one test's campaign record answers another test's query.
        for statement in ("DROP SCHEMA IF EXISTS idx_test CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          "CREATE SCHEMA idx_test", "SET search_path TO idx_test"):
            conn.execute(statement)
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS idx_test CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


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


# -- concurrent writers ------------------------------------------------------------------
#
# Two ingests share one index, so two writers can decide DDL for the same table at once.
# Each test below makes the interleaving deterministic rather than hoping a race fires: a
# second session holds the DDL lock, the writer under test is started and must queue on
# it, and the holder changes the table before letting go. The writer then has to act on
# the table as it is once it holds the lock, not as it read it before.


def _second_session():
    psycopg = pytest.importorskip("psycopg")
    other = psycopg.connect(DSN, autocommit=True)
    other.execute("SET search_path TO idx_test")
    return other


def _in_thread(fn):
    """Run *fn* in a thread; return ``(thread, outcome)``, *outcome* filled when it ends."""
    outcome = {}

    def run():
        try:
            outcome["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - the thread's failure is the assertion
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def _wait_until_queued_on_the_ddl_lock(conn, thread):
    """Block until some session is waiting for an advisory lock, or fail."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        waiting = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        ).fetchone()[0]
        if waiting:
            return
        assert thread.is_alive(), "the writer finished without waiting for the DDL lock"
        time.sleep(0.02)
    raise AssertionError("the writer never queued on the DDL lock")


def test_a_widening_waits_for_the_ddl_lock_and_does_not_undo_a_wider_one(conn):
    """A writer that read ``INTEGER`` and wants ``REAL`` must not retype a column to ``REAL``
    that another writer widened to ``TEXT`` while it waited -- that would narrow it."""
    index_schema.ensure_table(conn, "metrics", {"value": INTEGER})
    conn.execute("INSERT INTO metrics (campaign_id, config_name, run_id, value) "
                 "VALUES ('c1', 'goal-1', 0, 7)")
    with _second_session() as holder:
        with index_schema.ddl_lock(holder):
            thread, outcome = _in_thread(
                lambda: index_schema.ensure_table(conn, "metrics", {"value": REAL}))
            _wait_until_queued_on_the_ddl_lock(holder, thread)
            index_schema.ensure_table(holder, "metrics", {"value": TEXT})
        thread.join(timeout=30)

    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"] == [], "the column was already wider than REAL"
    assert index_schema.read_verdicts(conn, "metrics")["value"] == TEXT
    assert _column_types(conn, "metrics")["value"] == "text"
    assert conn.execute("SELECT value FROM metrics").fetchone()[0] == "7"


def test_a_new_column_waits_for_the_ddl_lock(conn):
    """Adding a column is DDL on a table every campaign shares, so it queues like the rest."""
    index_schema.ensure_table(conn, "metrics", {"value": INTEGER})
    with _second_session() as holder:
        with index_schema.ddl_lock(holder):
            thread, outcome = _in_thread(lambda: index_schema.ensure_table(
                conn, "metrics", {"value": INTEGER, "extra": REAL}))
            _wait_until_queued_on_the_ddl_lock(holder, thread)
            index_schema.ensure_table(holder, "metrics", {"value": INTEGER, "extra": TEXT})
        thread.join(timeout=30)

    assert "error" not in outcome, outcome.get("error")
    assert index_schema.read_verdicts(conn, "metrics")["extra"] == TEXT
    assert _column_types(conn, "metrics")["extra"] == "text"


def test_a_writer_that_found_no_table_joins_the_one_created_while_it_waited(conn):
    """Both writers saw no table. The second must widen into the first's, not record its
    own narrower verdicts over it and leave out the columns the first did not have."""
    # So the writer's first wait is the table's, not the bookkeeping tables' creation.
    index_schema.ensure_metadata_tables(conn)
    with _second_session() as holder:
        with index_schema.ddl_lock(holder):
            thread, outcome = _in_thread(lambda: index_schema.ensure_table(
                conn, "metrics", {"value": INTEGER, "mine": REAL}))
            _wait_until_queued_on_the_ddl_lock(holder, thread)
            index_schema.ensure_table(holder, "metrics", {"value": REAL})
        thread.join(timeout=30)

    assert "error" not in outcome, outcome.get("error")
    verdicts = index_schema.read_verdicts(conn, "metrics")
    assert verdicts["value"] == REAL
    assert verdicts["mine"] == REAL
    types = _column_types(conn, "metrics")
    assert types["value"] == "double precision"
    assert types["mine"] == "double precision"


def test_a_table_that_already_fits_takes_no_lock(conn):
    """The common path -- a run that agrees with the table -- never queues behind DDL."""
    index_schema.ensure_table(conn, "metrics", {"value": REAL})
    with _second_session() as holder:
        with index_schema.ddl_lock(holder):
            thread, outcome = _in_thread(
                lambda: index_schema.ensure_table(conn, "metrics", {"value": INTEGER}))
            thread.join(timeout=10)
            assert not thread.is_alive(), "a no-op ensure_table waited for the DDL lock"

    assert outcome == {"result": []}
