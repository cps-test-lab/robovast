# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The SQL `search_run_logs` builds, executed rather than string-matched.

A WHERE term is a raw string until something runs it: a typo, a wrong alias or a join
that silently matches nothing all look identical in a diff and all return "no hits",
which reads as a healthy sweep.
"""

import sqlite3

import pytest

from robovast.mcp_server.plugins.run_logs import _campaign_term, _shutdown_term

#: The campaign under test, and a second one sharing its configuration names -- the index
#: holds every campaign in one table, so a term that forgets ``campaign_id`` reads both.
_CAMPAIGN = "camp-a"
_OTHER = "camp-b"

# (campaign_id, config_name, run_id, wall_ts, message)
_LOG_ROWS = [
    (_CAMPAIGN, "cfg-a", 0, 100.0, "goal reached"),
    (_CAMPAIGN, "cfg-a", 0, 101.0, "Scenario 'trial' succeeded."),
    (_CAMPAIGN, "cfg-a", 0, 102.0, "transform failure"),          # shutdown
    (_CAMPAIGN, "cfg-b", 0, 200.0, "goal reached"),
    (_CAMPAIGN, "cfg-b", 0, 201.0, "Unable to start transition"),  # no verdict for this run
    (_CAMPAIGN, "cfg-c", 0, 300.0, "goal reached"),
    (_CAMPAIGN, "cfg-c", 0, 301.0, "transform failure"),           # verdict has no wall_ts
    (_OTHER, "cfg-a", 0, 400.0, "another campaign's line"),
]

# (campaign_id, config_name, run_id, wall_ts)
_VERDICTS = [(_CAMPAIGN, "cfg-a", 0, 101.0), (_CAMPAIGN, "cfg-c", 0, None),
             # Earlier than every line of camp-a's cfg-b: an uncorrelated term would
             # let this campaign's verdict trim the other campaign's log.
             (_OTHER, "cfg-b", 0, 1.0)]


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE run_log (campaign_id TEXT, config_name TEXT, "
                 "run_id INTEGER, wall_ts REAL, message TEXT)")
    conn.execute("CREATE TABLE scenario_timestamps (campaign_id TEXT, config_name TEXT, "
                 "run_id INTEGER, wall_ts REAL)")
    conn.executemany("INSERT INTO run_log VALUES (?, ?, ?, ?, ?)", _LOG_ROWS)
    conn.executemany("INSERT INTO scenario_timestamps VALUES (?, ?, ?, ?)", _VERDICTS)
    return conn


def _messages(conn: sqlite3.Connection) -> list:
    sql = (f"SELECT l.message FROM run_log l WHERE {_campaign_term(_CAMPAIGN)} "
           f"AND {_shutdown_term()} ORDER BY l.wall_ts")
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


def test_the_search_reads_only_the_campaign_it_was_asked_about(db):
    """One index holds every campaign's log, so scoping is the query's job. Without the
    campaign term a search of one campaign silently returns the corpus."""
    assert "another campaign's line" not in _messages(db)


def test_the_verdict_lookup_is_correlated_on_the_campaign_too(db):
    """camp-b records a verdict at wall_ts 1.0 for cfg-b. Correlated only on
    (config_name, run_id), it would trim every one of camp-a's cfg-b lines."""
    assert "Unable to start transition" in _messages(db)
