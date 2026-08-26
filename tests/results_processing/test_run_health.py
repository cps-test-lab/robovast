# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The run-health contract: RoboVAST learns one word, and absence is never a pass."""

import sqlite3

import pytest

from robovast.results_processing.run_health import (LEVELS, HealthRow, build_run_health_table,
                                                    load_health_checks)


@pytest.fixture(name="conn")
def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM run_health ORDER BY run_id, check_name")]


def test_the_table_exists_even_when_nothing_fills_it(conn):
    """An absent table and an empty one say different things -- "this campaign predates health
    checks" versus "they ran and found nothing" -- and only the second is evidence."""
    assert build_run_health_table(conn, {}) == 0
    assert _rows(conn) == []
    conn.execute("SELECT 1 FROM run_health")  # exists, rather than raising


def test_a_measure_survives_the_round_trip(conn):
    """``value``/``unit`` are the addition to the live finding contract and the point of it:
    a finding says bad, a measure says how bad, and a floor is a knee in a curve."""
    def check(_conn):
        return [HealthRow("cfg-a", 0, "control_loop_misses", "warn",
                          detail="60 misses in window", value=60, unit="count")]

    assert build_run_health_table(conn, {"nav2": check}) == 1
    row, = _rows(conn)
    assert (row["check_name"], row["level"], row["value"], row["unit"]) == \
        ("control_loop_misses", "warn", 60.0, "count")
    assert row["source"] == "stack"


def test_ok_is_a_row_because_absence_means_not_checked(conn):
    """The rule that makes the table readable at all. A clean run must SAY it was checked;
    otherwise "no row" covers both "healthy" and "no plugin installed", and the two have
    opposite consequences for a conclusion drawn from the campaign."""
    def check(_conn):
        return [HealthRow("cfg-a", 0, "solve_failures", "ok", value=0, unit="count"),
                HealthRow("cfg-a", 1, "solve_failures", "error", value=9, unit="count")]

    build_run_health_table(conn, {"moveit": check})
    assert [r["level"] for r in _rows(conn)] == ["ok", "error"]


def test_a_level_robovast_cannot_interpret_is_dropped_and_said_out_loud(conn, caplog):
    """RoboVAST interprets exactly one word. A stack's private vocabulary in that column
    could not be rendered, sorted or compared across stacks, and guessing a mapping would
    put the guess into data readers trust. Dropped loudly, never coerced."""
    def check(_conn):
        return [HealthRow("cfg-a", 0, "x", "CRITICAL"),
                HealthRow("cfg-a", 1, "x", "ok")]

    with caplog.at_level("WARNING"):
        assert build_run_health_table(conn, {"bad": check}) == 1
    assert [r["level"] for r in _rows(conn)] == ["ok"]
    assert "CRITICAL" in caplog.text
    assert all(lvl in LEVELS for lvl in (r["level"] for r in _rows(conn)))


def test_one_failing_check_does_not_cost_the_others_or_the_campaign(conn, caplog):
    """The runs are the deliverable. A stack plugin that raises must not take a campaign's
    data.db with it -- but it must be loud, because a swallowed error leaves rows missing,
    and a missing row reads as "not checked" rather than as a failure."""
    def boom(_conn):
        raise RuntimeError("plugin is broken")

    def fine(_conn):
        return [HealthRow("cfg-a", 0, "y", "ok")]

    with caplog.at_level("WARNING"):
        assert build_run_health_table(conn, {"boom": boom, "fine": fine}) == 1
    assert "plugin is broken" in caplog.text
    assert [r["check_name"] for r in _rows(conn)] == ["y"]


def test_a_plain_mapping_is_accepted_so_a_plugin_need_not_import_us(conn):
    """A stack shipping a check should not have to depend on RoboVAST to describe one run."""
    def check(_conn):
        return [{"config_name": "cfg-a", "run_id": 0, "check": "z", "level": "warn",
                 "detail": "d", "value": 1.5, "unit": "s"}]

    assert build_run_health_table(conn, {"dict": check}) == 1
    assert _rows(conn)[0]["value"] == 1.5


def test_rebuilding_replaces_rather_than_appends(conn):
    """Postprocessing is re-runnable, and a doubled row would double every count drawn from
    this table."""
    def check(_conn):
        return [HealthRow("cfg-a", 0, "z", "ok")]

    build_run_health_table(conn, {"a": check})
    build_run_health_table(conn, {"a": check})
    assert len(_rows(conn)) == 1


def test_an_uninstalled_name_is_reported_rather_than_silently_skipped(caplog):
    """A campaign naming a check it does not have must not read as a campaign with nothing
    to check -- that is rule 2 at the configuration level."""
    with caplog.at_level("WARNING"):
        checks = load_health_checks(declared=["nav2_health_that_is_not_installed"])
    assert "nav2_health_that_is_not_installed" not in checks
    assert "not installed" in caplog.text


def test_health_never_decides_pass_fail():
    """Pinned as a layering fact rather than a behaviour: every statement this module runs
    must target its OWN table.

    Two differently-calibrated oracles eventually disagree about the same run, and the
    scenario's verdict is the one that counts -- so health must be unable to write a run's
    status even by accident. Checked against the SQL it actually issues (prose about
    "passing values through" is not a reference to run_view.passed, which a plain substring
    search cannot tell apart).
    """
    import ast
    import inspect

    from robovast.results_processing import run_health

    tree = ast.parse(inspect.getsource(run_health))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sql = [t for t in literals
           if any(k in t.upper() for k in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                                           "CREATE ", "DROP "))]
    assert sql, "expected to find the module's own SQL"
    for stmt in sql:
        for other in ("run_view", "scenario_timestamps", "runs ", "passed", "status"):
            assert other not in stmt, \
                f"run_health must not read or write {other!r}, but issues: {stmt!r}"
    # Every statement interpolates the one table name it is allowed to touch.
    assert run_health.TABLE == "run_health"
