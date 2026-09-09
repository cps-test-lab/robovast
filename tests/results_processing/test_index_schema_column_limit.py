# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``ensure_table`` refuses a too-wide table before any DDL reaches Postgres.

Unlike ``test_index_schema.py`` this needs no real database: the whole point of the
guard is that it never gets as far as a connection, so a connection stub that fails the
test the moment anything is executed against it is *more* precise than a real one would
be here, not less.
"""

import contextlib

import pytest

from robovast.common.errors import TableColumnLimitExceeded
from robovast.results_processing import index_schema
from robovast.results_processing.csv_types import REAL


class _ExplodingConn:
    """A connection stub that fails the test if any SQL reaches it.

    The guard's whole point is to run before ``CREATE``/``ALTER`` is ever sent -- a
    doomed statement that reached Postgres would abort the surrounding transaction and
    could take an otherwise-healthy table down with it. This stub makes "never sent"
    part of what the test checks, not an assumption behind it.
    """

    def execute(self, *_a, **_kw):
        raise AssertionError("no DDL may be sent once the column count is refused")

    def cursor(self):
        raise AssertionError("no DDL may be sent once the column count is refused")


def _wide_types(n):
    return {f"col_{i}": REAL for i in range(n)}


def test_a_new_table_past_the_limit_is_refused_before_any_ddl(monkeypatch):
    monkeypatch.setattr(index_schema, "ensure_metadata_tables", lambda _conn: None)
    monkeypatch.setattr(index_schema, "read_verdicts", lambda *_a, **_kw: {})

    with pytest.raises(TableColumnLimitExceeded) as excinfo:
        index_schema.ensure_table(_ExplodingConn(), "costmap",
                                  _wide_types(index_schema._MAX_TABLE_COLUMNS + 1),  # noqa: SLF001
                                  schema="")

    message = str(excinfo.value)
    assert "costmap" in message
    assert "costmap_to_csv" in message or "flatten" in message, \
        "must point toward the fix, not just report the number"


def test_a_new_table_at_exactly_the_limit_is_accepted(monkeypatch):
    """The boundary itself must not be refused -- only strictly past it."""
    calls = []
    monkeypatch.setattr(index_schema, "ensure_metadata_tables", lambda _conn: None)
    monkeypatch.setattr(index_schema, "read_verdicts", lambda *_a, **_kw: {})
    monkeypatch.setattr(index_schema, "_record_verdict", lambda *_a, **_kw: None)
    monkeypatch.setattr(index_schema, "_scope",
                        lambda: type("S", (), {"secure_table": lambda self, *a, **k: None})())

    class _RecordingConn:
        def execute(self, sql, *_a, **_kw):
            calls.append(sql)

        # Creating a table and scoping it is one transaction (see ``ensure_table``), so a
        # stand-in for the connection has to offer one. It does nothing: what this test
        # watches is the DDL, and a stub that recorded a rollback would be asserting on
        # the stub.
        def transaction(self):
            @contextlib.contextmanager
            def _noop():
                yield

            return _noop()

    # 3 context columns are always prepended, so the data columns must leave headroom.
    n = index_schema._MAX_TABLE_COLUMNS - len(index_schema.CONTEXT_COLUMNS)  # noqa: SLF001
    index_schema.ensure_table(_RecordingConn(), "poses", _wide_types(n), schema="")

    assert any("CREATE TABLE" in c for c in calls), "at the limit, the table must be created"


def test_widening_an_existing_table_past_the_limit_is_also_refused(monkeypatch):
    """The second ingest of a stem (an ``ALTER ... ADD COLUMN`` batch) is guarded too --
    not only the first ``CREATE TABLE``."""
    monkeypatch.setattr(index_schema, "ensure_metadata_tables", lambda _conn: None)
    already = {f"col_{i}": REAL for i in range(index_schema._MAX_TABLE_COLUMNS - 1)}  # noqa: SLF001
    monkeypatch.setattr(index_schema, "read_verdicts", lambda *_a, **_kw: already)
    # Distinct names from `already`: the point is columns genuinely NEW to this table,
    # not ones it already has (which would need no ALTER and so trip no guard).
    new_types = {f"new_col_{i}": REAL for i in range(5)}

    with pytest.raises(TableColumnLimitExceeded):
        index_schema.ensure_table(_ExplodingConn(), "poses", new_types, schema="")
