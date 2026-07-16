# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only SQL data-access tools + the runs dimension table."""

import sqlite3

import pytest

from robovast.mcp_server.plugins import run_data
from robovast.results_processing.postprocessing_plugins import _build_runs_table


@pytest.fixture
def campaign(tmp_path):
    """A minimal campaign: campaign.db (params/objective) + config/run dirs + test.xml."""
    cdir = tmp_path / "camp-2026-07-16-120000"
    (cdir / "_execution").mkdir(parents=True)
    # campaign.db with two configs' params
    cdb = sqlite3.connect(cdir / "campaign.db")
    cdb.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    cdb.executemany("INSERT INTO unit VALUES (?,?,?)", [
        ("cfg-a", '{"wind": 2.5, "mass": 1.8}', 0.9),
        ("cfg-b", '{"wind": 4.0, "mass": 1.8}', 0.4),
    ])
    cdb.commit(); cdb.close()
    # per-config run dirs with a JUnit test.xml
    for cfg, passed in [("cfg-a", True), ("cfg-b", False)]:
        run0 = cdir / cfg / "0"; run0.mkdir(parents=True)
        fails = 0 if passed else 1
        (run0 / "test.xml").write_text(
            f'<testsuite tests="1" failures="{fails}" errors="0" time="12.5">'
            f'<testcase time="12.5"/></testsuite>')
    # build data.db with just the runs table
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    db.execute("CREATE TABLE _table_name_map (display_name TEXT PRIMARY KEY, sql_name TEXT)")
    _build_runs_table(db, cdir, sorted(d for d in cdir.iterdir()
                                       if d.is_dir() and not d.name.startswith("_")))
    db.commit(); db.close()
    return str(cdir)


def test_runs_table_has_params_status_duration(campaign):
    db = sqlite3.connect(f"{campaign}/_execution/data.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    assert {"config_name", "run_id", "status", "duration_s", "objective",
            "param_wind", "param_mass"} <= cols
    rows = {r[0]: r for r in db.execute(
        "SELECT config_name, status, duration_s, param_wind, objective FROM runs")}
    assert rows["cfg-a"][1] == "passed" and rows["cfg-a"][2] == 12.5
    assert rows["cfg-b"][1] == "failed"
    assert rows["cfg-a"][3] == "2.5" and rows["cfg-a"][4] == 0.9


def test_describe_lists_runs_and_attached_campaign(campaign):
    d = run_data.describe_campaign_data(campaign)
    pairs = {(t["schema"], t["table"]) for t in d["tables"]}
    assert ("main", "runs") in pairs
    assert ("campaign", "unit") in pairs  # campaign.db attached


def test_sql_query_and_param_join(campaign):
    r = run_data.query_campaign_data_sql(
        campaign, "SELECT param_wind, status FROM runs ORDER BY param_wind")
    assert r["columns"] == ["param_wind", "status"]
    assert r["rows"][0]["param_wind"] == "2.5"
    # attached campaign.db is queryable
    r2 = run_data.query_campaign_data_sql(campaign, "SELECT COUNT(*) n FROM campaign.unit")
    assert r2["rows"][0]["n"] == 2


@pytest.mark.parametrize("bad", [
    "DELETE FROM runs", "UPDATE runs SET run_id=9", "CREATE TABLE x(a INT)",
    "DROP TABLE runs", "ATTACH DATABASE 'x' AS y", "PRAGMA writable_schema=1",
])
def test_sql_rejects_writes(campaign, bad):
    assert "error" in run_data.query_campaign_data_sql(campaign, bad)


def test_sql_truncates_at_max_rows(campaign):
    r = run_data.query_campaign_data_sql(campaign, "SELECT * FROM runs", max_rows=1)
    assert r["row_count"] == 1 and r["truncated"] is True
