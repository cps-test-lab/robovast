# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign-list SSE stream route (`GET /campaigns/events`) is wired.

The stream is a server-side loop over the same ``list_campaigns`` pull: the browser
opens one ``EventSource`` and receives the full list on connect and on every change,
instead of polling. Its runtime behaviour (a live-updating text/event-stream) is
verified out-of-band with ``curl -N``; here we assert the route is registered and
distinct from the pull endpoint, which is what keeps the client contract honest.
"""

import threading

from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

#: Long enough for the stream's 1 s loop to send its first quiet tick, short enough
#: that a stream which never heartbeats fails the test rather than hanging it.
_HEARTBEAT_BUDGET_S = 5


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


def test_quiet_ticks_send_a_client_visible_heartbeat(tmp_path):
    """A tick with nothing to report is a ``heartbeat`` *event*, not an SSE comment.

    This is the contract the browser's staleness watchdog rests on. A comment
    (``: heartbeat``) holds proxies open but never reaches ``EventSource``, which
    leaves the client unable to tell a campaign list that has not changed from a
    socket that died in a suspended laptop or a torn-down ``kubectl port-forward``
    — no error, ``readyState`` still OPEN, and no further byte ever. That zombie is
    what showed a finished campaign as still running until someone hit Refresh, so
    the heartbeat has to be something the client can actually see and time out on.
    """
    from fastapi.testclient import TestClient

    app = _app(tmp_path)
    # No campaigns exist, so the list never changes: every tick after the first is quiet.
    def _stop_soon():
        app.state.should_exit = lambda: True

    timer = threading.Timer(_HEARTBEAT_BUDGET_S, _stop_soon)
    timer.start()
    try:
        with TestClient(app) as client:
            with client.stream("GET", Routes.CAMPAIGNS_STREAM) as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                body = "".join(response.iter_text())
    finally:
        timer.cancel()

    assert "event: heartbeat" in body, f"no client-visible heartbeat in: {body!r}"
    assert ": heartbeat" not in body.replace("event: heartbeat", ""), (
        "the invisible comment heartbeat is back")
