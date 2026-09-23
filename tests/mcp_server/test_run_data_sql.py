# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only SQL data-access tools.

The ``runs`` table is derived from ``campaign.db`` when a query names it, and a query sees
the campaign it was asked about and nothing else. What these tests are about is the
*reader*: truncation, the write refusals, the record schema, and the scoping -- a second
campaign with the same configuration names and run ids sits beside the one under test, so a
query that answered about both would look entirely healthy doing so.
"""

import asyncio
from pathlib import Path

import pytest

from robovast.mcp_server.plugins import results as run_data
from tests.robovast_data.conftest import write_store

_CAMPAIGN = "camp-2026-07-16-120000"

#: A second campaign with the SAME configuration names and run ids. Those are the keys
#: a scoping bug shows up on -- its parameter values differ so a leaked row is recognisable.
_OTHER = "camp-2026-07-16-130000"

#: (config_name, passes, wind) per campaign.
_UNITS = {
    _CAMPAIGN: [("cfg-a", True, 2.5), ("cfg-b", False, 4.0)],
    _OTHER: [("cfg-a", True, 90.0), ("cfg-b", True, 91.0)],
}

# The two query tools are coroutines. Driven here rather than through a pytest asyncio
# plugin, since the suite configures none.


def describe_campaign_data(*args, **kwargs):
    return asyncio.run(run_data.describe_campaign_data(*args, **kwargs))


def query_campaign_data_sql(*args, **kwargs):
    return asyncio.run(run_data.query_campaign_data_sql(*args, **kwargs))


def _write_campaign(root: Path, name: str) -> Path:
    cdir = root / name
    (cdir / "_execution").mkdir(parents=True)
    write_store(cdir, {cfg: {"params": {"wind": wind},
                             "runs": {0: "passed" if passed else "failed"}}
                       for cfg, passed, wind in _UNITS[name]})
    return cdir


@pytest.fixture
def campaign(tmp_path) -> str:
    """The campaign under test, with a second one beside it."""
    under_test = _write_campaign(tmp_path, _CAMPAIGN)
    _write_campaign(tmp_path, _OTHER)
    return str(under_test)


def test_describe_lists_runs_and_the_campaign_record(campaign):
    """Both halves stay reachable: the derived ``runs`` table and the record."""
    d = describe_campaign_data(campaign)
    by_name = {(t["schema"], t["table"]): t for t in d["tables"]}
    assert ("main", "runs") in by_name
    assert by_name[("campaign", "unit")]["kind"] == "record"
    assert by_name[("main", "run_view")]["kind"] == "view"


def test_sql_query_and_param_join(campaign):
    """Scalar scenario params are typed ``param_*`` columns, joinable with the outcome."""
    r = query_campaign_data_sql(campaign, "SELECT param_wind, status FROM runs "
                                          "ORDER BY param_wind")
    assert r["columns"] == ["param_wind", "status"]
    assert [row["param_wind"] for row in r["rows"]] == [2.5, 4.0]
    r2 = query_campaign_data_sql(campaign, "SELECT COUNT(*) n FROM campaign.unit")
    assert r2["rows"][0]["n"] == 2


@pytest.mark.parametrize("bad", [
    "DELETE FROM runs", "UPDATE runs SET run_id=9", "CREATE TABLE x(a INT)",
    "DROP TABLE runs", "INSERT INTO runs (run_id) VALUES (9)",
    "ATTACH 'x.db' AS x", "SET threads = 1", "COPY runs TO 'x.csv'",
    "SELECT 1; SELECT 2",
])
def test_sql_rejects_anything_but_one_select(campaign, bad):
    assert "error" in query_campaign_data_sql(campaign, bad)


def test_sql_truncates_at_limit(campaign):
    r = query_campaign_data_sql(campaign, "SELECT * FROM runs", limit=1)
    assert r["row_count"] == 1 and r["truncated"] is True


def test_an_empty_result_is_just_empty(campaign):
    r = query_campaign_data_sql(campaign, "SELECT * FROM runs WHERE config_name='nope'")
    assert r["row_count"] == 0 and r["rows"] == []


def test_a_directory_that_is_no_campaign_is_an_error(tmp_path):
    empty = tmp_path / "camp-2026-07-16-140000"
    (empty / "_execution").mkdir(parents=True)
    r = query_campaign_data_sql(str(empty), "SELECT * FROM runs")
    assert "error" in r and "campaign.db" in r["error"]


def test_query_scopes_to_the_campaign_it_was_asked_about(campaign):
    """No ``WHERE campaign_id`` is needed: the other campaign's rows are not there to read.

    Both campaigns hold ``cfg-a``/0 and ``cfg-b``/0, so a leak would double the count -- a
    plausible-looking number that is partly the wrong campaign's.
    """
    r = query_campaign_data_sql(campaign, "SELECT COUNT(*) n FROM runs")
    assert r["rows"][0]["n"] == 2
    leaked = query_campaign_data_sql(campaign, "SELECT param_wind FROM runs")
    assert {row["param_wind"] for row in leaked["rows"]} == {2.5, 4.0}


def test_list_campaign_plots(campaign, monkeypatch, tmp_path):
    # Author-declared plots live in the snapshot .vast under
    # visualization.results.data_browser.plots, which the service reads.
    from robovast.mcp_server import service_access
    from tests.service.null_service import serving
    service = serving(tmp_path, tmp_path / "workspaces")
    monkeypatch.setattr(service_access, "service_client", lambda: service)
    config_dir = Path(campaign) / "_config"
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
    r = run_data.list_campaign_plots(_CAMPAIGN)
    assert r["plots"][0]["title"] == "Wind vs objective"
    assert "SELECT" in r["plots"][0]["query"]
    assert r["plots"][0]["vega_lite"] == {"mark": "point"}
