# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Ctrl+C on ``vast serve`` stops within the graceful window, whatever I/O is open.

An SSE stream pulls its next chunk on a worker thread, and a pull can be slow -- a
listing over a loaded disk, an index that is not answering. Awaiting it plainly means
the shutdown is not noticed until it returns: the stream misses uvicorn's
graceful-shutdown deadline, uvicorn cancels the response task, and the cancellation
surfaces *after* the server has stopped, as an "Exception in ASGI application"
traceback.

So: a stuck pull does not hold the stream open.
"""

import threading
import time

from robovast.service.app import build_app
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
from tests.service.null_lane import NullLane

#: How long the stuck pull would block for -- far past any graceful-shutdown deadline.
_STUCK_S = 30
#: What the stream is allowed to take once shutdown is announced. Generous next to
#: uvicorn's 5 s deadline; the point is that it does not wait for the pull.
_CLOSE_BUDGET_S = 5


def test_sse_stream_closes_on_shutdown_while_a_pull_is_stuck(tmp_path):
    from fastapi.testclient import TestClient

    entered = threading.Event()
    release = threading.Event()

    class _Stuck(NullLane):
        # test double
        def list_campaigns(self, request):  # pylint: disable=signature-differs
            entered.set()
            release.wait(_STUCK_S)  # a pull that would hold shutdown hostage
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
