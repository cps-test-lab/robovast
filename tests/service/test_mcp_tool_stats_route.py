# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``/admin/mcp-tools``, ``/admin/mcp-calls`` and the CSV export of the latter.

The properties that carry these routes: a tool nobody has called is still a row, an
unreachable index is said rather than drawn as zero, and the export is the log.
"""

import os

import pytest

from robovast.mcp_server import tool_stats
from robovast.service.app import build_app
from robovast.service.interface import Routes
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")

SCHEMA = "mcp_route_test"


@pytest.fixture(name="client")
def _client(tmp_path, monkeypatch):
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from starlette.testclient import TestClient

    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        setup.execute(f"CREATE SCHEMA {SCHEMA}")
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    monkeypatch.setattr(tool_stats, "LOG", tool_stats.ToolCallLog())

    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    app = build_app(LocalTransport(store=store), mount_mcp=False, auth_token="t")
    yield TestClient(app, headers={"Authorization": "Bearer t"})

    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _record(*calls):
    for tool, duration, ok in calls:
        tool_stats.LOG.record(tool, duration, ok, args="{}", answer="ok" if ok else "Err: x")
    tool_stats.LOG.flush()


def test_the_ranking_answers_with_counts_and_durations(client):
    _record(("search_docs", 10.0, True), ("search_docs", 30.0, False),
            ("read_file", 5.0, True))

    body = client.get(Routes.ADMIN_MCP_TOOLS).json()
    assert body["status"] == "ok"
    by_tool = {row["tool"]: row for row in body["tools"]}
    assert by_tool["search_docs"]["calls"] == 2
    assert by_tool["search_docs"]["errors"] == 1
    assert by_tool["search_docs"]["mean_ms"] == pytest.approx(20.0)
    assert by_tool["search_docs"]["max_ms"] == pytest.approx(30.0)


def test_the_retained_window_is_reported_not_left_to_be_inferred(client):
    body = client.get(Routes.ADMIN_MCP_TOOLS).json()
    assert body["max_age_s"] == tool_stats.MAX_AGE_S
    assert body["max_rows"] == tool_stats.MAX_ROWS


def test_a_never_called_tool_still_gets_a_row(client, monkeypatch):
    from robovast.mcp_server import registry
    monkeypatch.setattr(registry, "get_plugin_tools",
                        lambda: {"execution": ["start_campaign", "stop_campaign"]})
    _record(("start_campaign", 1.0, True))

    rows = client.get(Routes.ADMIN_MCP_TOOLS).json()["tools"]
    by_tool = {row["tool"]: row for row in rows}
    assert by_tool["stop_campaign"]["calls"] == 0, (
        "a tool nobody chooses is the row worth reading; an aggregate over calls hides it")
    # Ranked, with the uncalled ones last.
    assert [r["tool"] for r in rows] == ["start_campaign", "stop_campaign"]


def test_the_call_log_reads_newest_first_and_filters(client):
    _record(("a", 1.0, True), ("b", 2.0, False))

    calls = client.get(Routes.ADMIN_MCP_CALLS).json()["calls"]
    assert [c["tool"] for c in calls] == ["b", "a"]

    failed = client.get(Routes.ADMIN_MCP_CALLS, params={"failed_only": True}).json()["calls"]
    assert [c["tool"] for c in failed] == ["b"]
    assert client.get(Routes.ADMIN_MCP_CALLS,
                      params={"tool": "a"}).json()["calls"][0]["tool"] == "a"


def test_the_export_is_a_csv_download_of_the_log(client):
    _record(("search_docs", 10.0, True))

    response = client.get(Routes.ADMIN_MCP_CALLS_CSV)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"mcp-calls-" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert lines[0] == "at,tool,duration_ms,ok,args,answer,actor"
    assert "search_docs" in lines[1]


def test_a_page_says_how_much_of_the_record_it_is(client):
    """A page that reported neither its total nor its bound read as the whole record.

    That is the defect this replaces: the export clamped silently, so asking for more rows
    than were served came back looking complete, while the ranking printed beside it
    summarised a window many times larger. The two disagreed and nothing said so.
    """
    _record(*[(f"t{i}", 1.0, True) for i in range(5)])

    page = client.get(Routes.ADMIN_MCP_CALLS, params={"limit": 2}).json()
    assert len(page["calls"]) == 2
    assert page["total"] == 5
    assert page["truncated"] is True
    assert (page["limit"], page["offset"]) == (2, 0)

    rest = client.get(Routes.ADMIN_MCP_CALLS, params={"limit": 2, "offset": 2}).json()
    assert [c["tool"] for c in rest["calls"]] == ["t2", "t1"]

    whole = client.get(Routes.ADMIN_MCP_CALLS, params={"limit": 50}).json()
    assert whole["truncated"] is False, "a page holding every match is not truncated"
    assert len(whole["calls"]) == whole["total"] == 5


def test_a_partial_export_says_so_in_the_only_place_a_download_has(client):
    """A CSV has no field to carry a bound, so a cut export says it in its filename.

    Whoever opens the saved file later has the name and the rows and nothing else; a
    short file that claims nothing is read as the whole record.
    """
    _record(*[(f"t{i}", 1.0, True) for i in range(4)])

    cut = client.get(Routes.ADMIN_MCP_CALLS_CSV, params={"limit": 2})
    assert "-partial-of-4.csv" in cut.headers["content-disposition"]
    assert len(cut.text.strip().splitlines()) == 3  # header + 2

    whole = client.get(Routes.ADMIN_MCP_CALLS_CSV, params={"limit": 50})
    assert "-partial-of-" not in whole.headers["content-disposition"]


def test_the_export_reaches_past_the_page_ceiling(client):
    """The panel's page bound is not the record's. The export streams, so it is not held
    to a ceiling that exists to bound one JSON response."""
    from robovast.service import app as app_module

    assert app_module._MCP_CALLS_PAGE_MAX < tool_stats.MAX_ROWS
    _record(*[(f"t{i}", 1.0, True) for i in range(3)])

    asked = client.get(Routes.ADMIN_MCP_CALLS_CSV,
                       params={"limit": app_module._MCP_CALLS_PAGE_MAX * 100})
    assert asked.status_code == 200
    assert "-partial-of-" not in asked.headers["content-disposition"]


def test_the_record_says_who_called(client):
    """``actor`` was a column, a model field and a CSV header that nothing ever wrote.

    An advertised capability that is always empty cannot be told from one that is merely
    unused, so a reader could not learn which caller a call came from -- the question the
    record exists to answer.
    """
    tool_stats.LOG.record("search_docs", 1.0, True, actor="an-editor/session-7")
    tool_stats.LOG.flush()

    assert client.get(Routes.ADMIN_MCP_CALLS).json()["calls"][0]["actor"] == \
        "an-editor/session-7"
    assert "an-editor/session-7" in client.get(Routes.ADMIN_MCP_CALLS_CSV).text


def test_an_unreachable_index_is_said_rather_than_drawn_as_zero(client, monkeypatch):
    from robovast.common import index_db
    monkeypatch.delenv(index_db.DSN_ENV, raising=False)

    body = client.get(Routes.ADMIN_MCP_TOOLS).json()
    assert body["status"] == "index-unreachable"
    assert body["detail"], "a panel that cannot say why it is empty invents a fact"
    assert client.get(Routes.ADMIN_MCP_CALLS).json()["status"] == "index-unreachable"
    # A download carries no status field, so it has to fail rather than send an empty file.
    assert client.get(Routes.ADMIN_MCP_CALLS_CSV).status_code == 503
