# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only SQL data-access tools.

The ``runs`` table here is written by hand: the builder that used to produce it went with
the ``data.db`` writer (see ``notes/runs_table_port.md``), and what these tests are about is
the *reader* — attach, truncation, the write refusals, the cross-campaign join.
"""

import asyncio
import sqlite3

import pytest

from robovast.mcp_server.plugins import results as run_data

# The two query tools are coroutines: they announce a pending object-store fetch *before*
# blocking on it, which needs an await. Driven here rather than through a pytest asyncio
# plugin, since the suite configures none.


def describe_campaign_data(*args, **kwargs):
    return asyncio.run(run_data.describe_campaign_data(*args, **kwargs))


def query_campaign_data_sql(*args, **kwargs):
    return asyncio.run(run_data.query_campaign_data_sql(*args, **kwargs))


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
    cdb.commit()
    cdb.close()
    # per-config run dirs with a JUnit test.xml
    for cfg, passed in [("cfg-a", True), ("cfg-b", False)]:
        run0 = cdir / cfg / "0"
        run0.mkdir(parents=True)
        fails = 0 if passed else 1
        (run0 / "test.xml").write_text(
            f'<testsuite tests="1" failures="{fails}" errors="0" time="12.5">'
            f'<testcase time="12.5"/></testsuite>')
    # data.db with just the runs table, in the shape postprocessing used to build:
    # per-run outcome from test.xml, params exploded into typed param_* columns.
    db = sqlite3.connect(cdir / "_execution" / "data.db")
    db.execute("CREATE TABLE _table_name_map (display_name TEXT PRIMARY KEY, sql_name TEXT)")
    db.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER, status TEXT, "
               "passed INTEGER, duration_s REAL, objective REAL, "
               "param_wind REAL, param_mass REAL)")
    db.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", [
        ("cfg-a", 0, "passed", 1, 12.5, 0.9, 2.5, 1.8),
        ("cfg-b", 0, "failed", 0, 12.5, 0.4, 4.0, 1.8),
    ])
    db.execute("CREATE INDEX idx_runs_ctx ON runs (config_name, run_id)")
    db.commit()
    db.close()
    return str(cdir)


def test_describe_lists_runs_and_attached_campaign(campaign):
    d = describe_campaign_data(campaign)
    pairs = {(t["schema"], t["table"]) for t in d["tables"]}
    assert ("main", "runs") in pairs
    assert ("campaign", "unit") in pairs  # campaign.db attached


def test_sql_query_and_param_join(campaign):
    r = query_campaign_data_sql(
        campaign, "SELECT param_wind, status FROM runs ORDER BY param_wind")
    assert r["columns"] == ["param_wind", "status"]
    assert r["rows"][0]["param_wind"] == 2.5
    # attached campaign.db is queryable
    r2 = query_campaign_data_sql(campaign, "SELECT COUNT(*) n FROM campaign.unit")
    assert r2["rows"][0]["n"] == 2


@pytest.mark.parametrize("bad", [
    "DELETE FROM runs", "UPDATE runs SET run_id=9", "CREATE TABLE x(a INT)",
    "DROP TABLE runs", "ATTACH DATABASE 'x' AS y", "PRAGMA writable_schema=1",
])
def test_sql_rejects_writes(campaign, bad):
    assert "error" in query_campaign_data_sql(campaign, bad)


def test_sql_truncates_at_limit(campaign):
    r = query_campaign_data_sql(campaign, "SELECT * FROM runs", limit=1)
    assert r["row_count"] == 1 and r["truncated"] is True


def test_empty_result_carries_note(campaign):
    r = query_campaign_data_sql(
        campaign, "SELECT * FROM runs WHERE config_name='nope'")
    assert r["row_count"] == 0 and "runs" in r.get("note", "")


def test_multi_campaign_cross_query(campaign, tmp_path):
    """A second campaign attaches as schema ``c1`` so two campaigns join in one query."""
    # Build a minimal second campaign (just a data.db with a runs table).
    other = tmp_path / "camp-2026-07-17-090000"
    (other / "_execution").mkdir(parents=True)
    db = sqlite3.connect(other / "_execution" / "data.db")
    db.execute("CREATE TABLE runs (config_name TEXT, run_id INTEGER, objective REAL)")
    db.executemany("INSERT INTO runs VALUES (?,?,?)",
                   [("x", 0, 0.1), ("x", 1, 0.2), ("y", 0, 0.3)])
    db.commit()
    db.close()

    r = query_campaign_data_sql(
        campaign,
        "SELECT (SELECT COUNT(*) FROM runs) AS a, (SELECT COUNT(*) FROM c1.runs) AS b",
        extra_campaign_ids=[str(other)])
    assert r["rows"][0]["a"] == 2   # primary campaign has 2 runs
    assert r["rows"][0]["b"] == 3   # attached c1 has 3
    assert r["attached"] == {"c1": str(other)}


def test_single_campaign_query_unchanged_without_extra(campaign):
    """Omitting extra_campaign_ids keeps the original single-campaign behavior."""
    r = query_campaign_data_sql(campaign, "SELECT COUNT(*) n FROM runs")
    assert r["rows"][0]["n"] == 2
    assert "attached" not in r


def test_list_campaign_plots(campaign):
    # Author-declared plots live in the snapshot .vast under
    # visualization.results.data_browser.plots.
    config_dir = __import__("pathlib").Path(campaign) / "_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "demo.vast").write_text(
        "visualization:\n"
        "  results:\n"
        "    data_browser:\n"
        "      plots:\n"
        "        - title: Wind vs objective\n"
        "          query: SELECT param_wind, objective FROM runs\n"
        "          vega_lite: {mark: point}\n",
        encoding="utf-8")
    r = run_data.list_campaign_plots(campaign)
    assert r["plots"][0]["title"] == "Wind vs objective"
    assert "SELECT" in r["plots"][0]["query"]
    assert r["plots"][0]["vega_lite"] == {"mark": "point"}
