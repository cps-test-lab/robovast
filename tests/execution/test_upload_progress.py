# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the share upload-progress plumbing.

Covers the shared ``UploadProgressReader`` (path-based providers), the
``StreamProgressReader`` used by the streamed (on-the-fly) upload path, and the
controller callback that publishes ``(sent, total, rate)`` into
``Status.extra['upload']``.
"""

# pylint: disable=import-outside-toplevel

import io

from robovast.execution.share_providers.base import StreamProgressReader, UploadProgressReader

# ---------------------------------------------------------------------------
# UploadProgressReader
# ---------------------------------------------------------------------------

def test_progress_reader_reports_cumulative_sent():
    data = b"x" * 1000
    samples = []
    reader = UploadProgressReader(
        io.BytesIO(data), total=len(data),
        progress_callback=lambda sent, total: samples.append((sent, total)))
    # Drain it the way urllib/requests would.
    while reader.read(256):
        pass
    assert samples, "callback was never invoked"
    sents = [s for s, _ in samples]
    assert sents == sorted(sents)          # monotonic
    assert samples[-1] == (1000, 1000)     # reaches total
    assert all(t == 1000 for _, t in samples)


def test_progress_reader_len_excludes_resume_offset():
    # __len__ must report only the bytes streamed this session so urllib/requests
    # set Content-Length correctly on a resumed upload.
    reader = UploadProgressReader(io.BytesIO(b"y" * 1000), total=1000, start_offset=400)
    assert len(reader) == 600
    samples = []
    reader = UploadProgressReader(
        io.BytesIO(b"y" * 600), total=1000, start_offset=400,
        progress_callback=lambda sent, total: samples.append(sent))
    while reader.read(256):
        pass
    # Cumulative sent starts at the offset and ends at total.
    assert samples[0] >= 400
    assert samples[-1] == 1000


def test_progress_reader_no_callback_is_noop():
    reader = UploadProgressReader(io.BytesIO(b"z" * 10), total=10)
    assert reader.read() == b"z" * 10  # does not raise without a callback


# ---------------------------------------------------------------------------
# StreamProgressReader — unknown length; reports (sent, 0), no __len__
# ---------------------------------------------------------------------------

def test_stream_progress_reader_reports_sent_with_zero_total():
    data = b"a" * 700
    samples = []
    reader = StreamProgressReader(
        io.BytesIO(data), progress_callback=lambda sent, total: samples.append((sent, total)))
    out = b""
    while True:
        chunk = reader.read(256)
        if not chunk:
            break
        out += chunk
    assert out == data
    assert samples[-1] == (700, 0)             # cumulative sent, unknown total = 0
    assert [s for s, _ in samples] == sorted(s for s, _ in samples)


def test_stream_progress_reader_has_no_len_or_fileno():
    # Absence of __len__/fileno is what forces http.client into chunked transfer.
    reader = StreamProgressReader(io.BytesIO(b"q" * 4))
    assert not hasattr(reader, "__len__")
    assert not hasattr(reader, "fileno")
    assert reader.read() == b"q" * 4          # no callback -> no error


# ---------------------------------------------------------------------------
# controller.make_upload_progress_cb — publishes into Status.extra['upload']
# ---------------------------------------------------------------------------

class _RecordingState:
    def __init__(self):
        self.extra = {}

    def update(self, **fields):
        if "extra" in fields:
            self.extra = fields["extra"]


def test_progress_cb_publishes_sent_total_and_rate():
    from robovast.execution.controller import make_upload_progress_cb

    state = _RecordingState()
    cb = make_upload_progress_cb(state)
    cb(0, 1000)                       # first sample always pushes
    assert state.extra["upload"]["sent"] == 0
    assert state.extra["upload"]["total"] == 1000
    assert state.extra["upload"]["rate"] is None
    cb(1000, 1000)                    # completion bypasses throttle, derives a rate
    up = state.extra["upload"]
    assert up["sent"] == 1000
    assert up["rate"] is not None and up["rate"] >= 0


def test_progress_cb_none_without_state():
    from robovast.execution.controller import make_upload_progress_cb
    assert make_upload_progress_cb(None) is None


# ---------------------------------------------------------------------------
# UploadProgress — the streamed path, where the provider's total is always 0
# ---------------------------------------------------------------------------

class _CountingState:
    """A ControllerState stand-in that also counts how often it was written."""

    def __init__(self):
        self.extra = {}
        self.writes = 0

    def update(self, **fields):
        if "extra" in fields:
            self.extra = fields["extra"]
            self.writes += 1


def test_unknown_total_does_not_defeat_the_throttle():
    """A streamed upload reports ``(sent, 0)``; that must still be throttled.

    The old guard ANDed in ``sent < total``, which with ``total == 0`` is false for
    every sample — so the "publish the final sample regardless" clause silently became
    "publish every sample", and a campaign-sized upload wrote a status update per
    256 KiB chunk.
    """
    from robovast.execution.controller import make_upload_progress_cb

    state = _CountingState()
    cb = make_upload_progress_cb(state)
    for i in range(1, 2001):
        cb(i * 256 * 1024, 0)
    # Time-throttled at 0.5 s, so a tight loop publishes once or twice — never per call.
    # (The very last sample may therefore go unpublished; that is what throttling *is*,
    # and the source counter reaching 100% is what guarantees the bar's final frame.)
    assert state.writes <= 5, f"{state.writes} status writes for 2000 chunks"
    assert state.extra["upload"]["sent"] > 0


def test_source_counters_drive_the_percentage():
    from robovast.execution.controller import make_upload_progress_cb

    state = _CountingState()
    cb = make_upload_progress_cb(state)
    cb.set_source_total(1000)
    assert state.extra["upload"]["source_total"] == 1000
    assert state.extra["upload"]["percent"] == 0.0
    # A wire sample first, so the compressed count is in the record before the source
    # side finishes: the two are reported side by side, not one derived from the other.
    cb(123, 0)
    for _ in range(10):
        cb.on_member(100)
    up = state.extra["upload"]
    assert up["source_done"] == 1000
    # Landing on 100% is published even though the throttle window has not elapsed.
    assert up["percent"] == 100.0
    assert up["sent"] == 123


def test_percent_is_none_without_a_source_total():
    from robovast.execution.controller import make_upload_progress_cb

    state = _CountingState()
    cb = make_upload_progress_cb(state)
    cb(4096, 0)
    up = state.extra["upload"]
    assert up["percent"] is None      # a reader shows an indeterminate bar, not 0%
    assert up["sent"] == 4096


def test_a_known_provider_total_still_fills_the_bar():
    """The path-based (resumable) upload knows its total; it must not lose the bar."""
    from robovast.execution.controller import make_upload_progress_cb

    state = _CountingState()
    cb = make_upload_progress_cb(state)
    cb(500, 1000)
    assert state.extra["upload"]["percent"] == 50.0
    cb(1000, 1000)
    assert state.extra["upload"]["percent"] == 100.0


def test_rate_survives_a_source_only_sample():
    """`on_member` advances the source side while `sent` stands still.

    Re-deriving the rate from that zero delta would report a stalled transfer in the
    middle of a healthy one.
    """
    import time as _time

    from robovast.execution.controller import make_upload_progress_cb

    state = _CountingState()
    cb = make_upload_progress_cb(state)
    cb.set_source_total(10_000)
    cb(1_000, 0)
    _time.sleep(0.6)
    cb(5_000, 0)
    rate = state.extra["upload"]["rate"]
    assert rate is not None and rate > 0
    for _ in range(50):
        cb.on_member(100)             # source moves, wire does not
    assert state.extra["upload"]["rate"] == rate


def test_finish_publishes_the_last_wire_sample():
    """The two counters do not end together, so the final frame needs forcing.

    The source side reaches 100% when the archiver reads the last file; bytes keep
    leaving while the compressor's buffer drains. Those trailing samples advance no
    percentage and (on a short upload) are inside the half-second window, so they are
    throttled away — which left the record reading ``100%`` and ``0 B sent``.
    """
    from robovast.execution.controller import make_upload_progress_cb

    state = _CountingState()
    cb = make_upload_progress_cb(state)
    cb.set_source_total(1000)
    for _ in range(10):
        cb.on_member(100)             # source hits 100% and publishes
    cb(900, 0)                        # ... then the wire drains, throttled away
    assert state.extra["upload"]["sent"] == 0
    cb.finish()
    assert state.extra["upload"]["sent"] == 900
    assert state.extra["upload"]["percent"] == 100.0
