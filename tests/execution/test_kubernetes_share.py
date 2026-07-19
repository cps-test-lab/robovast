# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""KubernetesBackend.share_campaign streams the raw campaign to the share provider.

With a provider configured it must stream (no disk tar) into
``provider.upload_archive_stream``; with none configured it must skip cleanly
(not a failure).
"""

import contextlib
import io
import types

from robovast.execution import campaign_archive
from robovast.execution.backends import RunOptions
from robovast.execution.cluster_execution import in_pod_upload
from robovast.execution.cluster_execution.kubernetes_backend import KubernetesBackend


class _FakeProvider:
    SHARE_TYPE = "fake"

    def __init__(self):
        self.uploaded = None

    def upload_archive_stream(self, fileobj, object_name, progress_callback=None):
        self.uploaded = (object_name, fileobj.read())


def _patch_stream(monkeypatch, payload=b"tar-bytes"):
    @contextlib.contextmanager
    def _fake_stream(campaign_root, exclude=None):
        yield io.BytesIO(payload)
    monkeypatch.setattr(campaign_archive, "campaign_tar_stream", _fake_stream)


def _backend():
    return KubernetesBackend(cluster_config=types.SimpleNamespace())


def test_share_campaign_streams_to_provider(monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", lambda: provider)
    monkeypatch.setattr(in_pod_upload, "verify_share_access", lambda p: None)
    _patch_stream(monkeypatch, b"tar-bytes")

    _backend().share_campaign("/scratch/camp-2026-01-01-000000", RunOptions())

    assert provider.uploaded == ("camp-2026-01-01-000000.tar.gz", b"tar-bytes")


def test_share_campaign_no_provider_is_noop(monkeypatch):
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", lambda: None)

    def _fail(*a, **k):
        raise AssertionError("must not stream without a provider")
    monkeypatch.setattr(campaign_archive, "campaign_tar_stream", _fail)

    # Does not raise, does not stream.
    _backend().share_campaign("/scratch/camp-2026-01-01-000000", RunOptions())
