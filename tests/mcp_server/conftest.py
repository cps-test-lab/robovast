# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the MCP plugin tests."""

import functools
import sys

import pytest


@functools.cache
def registered_tools() -> dict:
    """``{name: Tool}`` for every tool of every installed plugin, as the server registers it."""
    from fastmcp import FastMCP

    from robovast.mcp_server.registry import load_plugins
    from robovast.mcp_server.registry import registered_tools as tools_of
    mcp = FastMCP("tests")
    load_plugins(mcp)
    return tools_of(mcp)


@pytest.fixture(autouse=True)
def _tools_as_registered(monkeypatch):
    """Every plugin module's tool is the one the server registered, so a test that calls
    ``execution.stop_campaign(...)`` is answered as a caller is -- an exception the tool
    raises arrives as the error document."""
    for tool in registered_tools().values():
        bare = tool.fn.__wrapped__
        monkeypatch.setattr(sys.modules[bare.__module__], bare.__name__, tool.fn)


@pytest.fixture(autouse=True)
def _no_stray_service(monkeypatch):
    """Isolate MCP tests from any real ``vast serve`` on the conventional local port.

    The tools resolve where to run via ``detected_service_url()``: when a service is
    reachable they route to it over HTTP, otherwise they run locally / via a subprocess.
    That makes the tests non-deterministic — a leftover ``vast serve`` on 127.0.0.1:8800
    silently diverts them there and they 404 on the test's throwaway campaigns. Default
    every MCP test to "no service reachable" so it deterministically takes the local
    path; the tests that exercise the service path patch ``_service_client`` /
    ``_service`` directly and are unaffected.
    """
    monkeypatch.setattr(
        "robovast.client.service_target.detected_service_url",
        lambda *a, **k: "", raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_in_process_service():
    """Clear the in-process binding a mounted MCP sets, before and after each test.

    ``build_app(mount_mcp=True)`` binds the implementation process-wide so a mounted
    MCP calls it directly instead of over loopback HTTP. One process serves one app, so
    that is right in production — but a test session builds many, and a service test
    that mounts MCP would otherwise leave every later MCP test resolving *its*
    implementation instead of the one under test. The symptom is order-dependent
    failures, which is the worst kind to debug.
    """
    from robovast.mcp_server import service_access
    service_access.use_in_process_service(None)
    yield
    service_access.use_in_process_service(None)
