# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A digest-pinned ref is read from the registry, not mistaken for a tagged one.

Every ref this lane asks the registry about has just been pinned to a digest -- that is what
makes a probe bind to the bytes the pods will run rather than to whatever a tag points at
next. So ``repo@sha256:...`` is the *normal* input here, not an exotic one, and a parser that
only understands ``repo:tag`` splits it on the colon inside the digest: the request then goes
to a repository name ending in ``@sha256``, which no registry has.

That failure is silent in the worst way. A registry answers an unknown repository with 401 and
an empty ``WWW-Authenticate``, so the probe reports "these credentials were refused" rather
than "that path is wrong", and a caller which fails closed on an unreadable image refuses the
work while naming the image. The registry stub below answers exactly that way, so the tests
fail the way the real thing did rather than on a synthetic 404.
"""

import pytest

from robovast.execution.cluster_execution.registry_client import (PRESENT, manifest_labels,
                                                                  manifest_state,
                                                                  split_image_ref)

_REPO = "reg.local:5000/rv/sim"
_DIGEST = "sha256:" + "ab" * 32
_CONFIG_DIGEST = "sha256:" + "cd" * 32
_LABELS = {"org.robovast.compat-version": "2"}


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _Registry:
    """A v2 registry holding exactly one image, addressable by tag or by digest."""

    def __init__(self):
        self.paths = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _answer(self, url):
        self.paths.append(url)
        path = url.split("/v2/", 1)[1]
        if path in (f"rv/sim/manifests/{_DIGEST}", "rv/sim/manifests/v1"):
            return _Resp(200, {"config": {"digest": _CONFIG_DIGEST}},
                         {"Docker-Content-Digest": _DIGEST})
        if path == f"rv/sim/blobs/{_CONFIG_DIGEST}":
            return _Resp(200, {"config": {"Labels": dict(_LABELS)}})
        # What a registry says about a repository it does not have: a challenge no
        # credential can satisfy, rather than a 404 that would name the real problem.
        return _Resp(401, headers={"WWW-Authenticate": ""})

    def head(self, url, **_kw):
        return self._answer(url)

    def get(self, url, **_kw):
        return self._answer(url)


@pytest.fixture
def registry(monkeypatch):
    import requests
    stub = _Registry()
    monkeypatch.setattr(requests, "Session", lambda: stub)
    return stub


@pytest.mark.parametrize("ref,expected", [
    (f"{_REPO}@{_DIGEST}", ("reg.local:5000", "rv/sim", _DIGEST)),
    # The digest wins over a tag some tools carry alongside it; a reference is one thing,
    # and the digest is the half that names bytes.
    (f"{_REPO}:v1@{_DIGEST}", ("reg.local:5000", "rv/sim", _DIGEST)),
])
def test_a_digest_is_the_reference_not_a_tag(ref, expected):
    assert split_image_ref(ref) == expected


def test_labels_of_a_digest_pinned_image_are_read(registry):
    assert manifest_labels(f"{_REPO}@{_DIGEST}") == _LABELS
    assert f"/v2/rv/sim/manifests/{_DIGEST}" in registry.paths[0]


def test_labels_of_a_tagged_image_are_still_read(registry):
    assert manifest_labels(f"{_REPO}:v1") == _LABELS


def test_a_digest_pinned_manifest_is_present(registry):
    assert manifest_state(f"{_REPO}@{_DIGEST}") == PRESENT


def test_an_image_the_registry_does_not_have_still_reads_as_unreadable(registry):
    """The guard on the fix: a genuinely unaskable ref must keep reporting ``None``."""
    assert manifest_labels(f"reg.local:5000/rv/absent@{_DIGEST}") is None
