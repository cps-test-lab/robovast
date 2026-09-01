# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""KubernetesBackend.share_campaign streams the raw campaign to the share provider.

With a provider configured it must stream (no disk tar) into
``provider.upload_archive_stream``; with none configured it must fail loudly
(``preflight_upload_to_share`` rejects it up front, and ``share_campaign`` is a
defensive backstop) rather than silently skip.
"""

import contextlib
import io
import types

import pytest

from robovast.execution import campaign_archive
from robovast.execution.backends import CampaignConfigError, RunOptions
from robovast.execution.cluster_execution import in_pod_upload
from robovast.execution.cluster_execution.kubernetes_backend import KubernetesBackend


class _FakeProvider:
    SHARE_TYPE = "fake"

    def __init__(self):
        self.uploaded = None
        self.progress_callback = None

    def upload_archive_stream(self, fileobj, object_name, progress_callback=None):
        self.uploaded = (object_name, fileobj.read())
        self.progress_callback = progress_callback


def _patch_stream(monkeypatch, payload=b"tar-bytes"):
    @contextlib.contextmanager
    def _fake_stream(campaign_root, exclude=None, on_member=None):
        yield io.BytesIO(payload)
    monkeypatch.setattr(campaign_archive, "campaign_tar_stream", _fake_stream)


def _backend():
    return KubernetesBackend(cluster_config=types.SimpleNamespace())


def test_share_campaign_streams_to_provider_naming_the_variant(monkeypatch, tmp_path):
    provider = _FakeProvider()
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", lambda: provider)
    monkeypatch.setattr(in_pod_upload, "verify_share_access", lambda p: None)
    _patch_stream(monkeypatch, b"tar-bytes")
    campaign = tmp_path / "camp-2026-01-01-000000"
    (campaign / "_execution").mkdir(parents=True)

    _backend().share_campaign(str(campaign), RunOptions(),
                              progress_callback="the-callback")

    # Called from the finish tail, so postprocessing has not run: raw, and named so.
    assert provider.uploaded == ("camp-2026-01-01-000000.raw.tar.gz", b"tar-bytes")
    # Passing it is the point: this was dropped, so the upload bar the campaign view
    # renders never had anything to render.
    assert provider.progress_callback == "the-callback"


def test_share_campaign_names_a_postprocessed_campaign_as_such(monkeypatch, tmp_path):
    # A later `vast share export` of the same campaign finds postprocessing's provenance
    # record and says so. Nobody passes the variant in -- both callers read it off the
    # tree, so they cannot disagree.
    provider = _FakeProvider()
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", lambda: provider)
    monkeypatch.setattr(in_pod_upload, "verify_share_access", lambda p: None)
    _patch_stream(monkeypatch, b"tar-bytes")
    campaign = tmp_path / "camp-2026-01-01-000000"
    (campaign / "_transient").mkdir(parents=True)
    (campaign / "_transient" / "postprocessing.yaml").write_text(
        "generated_by: robovast\nentries:\n  - output: run-0/nav.csv\n"
        "    sources: [run-0/rosbag2]\n    plugin: rosbags_to_csv\n    params: {}\n",
        encoding="utf-8")

    _backend().share_campaign(str(campaign), RunOptions())

    assert provider.uploaded[0] == "camp-2026-01-01-000000.postprocessed.tar.gz"


def test_preflight_raises_without_share_configured(monkeypatch):
    monkeypatch.setattr(in_pod_upload, "share_type_configured", lambda: False)
    with pytest.raises(CampaignConfigError):
        _backend().preflight_upload_to_share()


def test_preflight_ok_when_share_configured(monkeypatch):
    monkeypatch.setattr(in_pod_upload, "share_type_configured", lambda: True)
    # Must not raise when a share type is set.
    _backend().preflight_upload_to_share()


def test_share_campaign_no_provider_raises(monkeypatch):
    monkeypatch.setattr(in_pod_upload, "load_provider_from_env", lambda: None)

    def _fail(*a, **k):
        raise AssertionError("must not stream without a provider")
    monkeypatch.setattr(campaign_archive, "campaign_tar_stream", _fail)

    # Defensive backstop: raises loudly rather than silently skipping.
    with pytest.raises(CampaignConfigError):
        _backend().share_campaign("/scratch/camp-2026-01-01-000000", RunOptions())
