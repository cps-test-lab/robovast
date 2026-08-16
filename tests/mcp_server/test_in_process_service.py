# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A mounted MCP calls the service it lives in, not itself over HTTP.

``vast serve`` mounts the MCP app on its own port, so the tools and the implementation
are in one process. They still went out over loopback and back in — a wasted round trip
per tool call, and once a token was required, a process authenticating to itself that
worked only because it happened to hold its own secret.
"""

from robovast.mcp_server import service_access


def test_without_mounting_the_client_is_resolved_over_http(monkeypatch):
    monkeypatch.setattr("robovast.client.service_target.detected_service_url",
                        lambda: "https://robovast.example.org")
    client = service_access.service_client()
    assert getattr(client, "base_url", None) == "https://robovast.example.org"


def test_no_service_and_no_mount_is_none(monkeypatch):
    monkeypatch.setattr("robovast.client.service_target.detected_service_url",
                        lambda: "")
    assert service_access.service_client() is None


def test_a_mounted_mcp_gets_the_implementation_itself(monkeypatch):
    sentinel = object()
    service_access.use_in_process_service(sentinel)

    def _must_not_be_called():
        raise AssertionError("a mounted MCP must not resolve a URL to reach itself")

    monkeypatch.setattr("robovast.client.service_target.detected_service_url",
                        _must_not_be_called)
    assert service_access.service_client() is sentinel


def test_building_the_app_with_mcp_binds_the_implementation():
    from robovast.service.app import build_app
    from robovast.service.client import LocalTransport

    impl = LocalTransport()
    build_app(impl, mount_mcp=True, auth_token="tok")
    assert service_access.service_client() is impl
