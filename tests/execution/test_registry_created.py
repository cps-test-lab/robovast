# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How old is the image the registry is offering?

The admin panel showed two digests and left the reader to work out which was newer, which a
digest cannot tell them. ``manifest_created`` answers it by reading the published image's own
build stamp — two small GETs, no layers — and everything asserted here is a way that read can
come back wrong rather than empty. An empty answer is fine and means "the registry did not
say"; a *manufactured* one would be read as the age of what is published, and believed.
"""

import types

import pytest

from robovast.execution.cluster_execution import registry_client
from robovast.execution.cluster_execution.registry_client import manifest_created

_REF = "repo.example.com/robovast:latest"
_CONFIG_DIGEST = "sha256:" + "ab" * 32
_CHILD_DIGEST = "sha256:" + "cd" * 32


def _resp(payload, status=200):
    def _json():
        if payload is None:
            raise ValueError("not JSON")
        return payload
    return types.SimpleNamespace(status_code=status, json=_json)


def _registry(monkeypatch, responses, calls=None):
    """Answer each ``/v2/<path>`` from *responses*, keyed by the tail of the path."""
    def _request(host, path, **kwargs):
        if calls is not None:
            calls.append(path)
        for key, value in responses.items():
            if path.endswith(key):
                return value
        return _resp(None, status=404)
    monkeypatch.setattr(registry_client, "_registry_request", _request)


def test_the_oci_label_is_what_dates_the_image(monkeypatch):
    """The stamp robovast writes at build time, straight off the config blob."""
    _registry(monkeypatch, {
        "manifests/latest": _resp({"config": {"digest": _CONFIG_DIGEST}}),
        f"blobs/{_CONFIG_DIGEST}": _resp(
            {"created": "2020-01-01T00:00:00Z",
             "config": {"Labels": {
                 "org.opencontainers.image.created": "2026-08-30T09:15:00Z"}}}),
    })
    # The label wins over the config's own `created`: a reproducible build writes a fixed
    # date there, so trusting it would report an image as years old on the day it is pushed.
    assert manifest_created(_REF) == "2026-08-30T09:15:00Z"


def test_an_unlabelled_image_falls_back_to_the_config(monkeypatch):
    """Not everything in a registry was built by us; the builder's own date still reads."""
    _registry(monkeypatch, {
        "manifests/latest": _resp({"config": {"digest": _CONFIG_DIGEST}}),
        f"blobs/{_CONFIG_DIGEST}": _resp({"created": "2026-07-01T12:00:00Z"}),
    })
    assert manifest_created(_REF) == "2026-07-01T12:00:00Z"


def test_a_zeroed_date_is_no_date(monkeypatch):
    """A reproducible build's epoch-zero stamp is not an age anyone wants shown."""
    _registry(monkeypatch, {
        "manifests/latest": _resp({"config": {"digest": _CONFIG_DIGEST}}),
        f"blobs/{_CONFIG_DIGEST}": _resp({"created": "1970-01-01T00:00:00Z"}),
    })
    assert manifest_created(_REF) == ""


def test_a_multi_arch_index_is_followed_to_a_real_image(monkeypatch):
    """buildx pushes an index whose entries include an attestation with no image config."""
    calls = []
    _registry(monkeypatch, {
        "manifests/latest": _resp({"manifests": [
            {"digest": "sha256:" + "ef" * 32,
             "platform": {"architecture": "unknown", "os": "unknown"}},
            {"digest": _CHILD_DIGEST, "platform": {"architecture": "amd64", "os": "linux"}},
        ]}),
        f"manifests/{_CHILD_DIGEST}": _resp({"config": {"digest": _CONFIG_DIGEST}}),
        f"blobs/{_CONFIG_DIGEST}": _resp({"created": "2026-08-30T09:15:00Z"}),
    }, calls=calls)
    assert manifest_created(_REF) == "2026-08-30T09:15:00Z"
    # The attestation entry was skipped rather than followed: its config is not an image
    # config, so picking it would report a published image as carrying no build date.
    assert not any("ef" * 32 in call for call in calls)


@pytest.mark.parametrize("responses", [
    {},                                                              # nothing answers
    {"manifests/latest": _resp(None)},                               # manifest not JSON
    {"manifests/latest": _resp({"config": {"digest": "not-a-digest"}})},
    {"manifests/latest": _resp({"config": {"digest": _CONFIG_DIGEST}}),
     f"blobs/{_CONFIG_DIGEST}": _resp({}, status=500)},              # blob refused
    {"manifests/latest": _resp({"config": {"digest": _CONFIG_DIGEST}}),
     f"blobs/{_CONFIG_DIGEST}": _resp({})},                          # no stamp at all
])
def test_every_uncertainty_is_empty_and_never_raises(monkeypatch, responses):
    """The date is an addition to the digest, so it must never take the digest down."""
    _registry(monkeypatch, responses)
    assert manifest_created(_REF) == ""


def test_an_unparseable_ref_is_empty_too(monkeypatch):
    _registry(monkeypatch, {})
    assert manifest_created("robovast") == ""
