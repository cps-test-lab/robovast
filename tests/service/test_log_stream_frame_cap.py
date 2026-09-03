# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A log stream frame is bounded, so opening a panel on a huge log is not a download.

A campaign's assembled infrastructure log reaches tens of megabytes, and the panels that
tail it are fixed-height panes whose reader wants the *end*. Serving the whole thing costs
the transfer, a JSON parse and a DOM node per line before the first character appears — for
output nobody scrolls back to. So a frame is served from its end.

What must hold is the pair: the reader is told bytes were dropped (a silent truncation is a
log that lies), and ``next_offset`` still counts the *log*, not what was sent, or every
resumed connection after a capped frame would re-send from the wrong byte.
"""

import threading

from robovast.common.campaign_logs import EXECUTION_DIR
from robovast.service.app import build_app
from robovast.service.client import LocalTransport
from robovast.service.interface import Routes
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

#: Long enough for the stream's sub-second loop to deliver its first frame, short enough
#: that a stream which never sends one fails the test rather than hanging it.
_FRAME_BUDGET_S = 5

_CAMPAIGN = "capped-2026-01-01-000000"

#: Each line is unique, so the assertions can say *which* end of the log was served.
_LINES = [f"line {i:07d} " + "x" * 64 for i in range(40000)]


def _app_with_log(tmp_path):
    results = tmp_path / "results"
    exec_dir = results / _CAMPAIGN / EXECUTION_DIR
    exec_dir.mkdir(parents=True)
    (exec_dir / "controller.log").write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return build_app(LocalTransport(store=store, results_dir=str(results)))


def _stream(app, path):
    """The bytes one connection produces before the app is asked to shut down."""
    from fastapi.testclient import TestClient

    timer = threading.Timer(_FRAME_BUDGET_S, lambda: setattr(app.state, "should_exit",
                                                             lambda: True))
    timer.start()
    try:
        with TestClient(app) as client:
            with client.stream("GET", path) as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                return "".join(response.iter_text())
    finally:
        timer.cancel()


def test_a_huge_log_is_served_from_its_end(tmp_path):
    app = _app_with_log(tmp_path)
    body = _stream(app, Routes.campaign_logs_stream(_CAMPAIGN))

    assert _LINES[-1] in body, "the tail — the whole point of the panel — is missing"
    assert _LINES[0] not in body, "the head was sent; the frame is not capped"
    # The reader is told, and told how much: a panel that silently drops the first
    # megabytes of a log is a panel that lies about what the campaign printed.
    assert "not shown" in body
    assert "vast campaign log" in body, "no way back to the whole log is named"


def test_the_frame_still_carries_the_offset_the_log_reached(tmp_path):
    """``id:`` is where the *log* continues, not how much of it was sent.

    The browser echoes it as ``Last-Event-ID`` on a reconnect, so a capped frame that
    reported the size it sent would make every resumed connection re-deliver the bytes
    between there and the true end — the log would appear twice from the cap onwards.
    """
    app = _app_with_log(tmp_path)
    body = _stream(app, Routes.campaign_logs_stream(_CAMPAIGN))

    ids = [int(line.split("id: ", 1)[1]) for line in body.splitlines()
           if line.startswith("id: ")]
    assert ids, f"no frame carried an offset: {body[:200]!r}"
    written = len("\n".join(_LINES).encode()) + 1
    # The banner the phase divider adds is the only other content in the stream.
    assert ids[0] > written, "the offset counts the bytes sent, not the log's own length"


def test_a_small_log_is_untouched(tmp_path):
    """The cap must not put a truncation notice on a log that fits."""
    results = tmp_path / "results"
    exec_dir = results / _CAMPAIGN / EXECUTION_DIR
    exec_dir.mkdir(parents=True)
    (exec_dir / "controller.log").write_text("only line\n", encoding="utf-8")
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    app = build_app(LocalTransport(store=store, results_dir=str(results)))

    body = _stream(app, Routes.campaign_logs_stream(_CAMPAIGN))
    assert "only line" in body
    assert "not shown" not in body
