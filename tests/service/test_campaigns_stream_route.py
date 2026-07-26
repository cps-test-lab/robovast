# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign-list SSE stream route (`GET /campaigns/events`) is wired.

The stream is a server-side loop over the same ``list_campaigns`` pull: the browser
opens one ``EventSource`` and receives the full list on connect and on every change,
instead of polling. Its runtime behaviour (a live-updating text/event-stream) is
verified out-of-band with ``curl -N``; here we assert the route is registered and
distinct from the pull endpoint, which is what keeps the client contract honest.
"""

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


def _app(tmp_path):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return build_app(LocalTransport(store=store))


def test_events_route_is_registered(tmp_path):
    app = _app(tmp_path)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert Routes.CAMPAIGNS_STREAM in paths          # the SSE stream
    assert Routes.CAMPAIGNS in paths                 # the pull endpoint still exists


def test_events_route_is_get_only(tmp_path):
    app = _app(tmp_path)
    methods = {
        m
        for r in app.routes
        if getattr(r, "path", None) == Routes.CAMPAIGNS_STREAM
        for m in (getattr(r, "methods", None) or set())
    }
    assert "GET" in methods
    assert "POST" not in methods  # the /campaigns POST (create) must not be shadowed
