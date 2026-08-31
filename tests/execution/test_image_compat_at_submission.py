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


def test_a_registry_that_will_not_say_refuses_rather_than_shrugging(_runner):
    """The deliberate difference from pinning, which shrugs.

    Pinning is an optimisation: not applying it leaves what would have run anyway. A compat
    check that could not read the image has established *nothing*, and proceeding is how an
    incompatible image becomes a campaign that fails obscurely halfway through instead of a
    refusal at submission.
    """
    with pytest.raises(CampaignConfigError) as e:
        _runner({})
    assert "cannot determine the container protocol version" in str(e.value)
    assert "ROBOVAST_SKIP_IMAGE_COMPAT_CHECK" in str(e.value)


def test_the_escape_hatch_lets_an_unreadable_registry_through(_runner, monkeypatch):
    """For the case the check cannot distinguish: a registry that is briefly unreachable.

    A guarantee that halts every campaign when an optional component hiccups is not one anybody
    keeps, so the way past it is documented rather than discovered.
    """
    monkeypatch.setenv("ROBOVAST_SKIP_IMAGE_COMPAT_CHECK", "1")
    runner = _runner({})
    assert runner.image == _DIGEST


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
