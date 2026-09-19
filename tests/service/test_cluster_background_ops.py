# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster lane's re-triggerable post-run operations dispatch at all.

``run_postprocessing`` / ``run_share`` are the two operations a caller reaches for
*after* a campaign has finished — exactly when the runs are already paid for and a
crash is most expensive. Both referenced a ``Phase`` that the module never imported,
so every call raised ``NameError`` before dispatching anything and surfaced as a bare
HTTP 500. Nothing covered them, so the name error survived; ruff had been reporting it
as F821 the whole time.

These tests stub the dispatcher, so the work closure never runs and no cluster is
needed. They assert only that the call resolves its names and hands the right phase to
the dispatcher — which is all that was broken, and all that a unit test can honestly
claim here.
"""

from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import ActionResult, RunPostprocessingRequest, RunShareRequest


@pytest.fixture
def svc():
    return ClusterService(namespace="ns", cluster_config_name="x",
                          cluster_config_kwargs={}, reap_on_start=False)


def _capture(svc, monkeypatch):
    seen = {}

    def _dispatch(campaign_id, *, phase, work):
        seen["campaign_id"] = campaign_id
        seen["phase"] = phase
        seen["work"] = work
        return ActionResult(ok=True, message="dispatched")

    monkeypatch.setattr(svc, "_dispatch_background", _dispatch)
    return seen


def test_run_postprocessing_dispatches_in_the_postprocessing_phase(svc, monkeypatch):
    seen = _capture(svc, monkeypatch)
    result = svc.run_postprocessing(RunPostprocessingRequest(campaign_id="camp-1"))
    assert result.ok
    assert seen["campaign_id"] == "camp-1"
    assert seen["phase"] == "postprocessing"
    assert callable(seen["work"])


def test_run_share_dispatches_in_the_sharing_phase(svc, monkeypatch):
    seen = _capture(svc, monkeypatch)
    result = svc.run_share(RunShareRequest(campaign_id="camp-1"))
    assert result.ok
    assert seen["campaign_id"] == "camp-1"
    assert seen["phase"] == "sharing"
    assert callable(seen["work"])


def test_postprocess_job_loads_the_given_context(monkeypatch):
    """Postprocessing must dial the context the campaign's Jobs were submitted with.

    The Kubernetes clients this path builds read whatever context is loaded at the moment
    they are constructed, so loading it is the whole of the requirement. Without it the path
    dials the ambient kubeconfig while the campaign's Jobs have gone to the service's
    ``--context`` cluster. Postprocessing then fails against
    a cluster the campaign never used, and said so self-contradictorily: naming the
    configured API server as unreachable while quoting a timeout to a different address.

    So this pins the load itself, rather than any probe that happens to imply it. The stub
    raises after recording,
    which stops the test where the context has been consumed and needs no cluster.
    """
    from robovast.execution.cluster_execution import kube_client, postprocess_job

    seen = {}

    class _Stop(Exception):
        pass

    def _load(context=None, **kwargs):
        seen["context"] = context
        raise _Stop

    monkeypatch.setattr(kube_client, "load_kube_config", _load)
    # The steps are rendered from the campaign's own `.vast`, which this test has none of.
    monkeypatch.setattr(postprocess_job, "image_steps_for", lambda *a, **k: [])

    with pytest.raises(_Stop):
        postprocess_job.run_conversion_job(
            MagicMock(), "camp-1", "/results/camp-1", "ns", "img", ["echo"],
            token="campaign:camp-1.0123abcd", kube_context="local")

    assert seen.get("context") == "local", (
        f"postprocessing did not load the caller's context: {seen}")


def test_postprocess_campaign_forwards_the_context(monkeypatch):
    """``postprocess_campaign`` hands its context to the Job it schedules."""
    from robovast.execution.cluster_execution import postprocess_job

    seen = {}

    class _Stop(Exception):
        pass

    def _conversion(*args, **kwargs):
        seen.update(kwargs)
        raise _Stop  # stop before the wait, which needs a cluster

    monkeypatch.setattr(postprocess_job, "run_conversion_job", _conversion)
    # The facts the manifest needs about the campaign are read from its directory through
    # `_read_submit_inputs`. Patched at that seam, so this test needs no campaign tree.
    monkeypatch.setattr(postprocess_job, "_read_submit_inputs",
                        lambda root, skip=None, skip_rosout=False:
                        (["echo"], "img", (), None))

    with pytest.raises(_Stop):
        postprocess_job.postprocess_campaign(
            MagicMock(), "camp-1", "/nonexistent", "ns", token="campaign:camp-1.0123abcd",
            kube_context="local")
    assert seen.get("kube_context") == "local"
