# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""nav2's control-loop-rate check: the first producer for ``run_health``.

The metric is nav2's own -- ``controller_server`` reporting it could not hold the frequency
it declared for itself. Measured across a sizing sweep it separated a good allocation from a
bad one 12x, where the throttle counter moved 1.4x over the same range and was not monotone
against it.

It now reads the central index, so every case here is also a case about scoping: the tables
hold every campaign, and a count that leaks across campaigns is the same bug as a count that
leaks across runs, one level up.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import os

import pytest

from robovast.results_processing.run_health import (LEVELS, TABLE,
                                                    build_run_health_table)
from robovast.results_processing.row_sink import PostgresRowSink
from robovast_nav.health_checks import CHECK_NAME, CONTROL_LOOP_MISS, ERROR_AT, ControlLoopRate

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "nav2_health_test"
CAMPAIGN = "camp-a"

_LINE = f"[controller_server]: {CONTROL_LOOP_MISS}, achieved 12.4Hz"


@pytest.fixture(name="conn")
def _conn():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}", f"SET search_path TO {SCHEMA}"):
            conn.execute(statement)
        yield conn
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _runs_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS runs (campaign_id text, config_name text, "
                 "run_id bigint)")


def _log_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS run_log (campaign_id text, config_name text, "
                 "run_id bigint, message text, in_window bigint, "
                 "sim_time double precision)")


def _db(conn, runs, log_rows, campaign_id=CAMPAIGN):
    """*runs* are ``(config_name, run_id)``; *log_rows* are
    ``(config_name, run_id, message, in_window, sim_time)``. Both are filed under
    *campaign_id*, which is what the check has to scope to."""
    _runs_table(conn)
    _log_table(conn)
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO runs VALUES (%s,%s,%s)",
                        [(campaign_id, *r) for r in runs])
        cur.executemany("INSERT INTO run_log VALUES (%s,%s,%s,%s,%s,%s)",
                        [(campaign_id, *r) for r in log_rows])
    return conn


def _by_run(rows):
    return {(r.config_name, r.run_id): r for r in rows}


# -- what it counts -------------------------------------------------------------------------

def test_a_clean_run_gets_an_ok_row(conn):
    """Rule 2, and the reason it matters here: a run that never missed and a run whose log was
    never converted must not look alike. Only the first is evidence."""
    rows = ControlLoopRate()(_db(conn, [("goal-1", 0)], []), CAMPAIGN)
    assert len(rows) == 1
    assert rows[0].level == "ok" and rows[0].value == 0.0
    assert rows[0].check == CHECK_NAME


def test_misses_are_counted_and_carried_as_a_measure(conn):
    """``value`` is the point of the contract: a finding says bad, a measure says how bad, and
    a resource floor is found in the knee of a curve rather than in a boolean."""
    _db(conn, [("goal-1", 0)],
               [("goal-1", 0, _LINE, 1, 5.0)] * 3)
    row = ControlLoopRate()(conn, CAMPAIGN)[0]
    assert row.value == 3.0 and row.unit == "misses" and row.level == "warn"


def test_a_sustained_loss_is_an_error(conn):
    """Ten, to agree with the ``repeat(10)`` debounce the scenario idiom uses on the same
    string. A post-hoc check disagreeing with the live one about the same evidence would be
    the second oracle rule 1 forbids."""
    _db(conn, [("goal-1", 0)], [("goal-1", 0, _LINE, 1, 5.0)] * ERROR_AT)
    assert ControlLoopRate()(conn, CAMPAIGN)[0].level == "error"


def test_every_level_it_emits_is_one_the_substrate_understands(conn):
    _db(conn, [("a", 0), ("b", 0), ("c", 0)],
               [("b", 0, _LINE, 1, 5.0)] + [("c", 0, _LINE, 1, 5.0)] * ERROR_AT)
    assert {r.level for r in ControlLoopRate()(conn, CAMPAIGN)} <= set(LEVELS)


# -- what it must NOT count -----------------------------------------------------------------

def test_lines_outside_the_trial_window_are_not_counted(conn):
    """Bring-up and teardown -- and in a packed job, another run's lines entirely."""
    _db(conn, [("goal-1", 0)], [("goal-1", 0, _LINE, 0, 5.0)] * 4)
    assert ControlLoopRate()(conn, CAMPAIGN)[0].level == "ok"


def test_lines_logged_before_the_clock_existed_are_not_counted(conn):
    """``sim_time`` NULL means the simulator's clock was not up, so the stack was not yet
    running against a simulated world and a missed rate says nothing about the trial."""
    _db(conn, [("goal-1", 0)], [("goal-1", 0, _LINE, 1, None)] * 4)
    assert ControlLoopRate()(conn, CAMPAIGN)[0].level == "ok"


def test_other_log_lines_are_not_counted(conn):
    _db(conn, [("goal-1", 0)],
               [("goal-1", 0, "[controller_server]: Passing new path to controller.", 1, 5.0)])
    assert ControlLoopRate()(conn, CAMPAIGN)[0].value == 0.0


