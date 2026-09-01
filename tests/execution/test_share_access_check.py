# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The WebDAV share's pre-flight: what each refusal is reported as.

``verify_access`` runs before a campaign's archive is streamed anywhere, so its
message is the only thing a reader gets — the campaign continues either way. Each
status therefore has to name the setting to change, and the check must not report a
server that declines the method as a collection that is not there.
"""

import click
import pytest

from robovast.execution.share_providers.base import ShareError


class _Resp:
    def __init__(self, status, reason=""):
        self.status_code = status
        self.reason = reason


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, headers=None, timeout=None):
        assert method == "PROPFIND"
        return self._resp


def _provider(monkeypatch, status, reason=""):
    monkeypatch.setenv("ROBOVAST_WEBDAV_URL", "https://dav.example/col/")
    monkeypatch.setenv("ROBOVAST_WEBDAV_USER", "u")
    monkeypatch.setenv("ROBOVAST_WEBDAV_PASSWORD", "p")
    from robovast.execution.share_providers.webdav import WebDavShareProvider

    provider = WebDavShareProvider()
    monkeypatch.setattr(provider, "_session", lambda: _Session(_Resp(status, reason)))
    return provider


@pytest.mark.parametrize("status", [200, 204, 207])
def test_accepts_the_success_statuses(monkeypatch, status):
    _provider(monkeypatch, status).verify_access()


def test_method_not_allowed_is_not_reported_as_a_missing_collection(monkeypatch):
    """405 says the *server* declines PROPFIND, which a URL check cannot fix.

    The endpoint may be a plain web address for the same storage, or WebDAV may be
    off for the account; both leave ROBOVAST_WEBDAV_URL looking correct, so telling
    the reader to check it sends them to re-verify a setting that is right.
    """
    with pytest.raises(ShareError) as excinfo:
        _provider(monkeypatch, 405, "Method Not Allowed").verify_access()
    message = str(excinfo.value)
    assert "does not answer WebDAV requests" in message
    assert "HTTP 405" in message
    assert "no WebDAV collection" not in message


def test_forbidden_says_permission_rather_than_password(monkeypatch):
    with pytest.raises(ShareError) as excinfo:
        _provider(monkeypatch, 403).verify_access()
    message = str(excinfo.value)
    assert "not allowed to list" in message
    assert "PASSWORD" not in message


def test_unauthorized_names_the_credential_settings(monkeypatch):
    with pytest.raises(ShareError) as excinfo:
        _provider(monkeypatch, 401).verify_access()
    assert "ROBOVAST_WEBDAV_USER" in str(excinfo.value)


def test_not_found_names_the_url_setting(monkeypatch):
    with pytest.raises(ShareError) as excinfo:
        _provider(monkeypatch, 404).verify_access()
    assert "ROBOVAST_WEBDAV_URL" in str(excinfo.value)


def test_share_errors_carry_no_traceback_and_stay_click_usage_errors(monkeypatch):
    """The share step is best-effort, so its refusal must read as an answer.

    ``failure_detail`` appends a stack for anything that does not opt out, and the
    frames through ``requests`` name nothing the message does not — while a stack in
    the campaign log reads as a crash in a campaign that in fact finished.
    """
    from robovast.client.status import failure_detail

    with pytest.raises(ShareError) as excinfo:
        _provider(monkeypatch, 405).verify_access()
    exc = excinfo.value
    assert isinstance(exc, click.UsageError)  # `vast share` still prints it as usage
    assert exc.include_traceback is False
    assert "Traceback" not in failure_detail(exc)
