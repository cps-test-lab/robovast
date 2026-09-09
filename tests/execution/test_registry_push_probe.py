# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Whether a push will be accepted, asked before the build rather than by attempting it.

An experiment image built through every layer, installed every package, and failed at its
final step with a 401 from the registry. Nothing upstream had reason to warn: the
capability flag a client reads says a registry is *configured*, and the cache probe that
runs just before a build is a manifest read -- which a registry may serve to anyone while
refusing to receive one. So an ``ABSENT`` from that probe was taken as "this tag is not
published yet", which is true, and read as "the push that publishes it will work", which
is a different sentence.

The probe here asks the only question that answers it: open a blob upload, which is the
first thing a push does and the first thing that needs push scope, and cancel it again.
"""

import functools
import types

import pytest

from robovast.execution.cluster_execution import registry_client
from robovast.execution.cluster_execution.registry_client import (PUSH_ALLOWED, PUSH_REFUSED,
                                                                  PUSH_UNKNOWN, push_state)

_REF = "repo.example.com/robovast/sut:h"
_LOCATION = "/v2/robovast/sut/blobs/uploads/95e0c0b0-1d1c"


def _resp(status, headers=None):
    return types.SimpleNamespace(status_code=status, headers=headers or {},
                                 json=lambda: {})


def _refuse(status, _path, **_kw):
    return _resp(status)


def _registry(monkeypatch, handler):
    """Install *handler* as the module's one request path; return the calls it saw."""
    seen: list = []

    def _request(host, path, **kwargs):
        seen.append((kwargs.get("method"), path, kwargs.get("token_scope")))
        return handler(path, **kwargs)

    monkeypatch.setattr(registry_client, "_registry_request", _request)
    return seen


# -- the three verdicts ------------------------------------------------------


def test_a_registry_that_opens_the_upload_may_be_pushed_to(monkeypatch):
    seen = _registry(monkeypatch, lambda path, **kw: _resp(202, {"Location": _LOCATION}))

    assert push_state(_REF) == PUSH_ALLOWED
    assert ("POST", "robovast/sut/blobs/uploads/", "push") in seen


def test_a_refused_credential_is_a_refusal_and_not_an_unknown(monkeypatch):
    """The whole point: this is the state that used to be discovered after the build."""
    for status in (401, 403):
        _registry(monkeypatch, functools.partial(_refuse, status))
        assert push_state(_REF) == PUSH_REFUSED, f"status {status}"


def test_a_registry_that_did_not_answer_is_unknown_not_refused(monkeypatch):
    """``None`` is "could not ask". Reading it as a refusal would block a build over a
    registry that is merely unreachable -- which the lane already survives."""
    _registry(monkeypatch, lambda path, **kw: None)
    assert push_state(_REF) == PUSH_UNKNOWN


def test_a_status_that_is_neither_is_unknown(monkeypatch):
    _registry(monkeypatch, lambda path, **kw: _resp(500))
    assert push_state(_REF) == PUSH_UNKNOWN


def test_a_ref_that_is_not_registry_qualified_is_unknown(monkeypatch):
    """It never reaches a registry, so nothing was refused."""
    _registry(monkeypatch, lambda path, **kw: _resp(202, {"Location": _LOCATION}))
    assert push_state("bare-image:tag") == PUSH_UNKNOWN


# -- the probe leaves nothing behind ----------------------------------------


def test_the_upload_it_opened_is_cancelled_again(monkeypatch):
    seen = _registry(monkeypatch, lambda path, **kw: _resp(202, {"Location": _LOCATION}))

    push_state(_REF)

    assert ("DELETE", "robovast/sut/blobs/uploads/95e0c0b0-1d1c", "push") in seen, \
        f"the upload session was left open: {seen}"


def test_an_absolute_location_is_cancelled_at_the_right_path(monkeypatch):
    """A registry may answer with a full URL rather than a path; only the path is ours."""
    absolute = "https://repo.example.com" + _LOCATION
    seen = _registry(monkeypatch, lambda path, **kw: _resp(201, {"Location": absolute}))

    push_state(_REF)

    assert ("DELETE", "robovast/sut/blobs/uploads/95e0c0b0-1d1c", "push") in seen


def test_a_registry_that_names_no_location_is_still_allowed(monkeypatch):
    """It accepted the upload, which is the answer asked for. Nothing to cancel."""
    seen = _registry(monkeypatch, lambda path, **kw: _resp(202))

    assert push_state(_REF) == PUSH_ALLOWED
    assert not [c for c in seen if c[0] == "DELETE"]


def test_nothing_is_cancelled_when_the_push_was_refused(monkeypatch):
    seen = _registry(monkeypatch, lambda path, **kw: _resp(401))

    push_state(_REF)

    assert not [c for c in seen if c[0] == "DELETE"]


# -- a pull token is not a push token ---------------------------------------


class _Exceptions:
    class Timeout(Exception):
        pass

    class ConnectionError(Exception):  # noqa: A001 - mirrors requests' own name
        pass

    class ChunkedEncodingError(Exception):
        pass


class _Session:
    """A registry that challenges once, then accepts the token it issued."""

    def __init__(self, tokens):
        self.tokens = tokens

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url, **kwargs):
        if "/token" in url:
            self.tokens.append(kwargs.get("params") or {})
            return types.SimpleNamespace(
                status_code=200, headers={},
                json=lambda: {"token": "tok", "expires_in": 300})
        return self._answer(kwargs)

    post = delete = head = get

    @staticmethod
    def _answer(kwargs):
        if (kwargs.get("headers") or {}).get("Authorization") == "Bearer tok":
            return types.SimpleNamespace(status_code=202,
                                         headers={"Location": _LOCATION},
                                         json=lambda: {})
        return types.SimpleNamespace(
            status_code=401, json=lambda: {},
            headers={"WWW-Authenticate":
                     'Bearer realm="https://repo.example.com/token",service="r"'})


@pytest.fixture(name="sessions")
def _sessions(monkeypatch):
    monkeypatch.setattr(registry_client, "_TOKENS", {})
    tokens: list = []

    class _Requests:
        @staticmethod
        def Session():  # pylint: disable=invalid-name
            return _Session(tokens)

        exceptions = _Exceptions

    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)
    return tokens


def _cfg():
    import base64
    import json
    return json.dumps({"auths": {"repo.example.com": {
        "auth": base64.b64encode(b"u:p").decode()}}})


def test_a_pull_token_is_never_presented_for_a_push(sessions):
    """A token grants one access level, not the repository as such.

    Sharing the cache entry across levels is not just a wasted round trip: the stale-scope
    token earns a 401, and to this probe a 401 is a credential that may not push.
    """
    registry_client._registry_request(  # noqa: SLF001 - the unit under test
        "repo.example.com", "robovast/sut/manifests/h", method="HEAD",
        dockerconfigjson=_cfg())
    assert len(sessions) == 1

    assert push_state(_REF, dockerconfigjson=_cfg()) == PUSH_ALLOWED

    scopes = [t.get("scope") for t in sessions]
    assert len(sessions) >= 2, (
        f"the push request reused the pull token: {scopes}")
    keys = [k[-1] for k in registry_client._TOKENS]  # noqa: SLF001
    assert "pull" in keys and "push" in keys, \
        f"pull and push share one cache entry: {sorted(registry_client._TOKENS)}"  # noqa: SLF001
