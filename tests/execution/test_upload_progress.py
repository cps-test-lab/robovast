# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for in-process upload + the monitor's upload-progress plumbing.

Covers the path that replaced the old upload subprocess: the shared
``UploadProgressReader``, ``in_pod_upload.upload_campaign`` forwarding a progress
callback to ``provider.upload_archive``, and the controller callback that
publishes ``(sent, total, rate)`` into ``Status.extra['upload']``.
"""

import io
import os

from robovast.execution.cluster_execution import in_pod_upload
from robovast.execution.cluster_execution.share_providers.base import \
    UploadProgressReader


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
# in_pod_upload.upload_campaign — forwards the callback, cleans up on success
# ---------------------------------------------------------------------------

class _FakeConfig:
    """Cluster config stub that writes a dummy archive on compress."""

    def __init__(self, payload=b"archive-bytes"):
        self._payload = payload

    def compress_campaign(self, campaign_id, archive_dir):
        path = os.path.join(archive_dir, f"{campaign_id}.tar.gz")
        with open(path, "wb") as fh:
            fh.write(self._payload)
        return path


class _FakeProvider:
    SHARE_TYPE = "fake"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def upload_archive(self, archive_path, object_name, progress_callback=None):
        self.calls.append((archive_path, object_name))
        if self.fail:
            raise RuntimeError("boom")
        if progress_callback:
            total = os.path.getsize(archive_path)
            progress_callback(total, total)


def test_upload_campaign_success_forwards_progress_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOVAST_ARCHIVE_DIR", str(tmp_path))
    provider = _FakeProvider()
    seen = []
    ok = in_pod_upload.upload_campaign(
        _FakeConfig(), "camp-2026-01-01-000000", provider,
        progress_cb=lambda sent, total: seen.append((sent, total)))
    assert ok is True
    assert provider.calls == [
        (str(tmp_path / "camp-2026-01-01-000000.tar.gz"),
         "camp-2026-01-01-000000.tar.gz")]
    assert seen and seen[-1][0] == seen[-1][1]            # progress forwarded
    assert not (tmp_path / "camp-2026-01-01-000000.tar.gz").exists()  # cleaned up


def test_upload_campaign_failure_returns_false_and_keeps_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOVAST_ARCHIVE_DIR", str(tmp_path))
    ok = in_pod_upload.upload_campaign(
        _FakeConfig(), "camp-2026-01-01-000000", _FakeProvider(fail=True))
    assert ok is False
    # Archive is preserved so a retrigger can reuse it.
    assert (tmp_path / "camp-2026-01-01-000000.tar.gz").exists()


# ---------------------------------------------------------------------------
# controller._make_upload_progress_cb — publishes into Status.extra['upload']
# ---------------------------------------------------------------------------

class _RecordingState:
    def __init__(self):
        self.extra = {}

    def update(self, **fields):
        if "extra" in fields:
            self.extra = fields["extra"]


def test_progress_cb_publishes_sent_total_and_rate():
    from robovast.execution.controller import _make_upload_progress_cb

    state = _RecordingState()
    cb = _make_upload_progress_cb(state)
    cb(0, 1000)                       # first sample always pushes
    assert state.extra["upload"]["sent"] == 0
    assert state.extra["upload"]["total"] == 1000
    assert state.extra["upload"]["rate"] is None
    cb(1000, 1000)                    # completion bypasses throttle, derives a rate
    up = state.extra["upload"]
    assert up["sent"] == 1000
    assert up["rate"] is not None and up["rate"] >= 0


def test_progress_cb_none_without_state():
    from robovast.execution.controller import _make_upload_progress_cb
    assert _make_upload_progress_cb(None) is None