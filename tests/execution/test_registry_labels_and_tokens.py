# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reading an image's labels, and not re-authenticating once per request to do it.

Two findings from one incident. A deployment whose credential could not read the registry was
told its image carried no protocol label and to rebuild it — advice that cannot work, because
the image was never read. And the walk that read it re-ran the whole auth challenge for every
single HTTP request, which is what a token endpoint is entitled to rate-limit.
"""

import types

import pytest

from robovast.execution.cluster_execution import registry_client
from robovast.execution.cluster_execution.registry_client import manifest_created, manifest_labels

_REF = "repo.example.com/robovast:latest"
_CONFIG = "sha256:" + "ab" * 32
_LABELS = {"org.robovast.compat-version": "2"}


def _resp(payload, status=200):
    def _json():
        if payload is None:
            raise ValueError("not JSON")
        return payload
    return types.SimpleNamespace(status_code=status, json=_json)


def _registry(monkeypatch, responses):
    def _request(host, path, **kwargs):
        for key, value in responses.items():
            if path.endswith(key):
                return value
        return _resp(None, status=404)
    monkeypatch.setattr(registry_client, "_registry_request", _request)


# -- three states, in two return values --------------------------------------


def test_labels_that_were_read_come_back_as_a_dict(monkeypatch):
    _registry(monkeypatch, {
        "manifests/latest": _resp({"config": {"digest": _CONFIG}}),
        f"blobs/{_CONFIG}": _resp({"config": {"Labels": dict(_LABELS)}}),
    })
    assert manifest_labels(_REF) == _LABELS


def test_an_image_with_no_labels_is_an_empty_dict_not_none(monkeypatch):
    """Asked, and it carries none. This is a fact about the image, and the only state a caller
    may answer with "rebuild it"."""
    _registry(monkeypatch, {
        "manifests/latest": _resp({"config": {"digest": _CONFIG}}),
        f"blobs/{_CONFIG}": _resp({"config": {}}),
    })
    assert manifest_labels(_REF) == {}


@pytest.mark.parametrize("responses, why", [
    ({}, "the manifest could not be fetched"),
    ({"manifests/latest": _resp({"config": {"digest": _CONFIG}})}, "the config blob 404s"),
    ({"manifests/latest": _resp({"config": {"digest": _CONFIG}}),
      f"blobs/{_CONFIG}": _resp(None)}, "the config is not JSON"),
    ({"manifests/latest": _resp({"config": {"digest": "not-a-digest"}})},
     "the manifest names no usable config"),
    ({"manifests/latest": _resp({"manifests": []})}, "an index with no image child"),
])
def test_every_way_of_not_reading_is_none(monkeypatch, responses, why):
    """``None``, never ``{}``: an empty dict would be read as "this image carries no labels",
    which is a claim about the image that none of these establish."""
    _registry(monkeypatch, responses)
    assert manifest_labels(_REF) is None, why


def test_the_build_date_reader_still_collapses_both(monkeypatch):
    """`manifest_created` keeps returning `""` for either, deliberately: a date is an addition
    to what its caller already shows, not a verdict about the image."""
    _registry(monkeypatch, {})
    assert manifest_created(_REF) == ""
    _registry(monkeypatch, {
        "manifests/latest": _resp({"config": {"digest": _CONFIG}}),
        f"blobs/{_CONFIG}": _resp({"config": {}}),
    })
    assert manifest_created(_REF) == ""


# -- one challenge per repository, not per request ---------------------------


class _Timeout(Exception):
    """Stands in for ``requests.exceptions.Timeout``."""


class _Exceptions:
    """The subset of ``requests.exceptions`` the reader names."""

    Timeout = _Timeout
    ConnectionError = type("ConnectionError", (Exception,), {})
    ChunkedEncodingError = type("ChunkedEncodingError", (Exception,), {})


class _Session:
    """A registry that demands a Bearer token and counts how often it issues one."""

    def __init__(self, tokens, ttl=300):
        self.tokens = tokens          # shared counter across sessions
        self.ttl = ttl
        self.gets: list = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, headers=None, params=None, auth=None, timeout=None, verify=None):
        if "/token" in url:
            self.tokens.append(params)
            return _resp({"token": "tok", "expires_in": self.ttl})
        self.gets.append(headers or {})
        if (headers or {}).get("Authorization") == "Bearer tok":
            return _resp({"ok": True})
        return types.SimpleNamespace(
            status_code=401, json=lambda: {},
            headers={"WWW-Authenticate":
                     'Bearer realm="https://repo.example.com/token",service="r"'})

    head = get


@pytest.fixture
def _sessions(monkeypatch):
    """Real `_registry_request`, fake transport, empty token cache."""
    monkeypatch.setattr(registry_client, "_TOKENS", {})
    tokens: list = []
    made: list = []

    class _Requests:
        # Named as `requests.Session` is, because it stands in for it.
        @staticmethod
        def Session():  # pylint: disable=invalid-name
            s = _Session(tokens)
            made.append(s)
            return s

        # The reader asks which failures mean "did not answer", so a stand-in for the
        # module has to answer that too -- a fake missing it fails the code under test on
        # the fake rather than on its subject.
        exceptions = _Exceptions

    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)
    return tokens, made


def _cfg():
    import base64
    import json
    blob = json.dumps({"auths": {"repo.example.com": {
        "auth": base64.b64encode(b"u:p").decode()}}})
    return blob


def test_a_token_is_earned_once_and_reused(_sessions):
    tokens, _ = _sessions
    for path in ("robovast/manifests/latest", f"robovast/blobs/{_CONFIG}",
                 "robovast/manifests/sha256:beef"):
        registry_client._registry_request(
            "repo.example.com", path, method="GET", dockerconfigjson=_cfg())
    assert len(tokens) == 1, (
        f"re-authenticated per request: {len(tokens)} token requests. Reading an image's "
        f"labels is three requests, and pinning a campaign's refs is one per ref.")


def test_a_different_repository_earns_its_own_token(_sessions):
    """A registry token is scoped to one repository, so reusing it across repositories would
    only earn a 401. Keyed on the repository to avoid the pointless round trip."""
    tokens, _ = _sessions
    registry_client._registry_request(
        "repo.example.com", "robovast/manifests/latest", method="GET",
        dockerconfigjson=_cfg())
    registry_client._registry_request(
        "repo.example.com", "other/manifests/latest", method="GET",
        dockerconfigjson=_cfg())
    assert len(tokens) == 2


def test_a_credential_is_never_the_cache_key_itself(_sessions):
    """One controller drives campaigns whose namespaces hold different pull secrets: a token
    fetched with one must not be handed to another. The key carries a digest, so a password
    never sits in a process-lifetime dict that nothing redacts."""
    import base64
    import json
    tokens, _ = _sessions
    other = json.dumps({"auths": {"repo.example.com": {
        "auth": base64.b64encode(b"u:different").decode()}}})
    registry_client._registry_request(
        "repo.example.com", "robovast/manifests/latest", method="GET",
        dockerconfigjson=_cfg())
    registry_client._registry_request(
        "repo.example.com", "robovast/manifests/latest", method="GET",
        dockerconfigjson=other)
    assert len(tokens) == 2, "a second credential reused the first one's token"
    keys = str(sorted(registry_client._TOKENS))
    assert "different" not in keys, "a password is sitting in the cache key"
    assert "u:" not in keys, "a credential is sitting in the cache key"


def test_a_refused_token_is_dropped_rather_than_failing(_sessions, monkeypatch):
    """A cached token can be revoked, or expire inside the safety margin. Presenting a stale
    one must cost a retry, never the request: a cache is not allowed to be worse than none."""
    tokens, _ = _sessions
    registry_client._store_token(
        registry_client._token_key("repo.example.com", "robovast/manifests/latest", ("u", "p")),
        "stale", 300)
    resp = registry_client._registry_request(
        "repo.example.com", "robovast/manifests/latest", method="GET",
        dockerconfigjson=_cfg())
    assert resp.status_code == 200
    assert len(tokens) == 1, "did not re-earn a token after the stale one was refused"


def test_an_expired_token_is_not_presented(_sessions):
    tokens, _ = _sessions
    key = registry_client._token_key(
        "repo.example.com", "robovast/manifests/latest", ("u", "p"))
    # A lifetime under the safety margin is already past by the time it is stored.
    registry_client._store_token(key, "tok", 0)
    assert registry_client._cached_token(key) is None
    registry_client._registry_request(
        "repo.example.com", "robovast/manifests/latest", method="GET",
        dockerconfigjson=_cfg())
    assert len(tokens) == 1


class _FlakySession(_Session):
    """Answers with a transport failure *fail_times* times, then normally."""

    def __init__(self, tokens, budget):
        super().__init__(tokens)
        self.budget = budget

    def get(self, url, headers=None, params=None, auth=None, timeout=None, verify=None):
        if "/token" not in url and self.budget:
            self.budget[0] -= 1
            raise _Timeout("handshake timed out")
        return super().get(url, headers=headers, params=params, auth=auth,
                           timeout=timeout, verify=verify)

    head = get


@pytest.fixture
def _flaky(monkeypatch):
    """Real `_registry_request` against a transport that fails a set number of times.

    The backoff is shortened rather than zeroed: the give-up rule compares elapsed time
    against the budget, so a zero wait spins instead of expiring and the test would measure
    nothing.
    """
    monkeypatch.setattr(registry_client, "_TOKENS", {})
    monkeypatch.setattr(registry_client, "_RETRY_BACKOFF", (0.01,))
    budget = [0]
    made: list = []

    class _Requests:
        @staticmethod
        def Session():  # pylint: disable=invalid-name
            s = _FlakySession([], budget if budget[0] else None)
            made.append(s)
            return s

        exceptions = _Exceptions

    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)
    return budget, made


def test_a_registry_that_did_not_answer_is_asked_again(_flaky):
    """This read gates a campaign launch: the compatibility check refuses a campaign it
    cannot read the image for, so one TLS handshake timeout refused a campaign on evidence
    about the network rather than about the image. It happens exactly when it matters, too
    -- a service upgrade prewarms the family images on every node, and the minutes after
    one are when a small control request is likeliest to be starved of egress.
    """
    budget, made = _flaky
    budget[0] = 1

    resp = registry_client._registry_request("repo.example.com", "x/manifests/latest",
                                             method="GET", dockerconfigjson=_cfg(),
                                             patience_s=1.0)

    assert resp is not None and resp.status_code == 200
    assert len(made) == 2, "the failed attempt and the one that answered"


def test_asking_stops_when_the_budget_is_spent(_flaky):
    """Bounded by elapsed time rather than by a number of goes: each attempt can itself
    burn the request timeout, so a count bounds the tries and not the wait."""
    budget, made = _flaky
    budget[0] = 999

    resp = registry_client._registry_request("repo.example.com", "x/manifests/latest",
                                             method="GET", dockerconfigjson=_cfg(),
                                             patience_s=0.05)

    assert resp is None
    assert 1 < len(made) < 999, "it kept asking, and it stopped"


def test_an_answer_is_never_retried(_sessions):
    """A status code -- any status code -- is an answer. Repeating a 401 or a 404 spends
    time to be told the same thing, and would multiply every real refusal by the attempt
    count."""
    _tokens, made = _sessions

    registry_client._registry_request("repo.example.com", "x/manifests/latest",
                                      method="GET", dockerconfigjson=_cfg())

    assert len(made) == 1


def test_only_the_launch_check_gets_the_patient_budget():
    """The same helper backs the image-state probes behind build decisions and status
    reads, where a path that stalls for minutes to answer "is this image published" is
    worse served than one that says it does not know. Only the compatibility check has the
    other trade: unread means the campaign is refused, and a campaign runs for minutes to
    days -- so its budget is sized against the outage (pulls lasting minutes), not against
    a person's patience.
    """
    assert registry_client.DEFAULT_PATIENCE <= 5
    assert registry_client.LAUNCH_PATIENCE >= 120
    # Widening, so a registry that is rate-limiting is owed a growing gap rather than a drum.
    assert list(registry_client._RETRY_BACKOFF) == sorted(registry_client._RETRY_BACKOFF)
    assert registry_client._RETRY_BACKOFF[0] < registry_client._RETRY_BACKOFF[-1]
