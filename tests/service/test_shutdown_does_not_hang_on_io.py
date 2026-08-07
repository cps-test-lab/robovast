# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Ctrl+C on ``vast serve`` stops within the graceful window, whatever I/O is open.

An SSE stream pulls its next chunk on a worker thread, and against a cluster that pull
is an S3 read over a ``kubectl port-forward`` — tens of seconds when the tunnel has
stalled. Awaiting it plainly meant the shutdown was not noticed until it returned: the
stream missed uvicorn's graceful-shutdown deadline, uvicorn cancelled the response
task, and the cancellation surfaced *after* the server had stopped as an "Exception in
ASGI application" traceback. Meanwhile the abandoned read hit the just-closed
port-forward, its retry loop re-opened the tunnel, and the ``kubectl`` child outlived
the service.

So: a stuck pull no longer holds the stream open, and nothing re-opens a port-forward
once shutdown has been announced.
"""

import threading
import time

import pytest

from robovast.common.shutdown import (begin_shutdown, is_shutting_down,
                                      reset_shutdown)
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.cluster_service import ClusterService
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

#: How long the stuck pull would block for — far past any graceful-shutdown deadline.
_STUCK_S = 30
#: What the stream is allowed to take once shutdown is announced. Generous next to
#: uvicorn's 5 s deadline; the point is that it does not wait for the pull.
_CLOSE_BUDGET_S = 5


@pytest.fixture(autouse=True)
def _clean_flag():
    reset_shutdown()
    yield
    reset_shutdown()


def test_sse_stream_closes_on_shutdown_while_a_pull_is_stuck(tmp_path):
    from fastapi.testclient import TestClient

    entered = threading.Event()
    release = threading.Event()

    class _Stuck(LocalTransport):
        def list_campaigns(self, request):
            entered.set()
            release.wait(_STUCK_S)  # the pull that used to hold shutdown hostage
            return super().list_campaigns(request)

    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    app = build_app(_Stuck(store=store))

    def _announce_shutdown():
        entered.wait(_CLOSE_BUDGET_S)
        app.state.should_exit = lambda: True

    flipper = threading.Thread(target=_announce_shutdown)
    try:
        with TestClient(app) as client:
            flipper.start()
            started = time.monotonic()
            with client.stream("GET", Routes.CAMPAIGNS_STREAM) as response:
                body = "".join(response.iter_text())  # returns when the stream closes
            elapsed = time.monotonic() - started
        assert entered.is_set(), "the pull never ran — the test proved nothing"
        assert elapsed < _CLOSE_BUDGET_S, (
            f"stream waited {elapsed:.1f}s for the stuck pull instead of closing")
        assert body.startswith(": open")  # the stream opened, then ended cleanly
    finally:
        release.set()
        flipper.join(timeout=_CLOSE_BUDGET_S)


def test_port_forward_is_not_reopened_once_shutdown_is_announced():
    """A late S3 read must not resurrect the tunnel the service just tore down."""
    service = ClusterService(namespace="ns", cluster_config_name="x",
                             cluster_config_kwargs={}, reap_on_start=False)
    begin_shutdown()
    with pytest.raises(RuntimeError, match="shutting down"):
        service._minio_port_forward_endpoint()


def test_serve_announces_the_shutdown_from_the_signal_handler():
    """The flag is raised in ``handle_exit``, not in the lifespan teardown.

    By lifespan-teardown time the graceful deadline has already expired and the
    port-forward is already closed — too late for the blocking I/O it exists to stop.
    """
    import uvicorn

    from robovast.service import app as app_module

    captured = {}

    class _FakeServer:
        def __init__(self, config):
            captured["server"] = self

        def handle_exit(self, sig, frame):
            captured["forwarded"] = True

        def run(self):
            self.handle_exit(2, None)  # stand in for the SIGINT uvicorn catches

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(uvicorn, "Server", _FakeServer)
        monkey.setattr(app_module, "build_app", lambda impl: _StubApp())
        assert not is_shutting_down()
        app_module.serve(impl=object())
    finally:
        monkey.undo()

    assert captured["forwarded"], "uvicorn's own exit handling must still run"
    assert is_shutting_down()


class _StubApp:
    """Just enough app for ``serve`` — it only reads/writes ``app.state``."""

    class _State:
        pass

    def __init__(self):
        self.state = self._State()
