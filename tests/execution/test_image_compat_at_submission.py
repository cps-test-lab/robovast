# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The image's protocol is checked by the submitter, before any pod exists.

It used to be checked by an initContainer running INSIDE the image, reading a file the image
carried. That is a workload inspecting its own image, which is not how admission is decided
anywhere else here, and it could only report by failing one init container per job in the batch.

The host-side check is strictly better on the initContainer's own terms: the refs are pinned to
digests immediately before it runs, so it binds to the exact bytes the pods would run -- and a
refusal costs no pods at all.

There was no test for the cluster compat path before. This change makes that path load-bearing.
"""

import types

import pytest

from robovast.common.execution import COMPAT_VERSION, COMPAT_VERSION_LABEL, MIN_IMAGE_COMPAT
from robovast.execution.backends import CampaignConfigError
from robovast.execution.cluster_execution.kubernetes_backend import BatchJobRunner

_TAG = "repo.example.com/robovast:latest"
_DIGEST = "repo.example.com/robovast@sha256:" + "ab" * 32


def _cluster_config():
    return types.SimpleNamespace(
        get_s3_endpoint=lambda: "http://s3.example.com",
        get_s3_credentials=lambda: ("ak", "sk"),
        get_host_aliases=lambda: [],
        get_registry_config=lambda: types.SimpleNamespace(
            pull_secret_name="", push_secret_name="", insecure=False, ca_configmap_name=""),
    )


@pytest.fixture
def _runner(monkeypatch):
    """Build a runner the real way, with the registry answering *labels*."""
    def build(labels, *, calls=None, label_cache=None):
        monkeypatch.setattr(
            "robovast.execution.cluster_execution.registry_client.manifest_digest",
            lambda ref, **kw: _DIGEST)
        def _labels(ref, **kw):
            if calls is not None:
                calls.append(ref)
            return labels
        monkeypatch.setattr(
            "robovast.execution.cluster_execution.registry_client.manifest_labels", _labels)
        monkeypatch.setattr(BatchJobRunner, "_discover_gpu_support",
                            lambda self: (setattr(self, "_gpu_capacity", 0),
                                          setattr(self, "_gpu_runtime_class", None)))
        return BatchJobRunner.for_batch(
            campaign_data={"configs": [{"name": "cfgA"}], "execution": {}},
            campaign_id="camp-2026-08-24-000000", batch_tag="batch-0", runs=1,
            cluster_config=_cluster_config(), namespace="ns", image=_TAG,
            image_label_cache=label_cache)
    return build


def test_an_image_within_the_window_is_admitted(_runner):
    runner = _runner({COMPAT_VERSION_LABEL: str(COMPAT_VERSION)})
    assert runner.image == _DIGEST, "the check runs after pinning, on the bytes that will run"


def test_an_image_the_host_cannot_drive_is_refused_before_any_pod(_runner):
    with pytest.raises(CampaignConfigError) as e:
        _runner({COMPAT_VERSION_LABEL: str(COMPAT_VERSION + 1)})
    assert "NEWER than this robovast" in str(e.value)
    assert "before any pod was created" in str(e.value)


def test_an_image_below_the_floor_is_refused(_runner):
    if MIN_IMAGE_COMPAT <= 1:
        pytest.skip("no protocol below the floor to test with")
    with pytest.raises(CampaignConfigError) as e:
        _runner({COMPAT_VERSION_LABEL: str(MIN_IMAGE_COMPAT - 1)})
    assert "no longer supports" in str(e.value)


def test_an_image_carrying_no_label_is_refused_and_told_to_rebuild(_runner):
    """The deliberate difference from pinning, which shrugs.

    Pinning is an optimisation: not applying it leaves what would have run anyway. A compat
    check that could not establish the version has established *nothing*, and proceeding is how
    an incompatible image becomes a campaign that fails obscurely halfway through instead of a
    refusal at submission.

    ``{}`` is the registry answering, with an image that carries no such label -- so rebuilding
    it IS the remedy, and the message says so.
    """
    with pytest.raises(CampaignConfigError) as e:
        _runner({})
    assert "cannot determine the container protocol version" in str(e.value)
    assert "reports no" in str(e.value), "must say the registry answered"
    assert "rebuild it from the revision" in str(e.value)
    assert "ROBOVAST_SKIP_IMAGE_COMPAT_CHECK" in str(e.value)


def test_a_registry_that_could_not_be_asked_does_not_blame_the_image(_runner):
    """``None`` is "never asked", and the two must not share a message.

    This is the failure that motivated splitting them. A deployment whose pull credential could
    not read the registry was told its image carried no compat label and to rebuild it -- so it
    was rebuilt, correctly, twice, while the message never mentioned the credential that was
    actually at fault. An image that was never read is not evidence about the image.
    """
    with pytest.raises(CampaignConfigError) as e:
        _runner(None)
    assert "cannot determine the container protocol version" in str(e.value)
    assert "never read" in str(e.value)
    assert "rebuilding it will not change this" in str(e.value)
    assert "registry credential" in str(e.value)
    # The remedy for the OTHER case must not appear here: it is the wrong advice, and being
    # given it is what cost the incident its two rebuilds.
    assert "rebuild it from the revision" not in str(e.value)


def test_the_escape_hatch_lets_an_unreadable_registry_through(_runner, monkeypatch):
    """For the case no reading can settle: a registry that is briefly unreachable.

    A guarantee that halts every campaign when an optional component hiccups is not one anybody
    keeps, so the way past it is documented rather than discovered. Asserted for ``None``, the
    state it is actually for -- the message for that state now names this switch as the way on
    once the operator knows the image.
    """
    monkeypatch.setenv("ROBOVAST_SKIP_IMAGE_COMPAT_CHECK", "1")
    assert _runner(None).image == _DIGEST
    assert _runner({}).image == _DIGEST


def test_the_registry_is_asked_once_per_campaign_not_once_per_batch(_runner):
    """A sweep is many batches over one image; the labels of a ref cannot change between them.

    The cache is the backend's, handed to each batch's runner, so this builds two the way a
    sweep does rather than asserting something about one.
    """
    calls: list = []
    shared: dict = {}
    labels = {COMPAT_VERSION_LABEL: str(COMPAT_VERSION)}
    _runner(labels, calls=calls, label_cache=shared)
    _runner(labels, calls=calls, label_cache=shared)
    assert len(calls) == 1, f"asked the registry once per batch: {calls}"
