# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The run-health contract in the central index.

Two rules carried over from the per-campaign writer -- RoboVAST learns one word, and absence
is never a pass -- plus the one the shared index adds: a check is called
``check(conn, campaign_id)`` and grades exactly one campaign.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import os
import sqlite3

import pytest

from robovast.results_processing import campaign_ingest, index_schema, run_health
from robovast.results_processing.row_sink import PostgresRowSink
from robovast.results_processing.run_health import (LEVELS, TABLE, HealthRow,
                                                    build_run_health_table,
                                                    load_health_checks)

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "run_health_test"


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


def _sink(conn, campaign_id="camp-a"):
    return PostgresRowSink(conn, campaign_id=campaign_id)


def _rows(conn, campaign_id="camp-a"):
    return conn.execute(
        "SELECT campaign_id, config_name, run_id, check_name, level, value, unit, detail, "
        f"source FROM {TABLE} WHERE campaign_id = %s ORDER BY run_id, check_name",
        (campaign_id,)).fetchall()


def _all_rows(conn):
    return conn.execute(
        f"SELECT campaign_id, config_name, run_id, level FROM {TABLE} "
        "ORDER BY campaign_id, run_id").fetchall()


# -- the contract the shared index adds -----------------------------------------------------

def test_a_check_is_told_which_campaign_it_is_grading(conn):
    """The whole reason the signature changed: one set of tables holds every campaign, so a
    check that is not told which one it is looking at cannot scope its own SQL."""
    seen = []

    def check(_conn, campaign_id):
        seen.append(campaign_id)
        return [HealthRow("cfg-a", 0, "z", "ok")]

    build_run_health_table(_sink(conn), conn, "camp-a", {"c": check})
    assert seen == ["camp-a"]
    assert _rows(conn)[0][0] == "camp-a"


def test_a_check_with_the_old_signature_fails_loudly_and_writes_nothing(conn, caplog):
    """An unported third-party check must be refused, not called with one argument and left
    to grade the whole corpus. The message has to name the change, because the traceback of a
    bare arity error does not say why the argument appeared."""
    def old_style(_conn):  # the pre-index contract
        return [HealthRow("cfg-a", 0, "z", "ok")]

    with caplog.at_level("ERROR"):
        assert build_run_health_table(_sink(conn), conn, "camp-a", {"old": old_style}) == 0
    assert "'old'" in caplog.text
    assert "check(conn, campaign_id)" in caplog.text
    assert "campaign_id" in caplog.text
    assert _rows(conn) == []


def test_one_campaigns_grades_never_include_another_campaigns_runs(conn):
    """A check scoped to its campaign, and a table that keeps them apart: the failure this
    replaces would have graded the whole corpus under one campaign's id."""
    def check(_conn, campaign_id):
        return [HealthRow(f"cfg-{campaign_id}", 0, "z", "ok")]

    build_run_health_table(_sink(conn, "camp-a"), conn, "camp-a", {"c": check})
    build_run_health_table(_sink(conn, "camp-b"), conn, "camp-b", {"c": check})

    assert [(r[0], r[1]) for r in _all_rows(conn)] == [
        ("camp-a", "cfg-camp-a"), ("camp-b", "cfg-camp-b")]
    assert len(_rows(conn, "camp-a")) == 1


def test_building_one_campaign_never_drops_anothers_grades(conn):
    """The old builder began with ``DROP TABLE run_health``. In one index that is every
    campaign's grades, so it is gone: idempotence comes from ``clear_campaign`` instead."""
    def check(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "z", "ok")]

    build_run_health_table(_sink(conn, "camp-a"), conn, "camp-a", {"c": check})
    build_run_health_table(_sink(conn, "camp-b"), conn, "camp-b", {"c": check})
    assert len(_all_rows(conn)) == 2

    index_schema.clear_campaign(conn, "camp-b")
    build_run_health_table(_sink(conn, "camp-b"), conn, "camp-b", {"c": check})
    assert len(_all_rows(conn)) == 2
    assert len(_rows(conn, "camp-a")) == 1


