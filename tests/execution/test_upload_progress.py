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