def test_counts_do_not_leak_between_runs(conn):
    _db(conn, [("goal-1", 0), ("goal-1", 1), ("goal-2", 0)],
               [("goal-1", 0, _LINE, 1, 5.0), ("goal-1", 0, _LINE, 1, 6.0),
                ("goal-2", 0, _LINE, 1, 5.0)])
    got = _by_run(ControlLoopRate()(conn, CAMPAIGN))
    assert got[("goal-1", 0)].value == 2.0
    assert got[("goal-1", 1)].value == 0.0
    assert got[("goal-2", 0)].value == 1.0


# -- absence is not a pass ------------------------------------------------------------------

def test_a_campaign_with_no_run_log_is_not_checked_rather_than_clean(conn):
    """A campaign whose logs were never converted, or one that is not a nav2 stack at all.
    Writing ``ok`` rows would be a clean bill produced from an absent measurement.

    Asked with ``to_regclass`` rather than by letting the query fail: on a non-autocommit
    connection a failed statement aborts the surrounding transaction, so a check probing by
    exception would take the rest of the ingest down with it.
    """
    _runs_table(conn)
    conn.execute("INSERT INTO runs VALUES (%s, 'goal-1', 0)", (CAMPAIGN,))
    assert ControlLoopRate()(conn, CAMPAIGN) == []


def test_another_campaigns_runs_are_never_counted_or_graded(conn):
    """The scoping the new signature exists for. One index holds every campaign, so an
    unscoped count files a neighbour's misses under this campaign's runs -- and would grade
    runs this check was never asked about."""
    _db(conn, [("goal-1", 0)], [("goal-1", 0, _LINE, 1, 5.0)])
    _db(conn, [("goal-1", 0), ("goal-9", 0)],
        [("goal-1", 0, _LINE, 1, 5.0)] * ERROR_AT, campaign_id="camp-b")

    rows = ControlLoopRate()(conn, CAMPAIGN)
    assert [(r.config_name, r.run_id, r.value) for r in rows] == [("goal-1", 0, 1.0)]


# -- it reaches the table -------------------------------------------------------------------

def test_the_rows_land_in_run_health(conn):
    _db(conn, [("goal-1", 0), ("goal-1", 1)], [("goal-1", 1, _LINE, 1, 5.0)] * 2)
    sink = PostgresRowSink(conn, campaign_id=CAMPAIGN)
    written = build_run_health_table(sink, conn, CAMPAIGN,
                                     {CHECK_NAME: ControlLoopRate()})
    assert written == 2
    got = dict(conn.execute(
        f"SELECT run_id, level FROM {TABLE} WHERE campaign_id = %s AND check_name = %s",
        (CAMPAIGN, CHECK_NAME)).fetchall())
    assert got == {0: "ok", 1: "warn"}


def test_it_is_resolvable_by_name_when_declared():
    """Discovered through the entry point, never imported by name from run_health -- that
    indirection is what keeps a MoveIt 2 campaign from being graded by nav2's idea of
    healthy."""
    from robovast.results_processing.run_health import HEALTH_GROUP, load_health_checks

    assert HEALTH_GROUP == "robovast.health_checks"
    checks = load_health_checks([CHECK_NAME])
    assert CHECK_NAME in checks, (
        "not discoverable; the entry point in pyproject.toml may need a reinstall")
    assert isinstance(checks[CHECK_NAME], ControlLoopRate)


def test_nothing_runs_undeclared():
    """The rule this check must not escape. A check that ran everywhere would grade campaigns
    it knows nothing about: this one finds no misses in a MoveIt 2 campaign and would write
    ``ok`` for every run of it -- a clean bill for a stack that was never there, which is the
    confusion rule 2 exists to prevent arriving through the mechanism meant to serve it."""
    from robovast.results_processing.run_health import load_health_checks

    assert load_health_checks() == {}
    assert load_health_checks([]) == {}


def test_a_declared_check_that_is_not_installed_is_reported(caplog):
    """Loud, and it must stay loud: a declared check that silently did not run leaves no
    rows, and no rows means "not checked" -- so the campaign reads as ungraded rather than as
    misconfigured."""
    import logging

    from robovast.results_processing.run_health import load_health_checks

    with caplog.at_level(logging.WARNING):
        assert load_health_checks(["no_such_check"]) == {}
    assert "no_such_check" in caplog.text


def test_every_shipped_nav2_example_declares_it():
    """The examples are documentation. One that ships without the declaration teaches the
    reader that grading happens by itself, which is exactly what stopped being true."""
    import glob

    import yaml

    examples = sorted(glob.glob("configs/examples/*/*.vast"))
    nav2 = [f for f in examples if "nav2" in open(f, encoding="utf-8").read()]
    assert nav2, "no nav2 examples found; the glob or the layout changed"
    missing = [f for f in nav2
               if CHECK_NAME not in ((yaml.safe_load(open(f, encoding="utf-8"))
                                      .get("results_processing") or {}).get("health_checks")
                                     or [])]
    assert not missing, f"nav2 examples not declaring {CHECK_NAME}: {missing}"



# -- the rule it must never break -----------------------------------------------------------

def test_health_never_decides_pass_fail():
    """The scenario's verdict is the verdict; this grades it. Two oracles eventually disagree
    about the same run, which is why nothing here reads or writes a run's status."""
    import inspect

    source = inspect.getsource(ControlLoopRate)
    for forbidden in ("status", "passed", "failed"):
        assert forbidden not in source, (
            f"the check references {forbidden!r}; health must not touch a run's verdict")