def test_re_ingest_reproduces_identical_rows(conn):
    """Postprocessing is re-runnable, and a doubled row doubles every count drawn from this
    table. Clear-then-write must land exactly what was there before."""
    def check(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "z", "warn", detail="d", value=3, unit="misses"),
                HealthRow("cfg-a", 1, "z", "ok", value=0, unit="misses")]

    build_run_health_table(_sink(conn), conn, "camp-a", {"c": check})
    before = _rows(conn)

    index_schema.clear_campaign(conn, "camp-a")
    build_run_health_table(_sink(conn), conn, "camp-a", {"c": check})
    assert _rows(conn) == before
    assert len(before) == 2


# -- the rules the per-campaign writer already had ------------------------------------------

def test_the_table_exists_even_when_nothing_fills_it(conn):
    """An absent table and an empty one say different things -- "this campaign predates health
    checks" versus "they ran and found nothing" -- and only the second is evidence."""
    assert build_run_health_table(_sink(conn), conn, "camp-a", {}) == 0
    assert _rows(conn) == []
    conn.execute(f"SELECT 1 FROM {TABLE}")  # exists, rather than raising


def test_a_measure_survives_the_round_trip(conn):
    """``value``/``unit`` are the addition to the live finding contract and the point of it:
    a finding says bad, a measure says how bad, and a floor is a knee in a curve."""
    def check(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "control_loop_misses", "warn",
                          detail="60 misses in window", value=60, unit="count")]

    assert build_run_health_table(_sink(conn), conn, "camp-a", {"nav2": check}) == 1
    row, = _rows(conn)
    assert (row[3], row[4], row[5], row[6]) == ("control_loop_misses", "warn", 60.0, "count")
    assert row[8] == "stack"


def test_a_measure_keeps_full_precision(conn):
    """``double precision``, not Postgres' 4-byte ``real``: a check may carry an epoch or a
    long-running duration as its measure, and ``real`` mangles both silently."""
    def check(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "t", "ok", value=1767225600.5, unit="s")]

    build_run_health_table(_sink(conn), conn, "camp-a", {"c": check})
    assert _rows(conn)[0][5] == 1767225600.5


def test_ok_is_a_row_because_absence_means_not_checked(conn):
    """The rule that makes the table readable at all. A clean run must SAY it was checked;
    otherwise "no row" covers both "healthy" and "no plugin installed", and the two have
    opposite consequences for a conclusion drawn from the campaign."""
    def check(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "solve_failures", "ok", value=0, unit="count"),
                HealthRow("cfg-a", 1, "solve_failures", "error", value=9, unit="count")]

    build_run_health_table(_sink(conn), conn, "camp-a", {"moveit": check})
    assert [r[4] for r in _rows(conn)] == ["ok", "error"]


def test_a_level_robovast_cannot_interpret_is_dropped_and_said_out_loud(conn, caplog):
    """RoboVAST interprets exactly one word. A stack's private vocabulary in that column
    could not be rendered, sorted or compared across stacks, and guessing a mapping would
    put the guess into data readers trust. Dropped loudly, never coerced."""
    def check(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "x", "CRITICAL"), HealthRow("cfg-a", 1, "x", "ok")]

    with caplog.at_level("WARNING"):
        assert build_run_health_table(_sink(conn), conn, "camp-a", {"bad": check}) == 1
    assert [r[4] for r in _rows(conn)] == ["ok"]
    assert "CRITICAL" in caplog.text
    assert all(r[4] in LEVELS for r in _rows(conn))


def test_one_failing_check_does_not_cost_the_others_or_the_campaign(conn, caplog):
    """The runs are the deliverable. A stack plugin that raises must not take a campaign's
    ingest with it -- but it must be loud, because a swallowed error leaves rows missing,
    and a missing row reads as "not checked" rather than as a failure."""
    def boom(_conn, campaign_id):
        raise RuntimeError("plugin is broken")

    def fine(_conn, campaign_id):
        return [HealthRow("cfg-a", 0, "y", "ok")]

    with caplog.at_level("WARNING"):
        assert build_run_health_table(
            _sink(conn), conn, "camp-a", {"boom": boom, "fine": fine}) == 1
    assert "plugin is broken" in caplog.text
    assert [r[3] for r in _rows(conn)] == ["y"]


