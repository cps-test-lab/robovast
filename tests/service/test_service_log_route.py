# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service serves its own log, with offsets that mean what they say.

The ring exists because stderr is not readable back, and it speaks the ``LogChunk``
protocol so the SSE tail in ``app.py`` streams it unchanged. That contract is what these
pin: resuming from ``next_offset`` returns only what is new, and a reader that has fallen
off the back of the window is served a gap rather than a duplicate.
"""

import logging

import pytest

from robovast.service import service_log
from robovast.service.interface import Routes


@pytest.fixture(autouse=True)
def ring():
    """A fresh ring per test, and the handler attached exactly once.

    The ring is process-global -- a logging handler cannot sensibly be otherwise -- so
    tests reset it rather than construct it.
    """
    service_log.install()
    # pylint: disable=protected-access
    service_log._RING._buf.clear()
    service_log._RING._base = 0
    yield service_log._RING


def test_it_holds_what_this_process_logged():
    logging.getLogger("robovast.test.ring").warning("a thing happened")
    assert "a thing happened" in service_log.read(0).text


def test_resuming_from_next_offset_returns_only_what_is_new():
    log = logging.getLogger("robovast.test.ring")
    log.warning("first")
    chunk = service_log.read(0)
    assert "first" in chunk.text
    assert service_log.read(chunk.next_offset).text == "", "a quiet tail must send nothing"
    log.warning("second")
    later = service_log.read(chunk.next_offset)
    assert "second" in later.text and "first" not in later.text


def test_a_running_service_never_reports_eof():
    """``eof`` would tell ``_sse_log_stream`` to close a stream that should keep tailing."""
    assert service_log.read(0).eof is False


def test_a_reader_behind_the_window_gets_a_gap_not_a_duplicate(ring, monkeypatch):
    """Once bytes are evicted the offset cannot be honoured; a consistent stream wins.

    The alternative -- replaying from wherever the offset happens to land -- would resend
    text the reader already has, which is worse than a visible gap: it corrupts a log
    somebody is reading rather than merely shortening it.
    """
    monkeypatch.setattr(service_log, "_MAX_BYTES", 200)
    log = logging.getLogger("robovast.test.ring")
    log.warning("x" * 150)
    stale = service_log.read(0).next_offset
    for _ in range(5):
        log.warning("y" * 150)          # push the first line out of the window
    # pylint: disable=protected-access
    assert ring._base > stale, "the window did not actually slide; the test proves nothing"
    resumed = service_log.read(stale)
    assert "x" * 150 not in resumed.text
    assert resumed.next_offset == ring._base + len(ring._buf)


def test_an_offset_past_the_end_resyncs_rather_than_going_silent():
    """A restarted process begins at 0 again, so a browser can resume above the end.

    Clamping would return empty forever, which reads as a service gone quiet -- the one
    thing a log panel must not do.
    """
    logging.getLogger("robovast.test.ring").warning("after a restart")
    assert "after a restart" in service_log.read(10_000_000).text


def test_the_routes_are_registered_and_read_only(tmp_path):
    from robovast.service.app import api_routes, build_app
    from robovast.service.local_transport import LocalTransport
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    routes = {r.path: r.methods for r in api_routes(build_app(LocalTransport(store=store),
                                                              mount_mcp=False))}
    for path in (Routes.ADMIN_LOG, Routes.ADMIN_LOG_STREAM):
        assert path in routes, f"{path} is not served"
        assert routes[path] - {"HEAD"} == {"GET"}, "reading a log must not be a write"
