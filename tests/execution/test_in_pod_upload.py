# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the in-controller upload-to-share helper."""

# pylint: disable=import-outside-toplevel

import pytest

from robovast.execution.cluster_execution import in_pod_upload


def _clear_share_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("ROBOVAST_SHARE") or key.startswith("ROBOVAST_WEBDAV"):
            monkeypatch.delenv(key, raising=False)


def test_load_provider_none_when_unconfigured(monkeypatch):
    _clear_share_env(monkeypatch)
    assert in_pod_upload.load_provider_from_env() is None
    assert in_pod_upload.share_type_configured() is False


def test_load_provider_unknown_type_raises(monkeypatch):
    _clear_share_env(monkeypatch)
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "no-such-provider")
    with pytest.raises(ValueError):
        in_pod_upload.load_provider_from_env()


def test_load_provider_webdav_from_env(monkeypatch):
    _clear_share_env(monkeypatch)
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "webdav")
    monkeypatch.setenv("ROBOVAST_WEBDAV_URL", "https://nas.example.com/dav/")
    monkeypatch.setenv("ROBOVAST_WEBDAV_USER", "u")
    monkeypatch.setenv("ROBOVAST_WEBDAV_PASSWORD", "p")
    provider = in_pod_upload.load_provider_from_env()
    assert provider is not None and provider.SHARE_TYPE == "webdav"


def test_load_provider_applies_overrides(monkeypatch):
    # Overrides (from a retrigger command) populate os.environ before the
    # provider validates its required vars.
    _clear_share_env(monkeypatch)
    provider = in_pod_upload.load_provider_from_env(overrides={
        "ROBOVAST_SHARE_TYPE": "webdav",
        "ROBOVAST_WEBDAV_URL": "https://nas.example.com/dav/",
        "ROBOVAST_WEBDAV_USER": "u",
        "ROBOVAST_WEBDAV_PASSWORD": "secret",
    })
    assert provider is not None and provider.SHARE_TYPE == "webdav"


def test_overrides_switch_provider_type(monkeypatch):
    # A retrigger can switch the share type: even with another type configured in
    # the environment, overrides naming a new type load that provider.
    _clear_share_env(monkeypatch)
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "nextcloud")
    monkeypatch.setenv("ROBOVAST_SHARE_URL", "https://cloud.example.com/s/tok")
    provider = in_pod_upload.load_provider_from_env(overrides={
        "ROBOVAST_SHARE_TYPE": "webdav",
        "ROBOVAST_WEBDAV_URL": "https://nas.example.com/dav/",
        "ROBOVAST_WEBDAV_USER": "u",
        "ROBOVAST_WEBDAV_PASSWORD": "secret",
    })
    assert provider.SHARE_TYPE == "webdav"


def test_switch_with_missing_var_raises(monkeypatch):
    # Switching to a provider without its required vars must fail (not silently
    # fall back) — the constructor raises click.UsageError.
    import click
    _clear_share_env(monkeypatch)
    with pytest.raises(click.UsageError):
        in_pod_upload.load_provider_from_env(overrides={
            "ROBOVAST_SHARE_TYPE": "webdav",  # missing URL/USER/PASSWORD
        })
