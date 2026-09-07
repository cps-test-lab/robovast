# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A table the ingest created is covered by the campaign scope, and stays covered.

The index connection is autocommit, so "create the table" and "put the policy on it" were
two separately committed acts. An ingest that died in between left a table that carried a
``campaign_id``, held recorded column verdicts -- which is what makes every later
``ensure_table`` take its widen path -- and was covered by nothing. From then on
:func:`~robovast.results_processing.index_scope.assert_enforceable` refused *every* scoped
read of the index, naming that table, and no amount of re-ingesting the same campaign
repaired it: only the whole-index sweep at the head of some other campaign's ingest did.

The two tests here are the two halves of that: creation cannot leave the window open, and
a table already in that state is repaired the first time the ingest touches it.
"""

import os

import pytest

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
SCHEMA = "index_scope_repair_test"

pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

TABLE = "scan_intensities"


@pytest.fixture(name="index")
def _index(monkeypatch):
    """A writable, empty index in its own schema."""
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        setup.execute(f"CREATE SCHEMA {SCHEMA}")
    with index_db.connect() as conn:
        yield conn
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _unsecure(conn, table):
    """Leave *table* exactly as an ingest killed between CREATE and the policy left it."""
    from robovast.results_processing import index_scope

    conn.execute(f'DROP POLICY "{index_scope.POLICY_NAME}" ON "{table}"')
    conn.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    conn.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


@pg
def test_a_table_the_ingest_creates_is_covered_before_anything_can_read_it(index):
    from robovast.results_processing import index_schema, index_scope

    index_schema.ensure_table(index, TABLE, {"v": index_schema.REAL})

    index_scope.assert_enforceable(index)
    assert index_scope.table_is_secured(index, TABLE)


@pg
def test_creating_a_table_and_scoping_it_are_one_act(index):
    """The window itself: if the policy cannot be applied, the table must not be there.

    A table committed without its policy is the state that refuses every later read, so
    the failure has to take the table with it rather than leave it behind.
    """
    from robovast.results_processing import index_schema, index_scope

    # The bookkeeping tables first, and scoped, so the refusal below lands on *this*
    # table's policy rather than on ``ensure_metadata_tables``' one-time securing.
    index_schema.ensure_metadata_tables(index)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the ingest died here")

    monkey = index_scope.secure_table
    index_scope.secure_table = refuse
    try:
        with pytest.raises(RuntimeError):
            index_schema.ensure_table(index, TABLE, {"v": index_schema.REAL})
    finally:
        index_scope.secure_table = monkey

    assert index.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0] is None
    # And the verdicts went with it, so the retry below takes the create path rather than
    # the widen path -- a table recorded as known but absent is its own trap.
    assert index_schema.read_verdicts(index, TABLE) == {}

    index_schema.ensure_table(index, TABLE, {"v": index_schema.REAL})
    assert index_scope.table_is_secured(index, TABLE)


@pg
def test_a_table_left_uncovered_is_repaired_the_next_time_it_is_written(index):
    """The heal. An index already holding one must not need an unrelated campaign."""
    from robovast.results_processing import index_schema, index_scope

    index_schema.ensure_table(index, TABLE, {"v": index_schema.REAL})
    _unsecure(index, TABLE)
    with pytest.raises(index_scope.ScopeNotEnforceable):
        index_scope.assert_enforceable(index)

    # The commonest shape of a later ingest: the same columns, nothing to widen. This is
    # the call that used to do nothing at all for the scope.
    index_schema.ensure_table(index, TABLE, {"v": index_schema.REAL})

    # Asserted through the check a scoped read actually runs, so this test says the read
    # is answerable again rather than only that a helper reports it is.
    index_scope.assert_enforceable(index)
    assert index_scope.table_is_secured(index, TABLE)


@pg
def test_the_repair_also_reaches_a_table_that_is_being_widened(index):
    from robovast.results_processing import index_schema, index_scope

    index_schema.ensure_table(index, TABLE, {"v": index_schema.REAL})
    _unsecure(index, TABLE)

    index_schema.ensure_table(index, TABLE,
                              {"v": index_schema.REAL, "w": index_schema.REAL})

    index_scope.assert_enforceable(index)
    assert index_scope.table_is_secured(index, TABLE)


@pg
def test_a_table_with_nothing_to_scope_by_is_not_reported_as_a_gap(index):
    """``table_is_secured`` must not send the repair after a table it would skip.

    The bookkeeping tables describe the index's own schema and carry no campaign_id;
    ``secure_table`` leaves them alone, so calling them unsecured would mean an ingest
    that tries, and logs, a repair per data file forever.
    """
    from robovast.results_processing import index_scope

    index.execute('CREATE TABLE "no_scope_here" (v double precision)')

    assert index_scope.table_is_secured(index, "no_scope_here")
    assert not index_scope.secure_table_if_needed(index, "no_scope_here")


@pg
def test_a_table_that_does_not_exist_needs_no_repair(index):
    from robovast.results_processing import index_scope

    assert index_scope.table_is_secured(index, "never_created")
