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


def test_an_unreachable_index_is_said_rather_than_drawn_as_zero(client, monkeypatch):
    from robovast.common import index_db
    monkeypatch.delenv(index_db.DSN_ENV, raising=False)

    body = client.get(Routes.ADMIN_MCP_TOOLS).json()
    assert body["status"] == "index-unreachable"
    assert body["detail"], "a panel that cannot say why it is empty invents a fact"
    assert client.get(Routes.ADMIN_MCP_CALLS).json()["status"] == "index-unreachable"
    # A download carries no status field, so it has to fail rather than send an empty file.
    assert client.get(Routes.ADMIN_MCP_CALLS_CSV).status_code == 503