def test_a_plain_mapping_is_accepted_so_a_plugin_need_not_import_us(conn):
    """A stack shipping a check should not have to depend on RoboVAST to describe one run."""
    def check(_conn, campaign_id):
        return [{"config_name": "cfg-a", "run_id": 0, "check": "z", "level": "warn",
                 "detail": "d", "value": 1.5, "unit": "s"}]

    assert build_run_health_table(_sink(conn), conn, "camp-a", {"dict": check}) == 1
    assert _rows(conn)[0][5] == 1.5


def test_a_callable_instance_is_accepted_as_well_as_a_function(conn):
    """The shipped checks are classes; the signature probe must read the bound ``__call__``
    rather than reject every plugin that packages itself as one."""
    class Check:
        def __call__(self, _conn, campaign_id):
            return [HealthRow("cfg-a", 0, "z", "ok")]

    assert build_run_health_table(_sink(conn), conn, "camp-a", {"c": Check()}) == 1


def test_an_uninstalled_name_is_reported_rather_than_silently_skipped(caplog):
    """A campaign naming a check it does not have must not read as a campaign with nothing
    to check -- that is rule 2 at the configuration level."""
    with caplog.at_level("WARNING"):
        checks = load_health_checks(declared=["nav2_health_that_is_not_installed"])
    assert "nav2_health_that_is_not_installed" not in checks
    assert "not installed" in caplog.text


# -- the ingest wires it in -----------------------------------------------------------------

def _campaign(tmp_path):
    root = tmp_path / "camp"
    (root / "goal-1" / "0").mkdir(parents=True)
    db = sqlite3.connect(root / "campaign.db")
    db.executescript("CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT);")
    db.execute("INSERT INTO campaign VALUES (1, 'camp')")
    db.commit()
    db.close()
    (root / "goal-1" / "0" / "nav-metrics.csv").write_text("duration_s\n12.5\n")
    return str(root)


def test_the_ingest_runs_the_declared_checks_against_this_campaign(conn, tmp_path,
                                                                   monkeypatch):
    """End to end: the ingest resolves the campaign's declared checks and hands each one the
    campaign it is grading, after the tables the check reads are in the index."""
    seen = {}

    def check(_conn, campaign_id):
        # The check's own input must already be there -- it is built last for that reason.
        seen["runs"] = _conn.execute(
            "SELECT count(*) FROM runs WHERE campaign_id = %s", (campaign_id,)).fetchone()[0]
        seen["campaign_id"] = campaign_id
        return [HealthRow("goal-1", 0, "z", "ok")]

    monkeypatch.setattr(run_health, "load_health_checks",
                        lambda declared=None, config_dir=None: {"c": check})
    totals = campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    assert totals[TABLE] == 1
    assert seen == {"runs": 1, "campaign_id": "camp-a"}
    assert _rows(conn)[0][:5] == ("camp-a", "goal-1", 0, "z", "ok")


def test_the_table_is_there_for_a_campaign_that_declared_nothing(conn, tmp_path):
    """Absent and empty say different things one level up too: the ingest always creates it."""
    totals = campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")
    assert totals[TABLE] == 0
    conn.execute(f"SELECT 1 FROM {TABLE}")


# -- the rule it must never break -----------------------------------------------------------

def test_health_never_decides_pass_fail():
    """Pinned as a layering fact rather than a behaviour: this module writes its OWN table and
    reads nothing.

    Two differently-calibrated oracles eventually disagree about the same run, and the
    scenario's verdict is the one that counts -- so health must be unable to write a run's
    status even by accident. The module issues no SQL at all now (rows go through the row
    sink), so the check is that it names no other table and no verdict column.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(run_health))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sql = [t for t in literals
           if any(k in t.upper() for k in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ",
                                           "CREATE TABLE", "DROP "))]
    assert not sql, f"run_health must issue no SQL of its own, but has: {sql!r}"
    assert run_health.TABLE == "run_health"
