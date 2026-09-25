# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The run-health contract: RoboVAST learns one word, absence is never a pass, and a check
is called ``check(conn, campaign_id)`` with a connection to that campaign's tables."""

import json

import pyarrow.parquet as pq

from robovast.results_processing import campaign_tables, run_health
from robovast.results_processing.data_query import open_data_db, query_data_db
from robovast.results_processing.run_health import (LEVELS, TABLE, HealthRow,
                                                    load_health_checks, run_checks, to_table)

from .conftest import write_campaign_db, write_results_tree


def _campaign(tmp_path, name="camp-a"):
    root = tmp_path / name
    write_results_tree(root)
    write_campaign_db(root, name)
    (root / "_config").mkdir()
    vast = root / "_config" / "c.vast"
    vast.write_text("version: 6\nresults_processing:\n  health_checks: [c]\n")
    return root, str(vast)


def _graded(rows, campaign_id="camp-a"):
    return to_table(rows, campaign_id).to_pylist()


# -- the contract --------------------------------------------------------------------------

def test_a_check_is_told_which_campaign_it_is_grading(tmp_path):
    root, _vast = _campaign(tmp_path)
    seen = []

    def check(_conn, campaign_id):
        seen.append(campaign_id)
        return [HealthRow("cfg-a", 0, "z", "ok")]

    rows = run_checks(open_data_db(root, "camp-a"), "camp-a", {"c": check})
    assert seen == ["camp-a"]
    assert _graded(rows)[0]["campaign_id"] == "camp-a"


def test_a_check_reads_this_campaigns_tables_through_its_connection(tmp_path):
    root, _vast = _campaign(tmp_path)
    seen = {}

    def check(conn, campaign_id):
        seen["runs"] = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        seen["error"] = conn.execute(
            "SELECT error FROM landing_error WHERE config_name = ? AND run_id = ?",
            ["cfg-a", 1]).fetchone()["error"]
        return [HealthRow("cfg-a", 0, "z", "ok")]

    run_checks(open_data_db(root, "camp-a"), "camp-a", {"c": check})
    assert seen == {"runs": 4, "error": 0.9}


def test_a_check_that_cannot_take_the_campaign_is_refused_loudly(tmp_path, caplog):
    root, _vast = _campaign(tmp_path)

    def one_argument(_conn):
        return [HealthRow("cfg-a", 0, "z", "ok")]

    with caplog.at_level("ERROR"):
        assert run_checks(open_data_db(root, "camp-a"), "camp-a", {"old": one_argument}) == []
    assert "'old'" in caplog.text and "check(conn, campaign_id)" in caplog.text


# -- the rules -----------------------------------------------------------------------------

def test_a_measure_survives_with_full_precision():
    rows = run_checks(None, "camp-a", {"nav2": lambda _c, _id: [
        HealthRow("cfg-a", 0, "control_loop_misses", "warn", detail="60 misses",
                  value=1767225600.5, unit="s")]})
    (row,) = _graded(rows)
    assert (row["check_name"], row["level"], row["value"], row["unit"], row["source"]) == (
        "control_loop_misses", "warn", 1767225600.5, "s", "stack")


def test_ok_is_a_row_because_absence_means_not_checked():
    rows = run_checks(None, "camp-a", {"moveit": lambda _c, _id: [
        HealthRow("cfg-a", 0, "solve_failures", "ok", value=0, unit="count"),
        HealthRow("cfg-a", 1, "solve_failures", "error", value=9, unit="count")]})
    assert [r["level"] for r in _graded(rows)] == ["ok", "error"]


def test_a_level_robovast_cannot_interpret_is_dropped_and_said_out_loud(caplog):
    with caplog.at_level("WARNING"):
        rows = run_checks(None, "camp-a", {"bad": lambda _c, _id: [
            HealthRow("cfg-a", 0, "x", "CRITICAL"), HealthRow("cfg-a", 1, "x", "ok")]})
    assert [r["level"] for r in rows] == ["ok"]
    assert "CRITICAL" in caplog.text
    assert all(r["level"] in LEVELS for r in rows)


