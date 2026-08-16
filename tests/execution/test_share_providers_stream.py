# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Provider streamed-upload (``upload_archive_stream``) behaviour.

The archive length is unknown while streaming, so WebDAV/Nextcloud send a chunked
body with **no** Content-Length, and GCS drives its resumable session with
``Content-Range: bytes X-Y/*`` chunks until a final chunk carries the total.
"""

import io

import pytest


# ---------------------------------------------------------------------------
# WebDAV — chunked PUT (generator body, no Content-Length)
# ---------------------------------------------------------------------------

class _FakeResp:
    status_code = 201
    text = ""


class _FakeSession:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def put(self, url, data=None, headers=None, timeout=None):
        self._sink["url"] = url
        self._sink["headers"] = headers or {}
        # A generator body is what makes requests use Transfer-Encoding: chunked.
        self._sink["is_generator"] = hasattr(data, "__next__")
        self._sink["body"] = b"".join(data)
        return _FakeResp()


def test_webdav_stream_sends_chunked_no_content_length(monkeypatch):
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "webdav")
    monkeypatch.setenv("ROBOVAST_WEBDAV_URL", "https://dav.example/col/")
    monkeypatch.setenv("ROBOVAST_WEBDAV_USER", "u")
    monkeypatch.setenv("ROBOVAST_WEBDAV_PASSWORD", "p")
    from robovast.execution.share_providers.webdav import \
        WebDavShareProvider

    provider = WebDavShareProvider()
    sink: dict = {}
    monkeypatch.setattr(provider, "_session", lambda: _FakeSession(sink))

    payload = b"z" * (600 * 1024)  # > 256 KiB chunk to force multiple yields
    progress = []
    provider.upload_archive_stream(
        io.BytesIO(payload), "camp.tar.gz",
        progress_callback=lambda sent, total: progress.append((sent, total)))

    assert sink["is_generator"] is True
    assert "Content-Length" not in {k.title(): v for k, v in sink["headers"].items()}
    assert sink["body"] == payload
    assert sink["url"].endswith("camp.tar.gz")
    assert progress[-1] == (len(payload), 0)  # unknown total reported as 0


# ---------------------------------------------------------------------------
# GCS — resumable chunk sequence: /* until the final chunk carries the total
# ---------------------------------------------------------------------------

def test_gcs_stream_chunks_ranges_and_final_total(monkeypatch):
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "gcs")
    monkeypatch.setenv("ROBOVAST_GCS_BUCKET", "bucket")
    from robovast.execution.share_providers import gcs as gcs_mod
    from robovast.execution.share_providers.gcs import \
        GcsShareProvider

    provider = GcsShareProvider()
    monkeypatch.setattr(provider, "_access_token_for_verify", lambda: "tok")
    monkeypatch.setattr(
        GcsShareProvider, "_gcs_initiate_resumable",
        staticmethod(lambda bucket, name, total, token: "https://session"))
    # Small chunk to force several resumable PUTs.
    monkeypatch.setattr(GcsShareProvider, "_STREAM_CHUNK", 4)

    chunks = []
    monkeypatch.setattr(
        GcsShareProvider, "_gcs_put_chunk",
        staticmethod(lambda uri, data, crange, is_last: chunks.append((data, crange, is_last))))

    payload = b"0123456789"  # 10 bytes -> chunks of 4,4,2
    provider.upload_archive_stream(io.BytesIO(payload), "camp.tar.gz")

    # Reassembles the payload and only the last chunk is final (carries the total).
    assert b"".join(d for d, _, _ in chunks) == payload
    finals = [is_last for _, _, is_last in chunks]
    assert finals == [False, False, True]
    assert chunks[0][1] == "bytes 0-3/*"
    assert chunks[1][1] == "bytes 4-7/*"
    assert chunks[-1][1] == "bytes 8-9/10"


def test_gcs_stream_empty_finalizes_zero_object(monkeypatch):
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "gcs")
    monkeypatch.setenv("ROBOVAST_GCS_BUCKET", "bucket")
    from robovast.execution.share_providers.gcs import \
        GcsShareProvider

    provider = GcsShareProvider()
    monkeypatch.setattr(provider, "_access_token_for_verify", lambda: "tok")
    monkeypatch.setattr(
        GcsShareProvider, "_gcs_initiate_resumable",
        staticmethod(lambda bucket, name, total, token: "https://session"))
    chunks = []
    monkeypatch.setattr(
        GcsShareProvider, "_gcs_put_chunk",
        staticmethod(lambda uri, data, crange, is_last: chunks.append((data, crange, is_last))))

    provider.upload_archive_stream(io.BytesIO(b""), "camp.tar.gz")
    assert chunks == [(b"", "bytes */0", True)]
