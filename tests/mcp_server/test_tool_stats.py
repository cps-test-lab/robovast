# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The MCP call log: what it keeps, what it cuts, and what it must never do.

Set ``ROBOVAST_TEST_PG_DSN`` to run the ones that need an index; the contract that
matters most -- recording must never fail a tool call -- is checked without one.
"""

import os
import time

import pytest

from robovast.mcp_server import tool_stats

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")

SCHEMA = "tool_stats_test"


@pytest.fixture(name="log")
def _log(monkeypatch):
    """A fresh log against an empty index schema, env pointed at it as a deployment would."""
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        setup.execute(f"CREATE SCHEMA {SCHEMA}")
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    yield tool_stats.ToolCallLog()
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_a_call_is_recorded_and_read_back(log):
    log.record("list_campaigns", 12.5, True, args='{"limit": 5}', answer="[]")
    log.flush()

    calls = log.read_calls()
    assert len(calls) == 1
    assert calls[0].tool == "list_campaigns"
    assert calls[0].ok is True
    assert calls[0].args == '{"limit": 5}'
    assert calls[0].answer == "[]"


def test_the_ranking_aggregates_over_the_log(log):
    for duration in (10.0, 30.0):
        log.record("search_docs", duration, True)
    log.record("search_docs", 50.0, False, answer="ValueError: nope")
    log.record("read_file", 5.0, True)
    log.flush()

    stats = {s.tool: s for s in log.read_stats()}
    assert stats["search_docs"].calls == 3
    assert stats["search_docs"].errors == 1
    assert stats["search_docs"].mean_ms == pytest.approx(30.0)
    assert stats["search_docs"].max_ms == pytest.approx(50.0)
    # Busiest first: the ranking is the panel's order, not the reader's job.
    assert [s.tool for s in log.read_stats()][0] == "search_docs"


def test_a_failed_call_keeps_what_it_was_given_and_why_it_failed(log):
    log.record("write_file", 3.0, False, args='{"path": "x"}',
               answer="FileNotFoundError: no such workspace")
    log.flush()

    failed = log.read_calls(failed_only=True)
    assert len(failed) == 1
    assert "FileNotFoundError" in failed[0].answer
    assert failed[0].args == '{"path": "x"}', (
        "the arguments are the point of keeping per-call rows: a count cannot be debugged")


def test_the_log_filters_to_one_tool(log):
    log.record("a", 1.0, True)
    log.record("b", 1.0, True)
    log.flush()
    assert [c.tool for c in log.read_calls(tool="b")] == ["b"]


def test_an_index_with_no_table_yet_reads_as_an_empty_log(log):
    # Nothing has been flushed, so the table does not exist. That is an empty log, and a
    # panel must be able to tell it from an error.
    assert log.read_calls() == []
    assert log.read_stats() == []


def test_pruning_drops_rows_past_the_age_and_the_row_cap(log, monkeypatch):
    monkeypatch.setattr(tool_stats, "MAX_ROWS", 2)
    log.record("old", 1.0, True)
    log.flush()
    from robovast.common import index_db
    with index_db.connect() as conn:
        conn.execute(f"UPDATE {tool_stats.TABLE} SET at = %s",
                     (time.time() - tool_stats.MAX_AGE_S - 1,))
    for name in ("new1", "new2", "new3"):
        log.record(name, 1.0, True)
    log.flush()
    with index_db.connect() as conn:
        log._prune(conn)  # pylint: disable=protected-access

    kept = {c.tool for c in log.read_calls()}
    assert "old" not in kept, "a row past MAX_AGE_S must go"
    assert len(kept) == 2, "the row cap is the backstop when age alone does not bound it"


def test_recording_never_raises_when_the_index_is_unreachable(monkeypatch):
    """The one contract that outranks the record: accounting must not fail a tool call."""
    from robovast.common import index_db
    monkeypatch.delenv(index_db.DSN_ENV, raising=False)

    log = tool_stats.ToolCallLog()
    log.record("list_campaigns", 1.0, True)
    assert log.flush() == 0


def test_a_long_answer_is_cut_and_says_so():
    long_answer = "\n".join(f"line {i}" for i in range(50))
    assert tool_stats.shorten(long_answer).endswith(tool_stats.ELISION)
    assert tool_stats.shorten(long_answer).count("\n") == tool_stats.MAX_LINES - 1

    wide = "x" * (tool_stats.MAX_CHARS * 2)
    cut = tool_stats.shorten(wide)
    assert len(cut) == tool_stats.MAX_CHARS + len(tool_stats.ELISION)

    assert tool_stats.shorten("short") == "short", "an untruncated value gains no marker"


def test_a_string_answer_keeps_its_lines_so_the_line_cap_can_bite():
    """The defect this guards: `server._short` collapses everything to one 400-char line.

    Recording through it made the line cap unreachable and "a few lines" a promise nothing
    kept, so the middleware hands the payload over uncut and this module decides.
    """
    rendered = tool_stats.render("\n".join(f"row {i}" for i in range(200)))
    assert rendered.count("\n") == 199

    kept = tool_stats.shorten(rendered)
    assert kept.count("\n") == tool_stats.MAX_LINES - 1
    assert kept.endswith(tool_stats.ELISION)


def test_arguments_render_as_compact_json():
    assert tool_stats.render({"limit": 3}) == '{"limit": 3}'


def test_an_unserialisable_value_still_renders():
    class Odd:
        def __repr__(self):
            return "<odd>"

    assert "odd" in tool_stats.render({"x": Odd()})


def test_pending_rows_are_written_at_exit(log, monkeypatch):
    """A service rolled seconds after a call must not take that call down with it.

    The buffer is flushed on every read, so anything anyone looks at is current; the gap
    this closes is the one nobody looked at before the process went away.
    """
    monkeypatch.setattr(tool_stats, "LOG", log)
    log.record("list_campaigns", 1.0, True)
    assert log._buffer, "the call is still pending"  # pylint: disable=protected-access

    # What atexit.register(LOG.flush) runs at shutdown.
    tool_stats.LOG.flush()

    assert [c.tool for c in log.read_calls()] == ["list_campaigns"]
