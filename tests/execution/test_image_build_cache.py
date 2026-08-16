# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""In-cluster build caching: the registry layer cache and the manifest existence probe.

A fresh BuildKit pod has no local cache, so the registry cache ref is the only layer reuse
available in-cluster; and the manifest probe is what makes a cache hit outlive the build
Job's 1 h TTL. Both are asserted here because a mistake in either is silent — the build
simply gets slow again, or (worse, see ``registry_client``) claims a hit for an image that
was never pushed.
"""

import json

import pytest

from robovast.execution.cluster_execution.cluster_image_build import (build_job_manifest,
                                                                      cache_image_ref,
                                                                      concrete_image_ref)
from robovast.execution.cluster_execution.registry_client import (credentials_for, manifest_exists,
                                                                  split_image_ref)


def _buildctl(**over):
    kwargs = dict(build_id="imgbuild-x-abc", image_ref="reg.local:5000/x:abc",
                  campaign_label="imgbuild-x-abc", init_env=[],
                  push_secret_name="push", namespace="ns")
    kwargs.update(over)
    manifest = build_job_manifest(**kwargs)
    return manifest['spec']['template']['spec']['containers'][0]['command'][-1]


# ---------------------------------------------------------------------------
# cache ref naming
# ---------------------------------------------------------------------------

def test_cache_ref_is_not_hash_qualified():
    """The whole point is importing layers built for a *different* hash."""
    a = concrete_image_ref("reg.local:5000/rv", "sim", "aaaaaaaaaaaa")
    b = concrete_image_ref("reg.local:5000/rv", "sim", "bbbbbbbbbbbb")
    assert a != b
    cache = cache_image_ref("reg.local:5000/rv", "sim")
    assert cache == "reg.local:5000/rv/sim:buildcache"
    assert "aaaaaaaaaaaa" not in cache


def test_cache_ref_folds_tag_version_like_the_image_ref():
    assert cache_image_ref("reg/rv", "sim:v2") == "reg/rv/sim-v2:buildcache"


# ---------------------------------------------------------------------------
# buildctl invocation
# ---------------------------------------------------------------------------

def test_no_cache_flags_without_a_cache_ref():
    cmd = _buildctl()
    assert "--import-cache" not in cmd
    assert "--export-cache" not in cmd


def test_cache_import_and_export_use_mode_max():
    cmd = _buildctl(cache_ref="reg.local:5000/x:buildcache")
    assert "--import-cache type=registry,ref=reg.local:5000/x:buildcache" in cmd
    # mode=max exports intermediate layers too — without it only the final layer is
    # reusable, which is worthless for a chain of per-entry pip installs.
    assert "mode=max" in cmd
    # A failed cache *export* must not fail a build whose image is already pushed.
    assert "ignore-error=true" in cmd


def test_insecure_applies_to_cache_refs_not_only_the_output():
    """The cache refs address the same registry; missing the flag fails them on TLS."""
    cmd = _buildctl(cache_ref="reg.local:5000/x:buildcache", insecure=True)
    assert cmd.count("registry.insecure=true") == 3   # output + import + export


def test_ca_mount_suppresses_insecure_everywhere():
    """A mounted CA makes the registry properly trusted, so nothing is skipped."""
    cmd = _buildctl(cache_ref="reg.local:5000/x:buildcache", insecure=True,
                    ca_configmap_name="registry-ca")
    assert "registry.insecure=true" not in cmd
    assert "--import-cache" in cmd


# ---------------------------------------------------------------------------
# ref parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("reg.local:5000/rv/sim:abc123", ("reg.local:5000", "rv/sim", "abc123")),
    ("ghcr.io/org/sim:abc", ("ghcr.io", "org/sim", "abc")),
    # A registry port must not be mistaken for a tag.
    ("registry.local:5000/x/y", ("registry.local:5000", "x/y", "latest")),
])
def test_split_image_ref(ref, expected):
    assert split_image_ref(ref) == expected


def test_split_image_ref_rejects_unqualified():
    with pytest.raises(ValueError):
        split_image_ref("bare-name:tag")


def test_credentials_from_auth_blob():
    import base64
    auth = base64.b64encode(b"user:pw").decode()
    cfg = json.dumps({"auths": {"reg.local:5000": {"auth": auth}}})
    assert credentials_for(cfg, "reg.local:5000") == ("user", "pw")


def test_credentials_prefer_explicit_fields():
    cfg = json.dumps({"auths": {"h": {"username": "u", "password": "p"}}})
    assert credentials_for(cfg, "h") == ("u", "p")


def test_credentials_missing_host_is_none():
    assert credentials_for(json.dumps({"auths": {}}), "h") is None


def test_credentials_garbage_is_none():
    assert credentials_for("not json", "h") is None


# ---------------------------------------------------------------------------
# manifest_exists — must fail closed
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def head(self, url, **kw):
        self.calls.append(url)
        return self._responses.pop(0)


def _patch_session(monkeypatch, session):
    import requests
    monkeypatch.setattr(requests, "Session", lambda: session)


def test_manifest_present(monkeypatch):
    _patch_session(monkeypatch, _Session([_Resp(200)]))
    assert manifest_exists("reg.local:5000/x:abc") is True


def test_manifest_absent(monkeypatch):
    _patch_session(monkeypatch, _Session([_Resp(404)]))
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_unexpected_status_reports_absent(monkeypatch):
    """A 500 must never be read as a cache hit — that yields ImagePullBackOff."""
    _patch_session(monkeypatch, _Session([_Resp(500)]))
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_unreachable_registry_reports_absent(monkeypatch):
    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, *a, **kw):
            raise OSError("connection refused")

    _patch_session(monkeypatch, _Boom())
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_unsatisfiable_auth_challenge_reports_absent(monkeypatch):
    _patch_session(monkeypatch, _Session([_Resp(401, {"WWW-Authenticate": "Basic"})]))
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_bad_ref_reports_absent(monkeypatch):
    assert manifest_exists("bare-name") is False


def test_insecure_uses_http_and_skips_verify(monkeypatch):
    seen = {}

    class _S(_Session):
        def head(self, url, **kw):
            seen["url"] = url
            seen["verify"] = kw.get("verify")
            return _Resp(200)

    _patch_session(monkeypatch, _S([]))
    assert manifest_exists("reg.local:5000/x:abc", insecure=True) is True
    assert seen["url"].startswith("http://")
    assert seen["verify"] is False


def test_ca_path_is_passed_as_verify(monkeypatch):
    seen = {}

    class _S(_Session):
        def head(self, url, **kw):
            seen["verify"] = kw.get("verify")
            return _Resp(200)

    _patch_session(monkeypatch, _S([]))
    assert manifest_exists("reg.local:5000/x:abc", ca_path="/certs/ca.pem") is True
    assert seen["verify"] == "/certs/ca.pem"
