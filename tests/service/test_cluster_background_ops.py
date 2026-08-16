# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The cluster lane's re-triggerable post-run operations dispatch at all.

``run_postprocessing`` / ``run_share`` are the two operations a caller reaches for
*after* a campaign has finished — exactly when the runs are already paid for and a
crash is most expensive. Both referenced a ``Phase`` that the module never imported,
so every call raised ``NameError`` before dispatching anything and surfaced as a bare
HTTP 500. Nothing covered them, so the name error survived; ruff had been reporting it
as F821 the whole time.

These tests stub the dispatcher, so the work closure never runs: no cluster, no object
store. They assert only that the call resolves its names and hands the right phase to
the dispatcher — which is all that was broken, and all that a unit test can honestly
claim here.
"""

from unittest.mock import MagicMock

import pytest

from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import (ActionResult, RunPostprocessingRequest,
                                        RunShareRequest)


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


def test_admission_check_uses_the_given_context(monkeypatch):
    """The Kueue admission check must dial the context the Jobs were submitted with.

    It used to be called with no context at all, so it dialled the ambient kubeconfig
    context while the campaign's Jobs had gone to the service's ``--context`` cluster.
    With the two pointing at different clusters, postprocessing failed against a cluster
    the campaign never used — and said so in a self-contradictory way, naming the
    configured API server as unreachable while the timeout it quoted was to a different
    address entirely.

    The stub raises ``ClusterUnreachableError``, a case this path already handles by
    returning a reason, so the test stops right where the context has been consumed and
    needs no cluster.
    """
    from robovast.common.errors import ClusterUnreachableError
    from robovast.execution.cluster_execution import (in_pod_storage,
                                                      kubernetes_kueue,
                                                      postprocess_job)

    seen = {}

    def _check(**kwargs):
        seen.update(kwargs)
        raise ClusterUnreachableError("api server unreachable")

    monkeypatch.setattr(kubernetes_kueue, "verify_kueue_admission_ready", _check)
    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("bucket", "prefix"))
    cfg = MagicMock()
    cfg.get_s3_credentials.return_value = ("key", "secret")
    cfg.get_s3_endpoint.return_value = "http://localhost:9000"

    ok, message = postprocess_job.run_conversion_job(
        cfg, "camp-1", "ns", "img", ["echo"], kube_context="local")

    assert seen.get("kube_context") == "local", (
        f"admission check did not receive the caller's context: {seen}")
    assert not ok and "cannot be scheduled" in message


def test_postprocess_campaign_forwards_the_context(monkeypatch):
    """``postprocess_campaign`` hands its context to the Job it schedules."""
    from robovast.execution.cluster_execution import postprocess_job

    seen = {}

    class _Stop(Exception):
        pass

    def _conversion(*args, **kwargs):
        seen.update(kwargs)
        raise _Stop  # stop before the object-store sync, which needs a cluster

    monkeypatch.setattr(postprocess_job, "run_conversion_job", _conversion)
    monkeypatch.setattr(postprocess_job, "rosbag_commands_for", lambda *a, **k: ["echo"])
    monkeypatch.setattr(postprocess_job, "campaign_vast", lambda root: "vast")
    monkeypatch.setattr(postprocess_job, "campaign_execution_image", lambda root: "img")

    with pytest.raises(_Stop):
        postprocess_job.postprocess_campaign(
            MagicMock(), "camp-1", "/nonexistent", "ns", kube_context="local")
    assert seen.get("kube_context") == "local"
