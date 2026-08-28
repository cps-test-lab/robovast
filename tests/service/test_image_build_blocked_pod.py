# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A build whose *pod* cannot start must fail, and must say what to fix.

Kubernetes leaves such a Job ``active`` forever -- an unpullable image keeps the pod
``Pending``, and with ``backoffLimit: 0`` and no ``activeDeadlineSeconds`` neither the
``succeeded`` nor the ``failed`` counter ever moves. A status read calling that
"building" leaves ``vast image wait`` polling until someone kills it, and the agent that
backgrounded it is never told anything at all.

Hence the two things asserted here, in this order of importance: the wait *ends*, and the
error *names the knob*. A test that only checked ``done`` would let the second half -- the
actual complaint -- regress silently, since "the image build failed; see the log tail" is
both useless and passing.
"""

import datetime
import tempfile
import types

import pytest

from robovast.execution.cluster_execution.cluster_execution import BLOCKED_GRACE_SECONDS
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.interface import ImageBuildStatus
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

BUILD = "imgbuild-sut-abc123"
SIDECAR_ERROR = ('Failed to pull image "harbor.example.org/robovast/robovast-sidecar:latest"'
                 ": pull access denied, repository does not exist or may require "
                 "authorization: authorization failed: no basic auth credentials")


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    return ClusterService(namespace="ns1", cluster_config_name="rke2",
                          cluster_config_kwargs={}, store=store, reap_on_start=False)


def _waiting(name, reason, message=""):
    return types.SimpleNamespace(
        name=name,
        state=types.SimpleNamespace(
            waiting=types.SimpleNamespace(reason=reason, message=message),
            terminated=None),
        last_state=None, restart_count=0)


def _pod(*, init=(), main=(), conditions=(), age_s=0.0):
    started = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(seconds=age_s))
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=f"{BUILD}-xyz", creation_timestamp=started,
                                       labels={"build-id": BUILD}),
        status=types.SimpleNamespace(
            phase="Pending", start_time=started, reason=None, message=None,
            init_container_statuses=list(init), container_statuses=list(main),
            conditions=list(conditions)))


def _wire(cs, monkeypatch, pod, *, job_phase="running", log=""):
    """Point the service at one fake pod, with the Job still active."""
    core = types.SimpleNamespace(
        list_namespaced_pod=lambda namespace, label_selector: types.SimpleNamespace(
            items=[pod] if pod is not None else []),
        read_namespaced_pod_log=lambda **kw: log)
    monkeypatch.setattr(cs, "_k8s", lambda: core)
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: job_phase)
    monkeypatch.setattr(cs, "_retire_build_context", lambda bid: None)
    return core


def _record(cs, *, phase="building"):
    status = ImageBuildStatus(build_id=BUILD, tag="sut", phase=phase, done=False)
    cs._image_build_state()[BUILD] = {
        "tag": "sut", "image_ref": "reg/sut:h", "hash": "h", "status": status}
    return status


# ---------------------------------------------------------------------------
# the wait ends
# ---------------------------------------------------------------------------

def test_a_fresh_image_pull_failure_is_blocked_not_yet_failed(cs, monkeypatch):
    """The grace window: a registry blip may clear, so it is reported, not acted on."""
    _wire(cs, monkeypatch, _pod(init=[_waiting("context-fetch", "ImagePullBackOff",
                                               SIDECAR_ERROR)], age_s=5.0))
    _record(cs)

    status = cs.get_image_build_status(BUILD)
    assert status.phase == "blocked"
    assert status.done is False


def test_an_image_pull_failure_past_the_grace_window_fails_the_build(cs, monkeypatch):
    """Otherwise this stays 'building' forever."""
    _wire(cs, monkeypatch, _pod(init=[_waiting("context-fetch", "ImagePullBackOff",
                                               SIDECAR_ERROR)],
                                age_s=BLOCKED_GRACE_SECONDS + 1))
    _record(cs)

    status = cs.get_image_build_status(BUILD)
    assert status.done is True
    assert status.phase == "failed"


def test_a_blip_that_clears_returns_to_building(cs, monkeypatch):
    """What the grace window is *for* -- so it must actually be reversible."""
    _wire(cs, monkeypatch, _pod(age_s=5.0))
    _record(cs, phase="blocked")

    status = cs.get_image_build_status(BUILD)
    assert status.phase == "building"
    assert status.error is None
    assert status.done is False


def test_a_build_from_a_previous_service_instance_fails_too(cs, monkeypatch):
    """A restarted service has no record to hang a timer on, which is why the verdict
    comes from the pod's own age rather than from bookkeeping this process kept."""
    _wire(cs, monkeypatch, _pod(init=[_waiting("context-fetch", "ErrImagePull",
                                               SIDECAR_ERROR)],
                                age_s=BLOCKED_GRACE_SECONDS + 1))

    status = cs.get_image_build_status(BUILD)
    assert status.done is True
    assert status.phase == "failed"
    assert "robovast-sidecar" in status.error.message


