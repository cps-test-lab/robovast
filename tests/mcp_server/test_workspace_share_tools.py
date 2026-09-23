# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The MCP's half of moving a workspace between deployments.

An agent driving a remote service can publish a project and take one back. Both go through
the service, which holds the share credentials -- the MCP host may be a machine that has
none, and per AGENTS.md §4 a tool never advertises a capability its caller cannot use.
"""

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import authoring
from robovast.service.interface import ShareWorkspaceArchive, WorkspaceInfo


class _FakeClient:
    def __init__(self, failing=None):
        self.calls = []
        self._failing = failing

    def export_workspace(self, workspace_id):
        self.calls.append(("export_workspace", workspace_id))
        if self._failing:
            raise RuntimeError(self._failing)
        return ShareWorkspaceArchive(slug="growth-sim",
                                     object_name="growth-sim.workspace.tar.gz",
                                     size=2048, url="stub://growth-sim.workspace.tar.gz")

    def create_workspace(self, request):
        self.calls.append(("create_workspace", request.name, request.from_campaign,
                           request.from_share))
        return WorkspaceInfo(workspace_id="ws-new1", name=request.name or "growth-sim")


@pytest.fixture(name="client")
def _client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


def test_export_answers_with_the_object_that_landed(client):
    """The object name, because that is what somebody else types to import it -- and the
    one thing the operation produced that the caller did not already have."""
    result = authoring.export_workspace("ws-ab12")
    assert result["object_name"] == "growth-sim.workspace.tar.gz"
    assert result["slug"] == "growth-sim"
    assert ("export_workspace", "ws-ab12") in client.calls


def test_export_without_a_share_reports_the_refusal(monkeypatch):
    """A service with no share cannot publish. The tool says so rather than raising into
    the transport, where a model never reads it."""
    monkeypatch.setattr(service_access, "service_client",
                        lambda: _FakeClient(failing="this service has no share configured"))
    assert "no share configured" in authoring.export_workspace("ws-ab12")["error"]


def test_a_workspace_is_created_from_the_share_by_slug(client):
    result = authoring.create_workspace(from_share="growth-sim")
    assert result["workspace_id"] == "ws-new1"
    assert ("create_workspace", "", "", "growth-sim") in client.calls


def test_the_two_sources_are_passed_through_for_the_service_to_refuse(client):
    """One refusal, in the one place that can be sure of it. A second check here would be a
    second rule, free to disagree with the service's."""
    authoring.create_workspace(from_campaign="nav-2026-08-18-194018", from_share="growth-sim")
    assert ("create_workspace", "", "nav-2026-08-18-194018", "growth-sim") in client.calls
