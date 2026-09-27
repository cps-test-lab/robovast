# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast workspace list``, ``image status``, ``service info`` and ``service resources``
answer with ``--json`` as their MCP tools do, and print their lines from the same document.

One fake service is asked through the CLI and through the tool, and the documents compared.
Stdout carries the document alone -- the target line goes to stderr -- so it parses.
"""

import contextlib
import json

import pytest
from click.testing import CliRunner

from robovast.client import cli as root_cli
from robovast.client import service_cli
from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import authoring, execution, reference
from robovast.service.interface import (DiskSpace, ImageBuildError, ImageBuildStatus,
                                        ListWorkspacesResponse, ResourceUsage, VersionInfo,
                                        WorkspaceInfo)


class _Client:
    def list_workspaces(self):
        return ListWorkspacesResponse(workspaces=[
            WorkspaceInfo(workspace_id="ws-1", name="demo", created_at="2026-01-02",
                          running_campaigns=["c-live"]),
            WorkspaceInfo(workspace_id="ws-2")])

    def get_image_build_status(self, build_id):
        return ImageBuildStatus(
            build_id=build_id, tag="t1", phase="failed", done=True,
            image_ref="build:t1",
            error=ImageBuildError(phase="apt", entry="ros-jazzy-nope",
                                  message="unable to locate package"))

    def version(self):
        return VersionInfo(robovast_version="abc123", code_revision="abc123",
                           package_version="1.2.0", api_version="7", backend="kubernetes",
                           kube_context="lab", kube_context_source="kubeconfig",
                           namespace="default", in_pod=True, can_build_images=False,
                           build_unavailable="no registry configured", can_schedule=True,
                           web_base="https://robovast.example.org")

    def resource_usage(self):
        return ResourceUsage(backend="kubernetes", cpu_capacity=64.0, cpu_used=12.0,
                             memory_capacity_bytes=256 * 1024 ** 3,
                             memory_used_bytes=32 * 1024 ** 3, parallel_runs=True,
                             jobs_running=3, jobs_pending=5,
                             disk=DiskSpace(capacity_bytes=500 * 1000 ** 3,
                                            used_bytes=100 * 1000 ** 3),
                             results=DiskSpace(capacity_bytes=2000 * 1000 ** 3,
                                               used_bytes=500 * 1000 ** 3),
                             metrics_unavailable="metrics-server is not installed",
                             storage_refusal="the results volume is below its reserve")


@pytest.fixture(name="ask")
def _ask(monkeypatch):
    @contextlib.contextmanager
    def _service(*_a, **_k):
        yield _Client(), "fake service"

    monkeypatch.setattr(root_cli, "service_client", _service)
    monkeypatch.setattr(service_cli, "service_client", _service)
    monkeypatch.setattr(service_access, "service_client", _Client)

    def ask(group, *args):
        result = CliRunner().invoke(group, list(args))
        assert result.exit_code == 0, result.output + result.stderr
        target_on = result.stderr if "--json" in args else result.stdout
        assert "Target: fake service" in target_on
        return result.stdout

    return ask


def test_workspace_list_json_is_what_list_workspaces_returns(ask):
    printed = json.loads(ask(root_cli.workspace, "list", "--json"))
    assert printed == authoring.list_workspaces()
    assert printed["total"] == 2


def test_image_status_json_is_what_get_image_build_status_returns(ask):
    printed = json.loads(ask(root_cli.image, "status", "b1", "--json"))
    assert printed == execution.get_image_build_status("b1")
    assert printed["error_detail"]["entry"] == "ros-jazzy-nope"


def test_service_info_json_is_what_get_service_info_returns(ask):
    printed = json.loads(ask(service_cli.service, "info", "--json"))
    assert printed == reference.get_service_info()
    assert printed["build_unavailable"] == "no registry configured"


def test_service_resources_json_is_what_get_resource_usage_returns(ask):
    printed = json.loads(ask(service_cli.service, "resources", "--json"))
    assert printed == execution.get_resource_usage()
    assert printed["results"]["capacity_bytes"] == 2000 * 1000 ** 3


def test_the_printed_workspace_list_is_drawn_from_the_listing(ask):
    printed = ask(root_cli.workspace, "list")
    assert "ws-1  demo" in printed
    assert "[running: c-live]" in printed
    assert "ws-2  -" in printed


def test_the_printed_image_status_is_drawn_from_the_report(ask):
    printed = ask(root_cli.image, "status", "b1")
    assert "phase=failed" in printed
    assert "[apt] unable to locate package" in printed
    assert "get_image_build_log(build_id='b1'" in printed


def test_the_printed_service_info_is_drawn_from_the_report(ask):
    printed = ask(service_cli.service, "info")
    assert "version   1.2.0" in printed
    assert "revision  abc123" in printed
    assert "web       https://robovast.example.org" in printed
    assert "builds    no — no registry configured" in printed
    assert "context   lab (kubeconfig)" in printed
    assert "namespace default (in the cluster)" in printed


def test_the_printed_resources_are_drawn_from_the_report(ask):
    printed = ask(service_cli.service, "resources")
    assert "cpu       12.0 / 64.0 cores" in printed
    assert "runs      3 running, 5 pending" in printed
    assert "disk      400 GB free of 500 GB" in printed
    assert "results   1500 GB free of 2000 GB" in printed
    assert "measured  not read: metrics-server is not installed" in printed
    assert "refusing  the results volume is below its reserve" in printed
