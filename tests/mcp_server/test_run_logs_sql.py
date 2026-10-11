# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The SQL `search_run_logs` builds, executed rather than string-matched.

A WHERE term is a raw string until something runs it: a typo, a wrong alias or a join
that silently matches nothing all look identical in a diff and all return "no hits",
which reads as a healthy sweep. Run on DuckDB with the query engine's own ``REGEXP``, so a
pattern RE2 rejects fails here as it would for a caller.
"""

import duckdb
import pytest

from robovast.mcp_server.plugins.run_logs import (_glob_to_regex, _predicates, _rollup_sql,
                                                  _shutdown_term)
from robovast_data.engine import _MACROS

# (config_name, run_id, seq, wall_ts, message)
_LOG_ROWS = [
    ("cfg-a", 0, 0, 100.0, "goal reached"),
    ("cfg-a", 0, 1, 101.0, "Scenario 'trial' succeeded."),
    ("cfg-a", 0, 2, 102.0, "transform failure"),          # shutdown
    ("cfg-b", 0, 0, 200.0, "goal reached"),
    ("cfg-b", 0, 1, 201.0, "Unable to start transition"),  # no verdict for this run
    ("cfg-c", 0, 0, 300.0, "goal reached"),
    ("cfg-c", 0, 1, 301.0, "transform failure"),           # verdict has no wall_ts
]

# (config_name, run_id, wall_ts)
_VERDICTS = [("cfg-a", 0, 101.0), ("cfg-c", 0, None)]


@pytest.fixture
def db():
    conn = duckdb.connect()
    for macro in _MACROS:
        conn.execute(macro)
    conn.execute("CREATE TABLE run_log (config_name TEXT, run_id INTEGER, seq INTEGER, "
                 "wall_ts DOUBLE, message TEXT, severity TEXT, sim_time DOUBLE)")
    conn.execute("CREATE TABLE scenario_timestamps (config_name TEXT, run_id INTEGER, "
                 "wall_ts DOUBLE)")
    conn.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER, passed INTEGER, "
                 "status TEXT)")
    conn.execute("CREATE TABLE run_clock (config_name TEXT, run_id INTEGER, "
                 "clock_map_source TEXT)")
    conn.executemany("INSERT INTO run_log VALUES (?, ?, ?, ?, ?, 'other', NULL)", _LOG_ROWS)
    conn.executemany("INSERT INTO scenario_timestamps VALUES (?, ?, ?)", _VERDICTS)
    conn.executemany("INSERT INTO runs VALUES (?, 0, ?, ?)",
                     [("cfg-a", 1, "passed"), ("cfg-b", 0, "failed")])
    conn.execute("INSERT INTO run_clock VALUES ('cfg-a', 0, 'ros_clock_bag')")
    return conn


def _messages(conn) -> list:
    sql = (f"SELECT l.message FROM run_log l WHERE {_shutdown_term()} "
           "ORDER BY l.config_name, l.run_id, l.seq")
    return [r[0] for r in conn.execute(sql).fetchall()]


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
    assert _messages(db).count("goal reached") == 3


def _config_term(config_filter: str) -> str:
    """The one WHERE term `config_filter` contributes."""
    terms = _predicates(grep="", min_severity="", config_filter=config_filter, run_id=None,
                        container="", node="", source="", t0=None, t1=None, in_window=None)
    assert len(terms) == 1
    return terms[0]


@pytest.mark.parametrize("config_filter, name, matches", [
    ("probe-*", "probe-a", True),
    ("probe-*", "other-probe-a", False),   # the pattern anchors at the start
    ("*-1", "probe-1", True),
    ("*-1", "probe-11", False),            # ... and at the end
    ("probe-?", "probe-a", True),
    ("probe-?", "probe-ab", False),
    ("probe [a]*", "probe a.b", True),     # escaped space and a class survive RE2
    ("[!x]-1", "y-1", True),
    ("[!x]-1", "x-1", False),
])
def test_the_config_filter_matches_the_campaign_glob_vocabulary(db, config_filter, name,
                                                                matches):
    """`config_filter` is documented as the glob the campaign tools take, so `*` and `?`
    have to keep meaning what they do there -- and anchored at BOTH ends, since a filter
    that also selects `config-11` reports another configuration's runs as this one's."""
    db.execute("INSERT INTO run_log VALUES (?, 0, 0, 500.0, 'filtered line', 'other', NULL)",
               [name])
    rows = db.execute(f"SELECT l.message FROM run_log l WHERE "
                      f"{_config_term(config_filter)}").fetchall()
    assert (["filtered line"] == [r[0] for r in rows]) is matches


def test_the_config_filter_is_anchored_without_an_anchor_re2_rejects():
    """``\\Z`` is Python's end anchor and RE2 refuses it, so a glob built with it fails every
    filtered search outright."""
    pattern = _glob_to_regex("cfg-*")
    assert pattern.startswith("^") and pattern.endswith("$")
    assert "\\Z" not in pattern


def test_the_rollup_runs_and_joins_the_verdict_and_the_clock(db):
    """The default call's SQL, executed: every selected run column is grouped or aggregated,
    and a run with no ``runs`` or ``run_clock`` row still reports its hits."""
    rows = db.execute(_rollup_sql([], limit=10)).fetchall()
    columns = [d[0] for d in db.description]
    by_config = {r[columns.index("config_name")]: dict(zip(columns, r)) for r in rows}
    assert by_config["cfg-a"]["clock_map_source"] == "ros_clock_bag"
    assert by_config["cfg-a"]["status"] == "passed"
    assert by_config["cfg-c"]["hits"] == 2 and by_config["cfg-c"]["status"] is None
