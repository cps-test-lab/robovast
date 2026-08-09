# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The SQL `search_run_logs` builds, executed rather than string-matched.

A WHERE term is a raw string until something runs it: a typo, a wrong alias or a join
that silently matches nothing all look identical in a diff and all return "no hits",
which reads as a healthy sweep.
"""

import sqlite3

import pytest

from robovast.mcp_server.plugins.run_logs import _shutdown_term

# (config_name, run_id, wall_ts, message)
_LOG_ROWS = [
    ("cfg-a", 0, 100.0, "goal reached"),
    ("cfg-a", 0, 101.0, "Scenario 'trial' succeeded."),
    ("cfg-a", 0, 102.0, "transform failure"),          # shutdown
    ("cfg-b", 0, 200.0, "goal reached"),
    ("cfg-b", 0, 201.0, "Unable to start transition"),  # no verdict for this run
    ("cfg-c", 0, 300.0, "goal reached"),
    ("cfg-c", 0, 301.0, "transform failure"),           # verdict has no wall_ts
]

# (config_name, run_id, wall_ts)
_VERDICTS = [("cfg-a", 0, 101.0), ("cfg-c", 0, None)]


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE run_log (config_name TEXT, run_id INTEGER, "
                 "wall_ts REAL, message TEXT)")
    conn.execute("CREATE TABLE scenario_timestamps (config_name TEXT, run_id INTEGER, "
                 "wall_ts REAL)")
    conn.executemany("INSERT INTO run_log VALUES (?, ?, ?, ?)", _LOG_ROWS)
    conn.executemany("INSERT INTO scenario_timestamps VALUES (?, ?, ?)", _VERDICTS)
    return conn


def _messages(conn: sqlite3.Connection) -> list:
    sql = f"SELECT l.message FROM run_log l WHERE {_shutdown_term(0)} ORDER BY l.wall_ts"
    return [r[0] for r in conn.execute(sql)]


def test_the_term_is_valid_sql_and_keeps_the_trial(db):
    assert "goal reached" in _messages(db)


def test_a_line_after_the_verdict_is_excluded_but_the_verdict_itself_stays(db):
    """Strict `>`: the verdict line carries the failure snapshot that explains it, so
    excluding it would delete the diagnosis a reader came for."""
    kept = _messages(db)
    assert "Scenario 'trial' succeeded." in kept
    assert "transform failure" not in kept[:3]


def test_a_run_with_no_recorded_verdict_is_not_trimmed(db):
    """`cfg-b` has no row at all. Trimming it to some other run's verdict, or to
    nothing, would answer a different question than the one asked."""
    assert "Unable to start transition" in _messages(db)


def test_a_verdict_with_no_wall_time_trims_nothing(db):
    """`cfg-c` reached a verdict the log could not place. Trimming to an invented
    moment is worse than not trimming; the NULL guard is what stops the NOT EXISTS
    from silently matching."""
    assert _messages(db).count("transform failure") == 1  # cfg-c's, not cfg-a's


def test_the_term_scopes_per_run_not_across_the_table(db):
    """Without the (config_name, run_id) correlation, cfg-a's verdict at 101.0 would
    exclude every later line in the campaign -- cfg-b's and cfg-c's included."""
    kept = _messages(db)
    assert "goal reached" in kept
    assert len([m for m in kept if m == "goal reached"]) == 3


def test_the_attached_schema_prefix_reaches_the_verdict_table():
    """A cross-campaign search attaches the others as c1., c2.… The term names a table,
    so it must carry that prefix -- unprefixed it would read the primary campaign's
    verdicts while filtering an attached campaign's log."""
    assert "c2.scenario_timestamps" in _shutdown_term(2)
    assert "scenario_timestamps" in _shutdown_term(0)
    assert "c0." not in _shutdown_term(0)