def test_one_failing_check_does_not_cost_the_others(caplog):
    def boom(_conn, campaign_id):
        raise RuntimeError("plugin is broken")

    with caplog.at_level("WARNING"):
        rows = run_checks(None, "camp-a", {
            "boom": boom, "fine": lambda _c, _id: [HealthRow("cfg-a", 0, "y", "ok")]})
    assert "plugin is broken" in caplog.text
    assert [r["check_name"] for r in rows] == ["y"]


def test_a_plain_mapping_is_accepted_so_a_plugin_need_not_import_us():
    rows = run_checks(None, "camp-a", {"dict": lambda _c, _id: [
        {"config_name": "cfg-a", "run_id": 0, "check": "z", "level": "warn",
         "detail": "d", "value": 1.5, "unit": "s"}]})
    assert _graded(rows)[0]["value"] == 1.5


def test_a_callable_instance_is_accepted_as_well_as_a_function():
    class Check:
        def __call__(self, _conn, campaign_id):
            return [HealthRow("cfg-a", 0, "z", "ok")]

    assert len(run_checks(None, "camp-a", {"c": Check()})) == 1


def test_an_uninstalled_name_is_reported_rather_than_silently_skipped(caplog):
    with caplog.at_level("WARNING"):
        checks = load_health_checks(declared=["nav2_health_that_is_not_installed"])
    assert "nav2_health_that_is_not_installed" not in checks
    assert "not installed" in caplog.text


# -- the campaign-end pass writes it -------------------------------------------------------

def test_the_declared_checks_grade_the_campaign_into_its_table(tmp_path, monkeypatch):
    root, vast = _campaign(tmp_path)

    def check(conn, campaign_id):
        runs = conn.execute("SELECT config_name, run_id FROM runs ORDER BY 1, 2").fetchall()
        return [HealthRow(r["config_name"], r["run_id"], "z", "ok") for r in runs]

    monkeypatch.setattr(run_health, "load_health_checks",
                        lambda declared=None, config_dir=None: {"c": check})
    assert campaign_tables.write_run_health(str(root), vast) == 4
    answer = query_data_db(root, "SELECT config_name, run_id, level FROM run_health "
                                 "ORDER BY 1, 2", campaign_id="camp-a")
    assert [(r["config_name"], r["run_id"], r["level"]) for r in answer["rows"]] == [
        ("cfg-a", 0, "ok"), ("cfg-a", 1, "ok"), ("cfg-b", 0, "ok"), ("cfg-b", 1, "ok")]


def test_the_table_is_there_for_a_campaign_whose_checks_said_nothing(tmp_path, monkeypatch):
    """An absent table says "never graded"; an empty one says the checks ran."""
    root, vast = _campaign(tmp_path)
    monkeypatch.setattr(run_health, "load_health_checks",
                        lambda declared=None, config_dir=None: {})
    assert campaign_tables.write_run_health(str(root), vast) == 0
    manifest = json.loads((root / ".cache" / "MANIFEST.json").read_text())
    files = manifest["tables"][TABLE]["campaign"]["files"]
    table = pq.read_table(root / ".cache" / files[0])
    assert table.num_rows == 0 and "level" in table.column_names


def test_a_run_scope_reads_only_its_runs_grades(tmp_path, monkeypatch):
    root, vast = _campaign(tmp_path)
    monkeypatch.setattr(run_health, "load_health_checks", lambda declared=None, config_dir=None: {
        "c": lambda _c, _id: [HealthRow("cfg-a", 0, "z", "ok"),
                              HealthRow("cfg-b", 1, "z", "warn")]})
    campaign_tables.write_run_health(str(root), vast)
    from robovast_data import open_data
    assert list(open_data(str(root / "cfg-b" / "1")).table(TABLE).level) == ["warn"]


# -- the rule it must never break -----------------------------------------------------------

def test_health_never_decides_pass_fail():
    """This module writes its OWN table and reads nothing: it issues no SQL, so it cannot
    write a run's status even by accident."""
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
