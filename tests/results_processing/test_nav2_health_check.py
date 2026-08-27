# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""nav2's control-loop-rate check: the first producer for ``run_health``.

The metric is nav2's own -- ``controller_server`` reporting it could not hold the frequency
it declared for itself. Measured across a sizing sweep it separated a good allocation from a
bad one 12x, where the throttle counter moved 1.4x over the same range and was not monotone
against it.
"""

import sqlite3

import pytest

from robovast.results_processing.health_checks.nav2 import (CHECK_NAME, CONTROL_LOOP_MISS,
                                                            ERROR_AT, ControlLoopRate)
from robovast.results_processing.run_health import (LEVELS, TABLE,
                                                    build_run_health_table)

_LINE = f"[controller_server]: {CONTROL_LOOP_MISS}, achieved 12.4Hz"


def _db(runs, log_rows):
    """*log_rows* are ``(config_name, run_id, message, in_window, sim_time)``."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER)")
    conn.executemany("INSERT INTO runs VALUES (?,?)", runs)
    conn.execute("CREATE TABLE run_log (config_name TEXT, run_id INTEGER, message TEXT, "
                 "in_window INTEGER, sim_time REAL)")
    conn.executemany("INSERT INTO run_log VALUES (?,?,?,?,?)", log_rows)
    conn.commit()
    return conn


def _by_run(rows):
    return {(r.config_name, r.run_id): r for r in rows}


# -- what it counts -------------------------------------------------------------------------

def test_a_clean_run_gets_an_ok_ROW():
    """Rule 2, and the reason it matters here: a run that never missed and a run whose log was
    never converted must not look alike. Only the first is evidence."""
    rows = ControlLoopRate()(_db([("goal-1", 0)], []))
    assert len(rows) == 1
    assert rows[0].level == "ok" and rows[0].value == 0.0
    assert rows[0].check == CHECK_NAME


def test_misses_are_counted_and_carried_as_a_measure():
    """``value`` is the point of the contract: a finding says bad, a measure says how bad, and
    a resource floor is found in the knee of a curve rather than in a boolean."""
    conn = _db([("goal-1", 0)],
               [("goal-1", 0, _LINE, 1, 5.0)] * 3)
    row = ControlLoopRate()(conn)[0]
    assert row.value == 3.0 and row.unit == "misses" and row.level == "warn"


def test_a_sustained_loss_is_an_error():
    """Ten, to agree with the ``repeat(10)`` debounce the scenario idiom uses on the same
    string. A post-hoc check disagreeing with the live one about the same evidence would be
    the second oracle rule 1 forbids."""
    conn = _db([("goal-1", 0)], [("goal-1", 0, _LINE, 1, 5.0)] * ERROR_AT)
    assert ControlLoopRate()(conn)[0].level == "error"


def test_every_level_it_emits_is_one_the_substrate_understands():
    conn = _db([("a", 0), ("b", 0), ("c", 0)],
               [("b", 0, _LINE, 1, 5.0)] + [("c", 0, _LINE, 1, 5.0)] * ERROR_AT)
    assert {r.level for r in ControlLoopRate()(conn)} <= set(LEVELS)


# -- what it must NOT count -----------------------------------------------------------------

def test_lines_outside_the_trial_window_are_not_counted():
    """Bring-up and teardown -- and in a packed job, another run's lines entirely."""
    conn = _db([("goal-1", 0)], [("goal-1", 0, _LINE, 0, 5.0)] * 4)
    assert ControlLoopRate()(conn)[0].level == "ok"


def test_lines_logged_before_the_clock_existed_are_not_counted():
    """``sim_time`` NULL means the simulator's clock was not up, so the stack was not yet
    running against a simulated world and a missed rate says nothing about the trial."""
    conn = _db([("goal-1", 0)], [("goal-1", 0, _LINE, 1, None)] * 4)
    assert ControlLoopRate()(conn)[0].level == "ok"


def test_other_log_lines_are_not_counted():
    conn = _db([("goal-1", 0)],
               [("goal-1", 0, "[controller_server]: Passing new path to controller.", 1, 5.0)])
    assert ControlLoopRate()(conn)[0].value == 0.0


def test_counts_do_not_leak_between_runs():
    conn = _db([("goal-1", 0), ("goal-1", 1), ("goal-2", 0)],
               [("goal-1", 0, _LINE, 1, 5.0), ("goal-1", 0, _LINE, 1, 6.0),
                ("goal-2", 0, _LINE, 1, 5.0)])
    got = _by_run(ControlLoopRate()(conn))
    assert got[("goal-1", 0)].value == 2.0
    assert got[("goal-1", 1)].value == 0.0
    assert got[("goal-2", 0)].value == 1.0


# -- absence is not a pass ------------------------------------------------------------------

def test_a_campaign_with_no_run_log_is_not_checked_rather_than_clean():
    """A campaign whose logs were never converted, or one that is not a nav2 stack at all.
    Writing ``ok`` rows would be a clean bill produced from an absent measurement."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER)")
    conn.execute("INSERT INTO runs VALUES ('goal-1', 0)")
    conn.commit()
    assert ControlLoopRate()(conn) == []


# -- it reaches the table -------------------------------------------------------------------

def test_the_rows_land_in_run_health(tmp_path):
    conn = _db([("goal-1", 0), ("goal-1", 1)], [("goal-1", 1, _LINE, 1, 5.0)] * 2)
    written = build_run_health_table(conn, {CHECK_NAME: ControlLoopRate()})
    assert written == 2
    got = dict(conn.execute(
        f"SELECT run_id, level FROM {TABLE} WHERE check_name = ?", (CHECK_NAME,)).fetchall())
    assert got == {0: "ok", 1: "warn"}


def test_it_is_registered_as_an_entry_point():
    """Discovered, never imported by name from run_health -- that indirection is what keeps a
    MoveIt 2 campaign from being graded by nav2's idea of healthy."""
    from robovast.results_processing.run_health import HEALTH_GROUP, load_health_checks

    assert HEALTH_GROUP == "robovast.health_checks"
    checks = load_health_checks()
    assert CHECK_NAME in checks, (
        "not discoverable; the entry point in pyproject.toml may need a reinstall")
    assert isinstance(checks[CHECK_NAME], ControlLoopRate)


# -- the rule it must never break -----------------------------------------------------------

def test_health_never_decides_pass_fail():
    """The scenario's verdict is the verdict; this grades it. Two oracles eventually disagree
    about the same run, which is why nothing here reads or writes a run's status."""
    import inspect

    source = inspect.getsource(ControlLoopRate)
    for forbidden in ("status", "passed", "failed"):
        assert forbidden not in source, (
            f"the check references {forbidden!r}; health must not touch a run's verdict")
