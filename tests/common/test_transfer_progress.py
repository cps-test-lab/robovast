# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The transfer progress bar, and the case with no total to divide by.

A campaign archive is tarred on the fly, so the response carries no ``Content-Length``
and the callback is driven with ``total=0``. That branch was unthrottled -- there is no
percentage to advance, which is exactly what the sized branch throttles on -- so it wrote
once per chunk. Downloading a 12 MB archive produced ~150 KB of carriage returns: one
line in a terminal, and every one of them in a log, a CI job, or captured tool output.
"""

import io
import time

from robovast.common import make_transfer_progress_callback


def _capture(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    return buf


def test_an_unsized_transfer_does_not_write_once_per_chunk(monkeypatch):
    buf = _capture(monkeypatch)
    cb = make_transfer_progress_callback("camp", time.monotonic())
    for i in range(1, 2001):          # 2000 chunks, as a multi-MB archive delivers
        cb(i * 1024 * 1024, 0)        # total=0: the service could not say how big it is
    # A handful of lines, not one per chunk. The exact count is time-dependent; the
    # property is that it is bounded by elapsed time rather than by chunk count.
    assert buf.getvalue().count("\r") < 50


def test_an_unsized_transfer_still_reports_something(monkeypatch):
    # Throttling must not become silence: a share download is minutes of somebody else's
    # storage, and no output at all is indistinguishable from a hang.
    buf = _capture(monkeypatch)
    cb = make_transfer_progress_callback("camp", time.monotonic())
    cb(1024 * 1024, 0)
    assert "camp" in buf.getvalue()


def test_a_sized_transfer_still_draws_a_bar(monkeypatch):
    buf = _capture(monkeypatch)
    cb = make_transfer_progress_callback("camp", time.monotonic())
    cb(50, 100)
    cb(100, 100)
    out = buf.getvalue()
    assert "50.0%" in out and "100.0%" in out