def test_a_pod_read_that_fails_is_not_a_verdict(cs, monkeypatch):
    """'Could not ask' must never be recorded as 'not blocked' -- nor as a failure."""
    def boom(namespace, label_selector):
        raise RuntimeError("apiserver unreachable")
    monkeypatch.setattr(cs, "_k8s", lambda: types.SimpleNamespace(
        list_namespaced_pod=boom, read_namespaced_pod_log=lambda **kw: ""))
    monkeypatch.setattr(cs, "_existing_build_job", lambda bid: "running")
    _record(cs)

    status = cs.get_image_build_status(BUILD)
    assert status.phase == "building"
    assert status.done is False


def test_a_healthy_pending_pod_is_left_alone(cs, monkeypatch):
    """Pulling a large base image is `ContainerCreating`, not a blocked reason -- deriving
    the window from pod age must not turn a slow legitimate pull into a failure."""
    _wire(cs, monkeypatch,
          _pod(main=[_waiting("buildkit", "ContainerCreating")],
               age_s=BLOCKED_GRACE_SECONDS * 10))
    _record(cs)

    status = cs.get_image_build_status(BUILD)
    assert status.phase == "building"
    assert status.done is False


# ---------------------------------------------------------------------------
# the error names the knob
# ---------------------------------------------------------------------------

def test_the_failure_points_at_the_infrastructure_never_at_the_build_section(cs, monkeypatch):
    _wire(cs, monkeypatch, _pod(init=[_waiting("context-fetch", "ImagePullBackOff",
                                               SIDECAR_ERROR)],
                                age_s=BLOCKED_GRACE_SECONDS + 1))
    _record(cs)

    err = cs.get_image_build_status(BUILD).error
    assert err.phase == "builder-pod"
    # The whole point: editing build: cannot fix a pod that never started, and the previous
    # behaviour (classify_build_error on an empty log) said exactly that it could.
    assert err.fixable_by == "infra"
    assert "no basic auth credentials" in err.message      # Kubernetes' own words
    assert "robovast-sidecar" in err.message               # which image
    assert "pull Secret" in err.message                    # which knob


def test_the_blocked_phase_already_carries_the_reason(cs, monkeypatch):
    """A status saying only "blocked" repeats the complaint: an agent with no idea why."""
    _wire(cs, monkeypatch, _pod(init=[_waiting("context-fetch", "ImagePullBackOff",
                                               SIDECAR_ERROR)], age_s=1.0))
    _record(cs)

    err = cs.get_image_build_status(BUILD).error
    assert err is not None
    assert "no basic auth credentials" in err.message
    assert "cannot start yet" in err.message


def test_a_buildkit_pull_failure_names_the_public_registry_not_the_sidecar(cs, monkeypatch):
    """Same Kubernetes reason, different fix -- so the two must not share one message."""
    _wire(cs, monkeypatch, _pod(main=[_waiting("buildkit", "ImagePullBackOff",
                                               "moby/buildkit:rootless: i/o timeout")],
                                age_s=BLOCKED_GRACE_SECONDS + 1))
    _record(cs)

    err = cs.get_image_build_status(BUILD).error
    assert "BuildKit" in err.message
    assert "robovast-sidecar" not in err.message


def test_an_unschedulable_pod_reports_capacity_not_an_image(cs, monkeypatch):
    _wire(cs, monkeypatch, _pod(
        conditions=[types.SimpleNamespace(
            type="PodScheduled", status="False", reason="Unschedulable",
            message="0/3 nodes are available: 3 Insufficient cpu")],
        age_s=BLOCKED_GRACE_SECONDS + 1))
    _record(cs)

    err = cs.get_image_build_status(BUILD).error
    assert err.fixable_by == "infra"
    assert "Insufficient cpu" in err.message
    assert "capacity" in err.message
    assert "robovast-sidecar" not in err.message


def test_the_build_log_falls_back_to_the_reason_the_pod_could_not_start(cs, monkeypatch):
    """The failure advises reading the log; an empty log makes that advice a dead end."""
    _wire(cs, monkeypatch, _pod(init=[_waiting("context-fetch", "ImagePullBackOff",
                                               SIDECAR_ERROR)],
                                age_s=BLOCKED_GRACE_SECONDS + 1),
          log="")

    text = cs._build_log_text(BUILD)
    assert "ImagePullBackOff" in text
    assert "no basic auth credentials" in text


def test_a_pod_with_no_timestamp_is_not_granted_an_unmeasurable_grace(cs, monkeypatch):
    """The window is the pod's age, so a pod with no age cannot be waited out -- doing so
    would restore the indefinite wait this whole change removes. The block was observed
    either way."""
    pod = _pod(init=[_waiting("context-fetch", "ImagePullBackOff", SIDECAR_ERROR)])
    pod.status.start_time = None
    pod.metadata.creation_timestamp = None
    _wire(cs, monkeypatch, pod)
    _record(cs)

    status = cs.get_image_build_status(BUILD)
    assert status.done is True
    assert status.phase == "failed"
