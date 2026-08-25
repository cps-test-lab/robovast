# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Pod-accurate classification shared by the service lister and the CLI monitor."""

import types
from unittest import mock

from robovast.execution.cluster_execution.cluster_execution import (blocked_job_reasons, job_phase,
                                                                    list_jobs_with_phase,
                                                                    pod_container_failures,
                                                                    pod_invalidating_restart,
                                                                    pod_restarted_containers,
                                                                    pod_termination_reason,
                                                                    restarted_job_reasons,
                                                                    running_scenario_job_names)


def _job(name, *, succeeded=0, active=0, failed=0, suspend=False):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels={}, annotations={}),
        spec=types.SimpleNamespace(suspend=suspend),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed))


def _pod(job_name, phase="Running", *, waiting=None, terminated=None, pod_reason=None,
         unschedulable=None, restarts=None, sidecar=True, limits=None):
    """A pod for *job_name*.

    ``waiting=(reason, message)`` puts its container in that ``waiting`` state (as
    ImagePullBackOff would), phase Pending. ``terminated=(container, reason)`` puts a
    container in that ``terminated`` state (as OOMKilled would), phase Failed.
    ``pod_reason=(reason, message)`` sets a pod-level termination reason (Evicted /
    DeadlineExceeded), phase Failed. ``unschedulable=(reason, message)`` sets the
    ``PodScheduled=False`` *condition* the scheduler writes, phase Pending -- note there
    are no container statuses at all in that state, because there is no node to create
    them on. ``restarts=(container, count, last_reason, exit_code)`` gives a container a
    non-zero ``restart_count`` plus the ``last_state.terminated`` it died with, leaving
    the phase Running: that combination is the point, a pod that still looks healthy.

    A restarted container is a NATIVE SIDECAR by default -- declared in ``initContainers``
    with ``restartPolicy: Always``, its status in ``init_container_statuses`` -- because
    that is the only shape a scenario pod can actually produce. The pod itself is
    ``restartPolicy: Never``, so its regular container and its one-shot ``s3-init`` are
    never restarted at all; ``sidecar=False`` builds that impossible-but-worth-refusing
    case. ``limits={"memory": "4Gi"}`` gives the restarted container declared limits; the
    default of none is what the campaigns in question actually ran.
    """
    container_statuses = None
    reason_attr = None
    message_attr = None
    conditions = None
    if waiting is not None:
        reason, message = waiting
        phase = "Pending"
        container_statuses = [types.SimpleNamespace(
            state=types.SimpleNamespace(
                waiting=types.SimpleNamespace(reason=reason, message=message)))]
    if terminated is not None:
        cname, treason = terminated
        phase = "Failed"
        container_statuses = [types.SimpleNamespace(
            name=cname,
            state=types.SimpleNamespace(
                waiting=None,
                terminated=types.SimpleNamespace(reason=treason)))]
    if pod_reason is not None:
        reason_attr, message_attr = pod_reason
        phase = "Failed"
    if unschedulable is not None:
        reason, message = unschedulable
        phase = "Pending"
        conditions = [types.SimpleNamespace(
            type="PodScheduled", status="False", reason=reason, message=message)]
    init_container_statuses = None
    extra_init = []
    if restarts is not None:
        cname, count, last_reason, exit_code = restarts
        status = types.SimpleNamespace(
            name=cname,
            image="an-image",
            image_id="an-image@sha256:abc",
            restart_count=count,
            state=types.SimpleNamespace(waiting=None, terminated=None),
            last_state=types.SimpleNamespace(
                terminated=types.SimpleNamespace(
                    reason=last_reason, exit_code=exit_code, signal=None, message=None,
                    started_at=None, finished_at=None)))
        extra_init = [_container(cname, restart_policy="Always" if sidecar else None,
                                 limits=limits)]
        if sidecar:
            init_container_statuses = [status]
        else:
            container_statuses = [status]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"{job_name}-pod", labels={"batch.kubernetes.io/job-name": job_name}),
        spec=types.SimpleNamespace(
            node_name="a-node",
            containers=[_container("robovast", limits={"cpu": "1.25"})],
            init_containers=[_container("s3-init")] + extra_init),
        status=types.SimpleNamespace(
            phase=phase, container_statuses=container_statuses,
            init_container_statuses=init_container_statuses,
            reason=reason_attr, message=message_attr, conditions=conditions))


def _container(name, *, restart_policy=None, limits=None):
    """One container spec, as ``pod_workload_containers`` reads it."""
    return types.SimpleNamespace(
        name=name, restart_policy=restart_policy,
        resources=types.SimpleNamespace(limits=limits or {}))


class _Batch:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_namespaced_job(self, namespace, label_selector):
        return types.SimpleNamespace(items=self._jobs)


class _Core:
    def __init__(self, pods):
        self._pods = pods

    def list_namespaced_pod(self, namespace, label_selector):
        return types.SimpleNamespace(items=self._pods)


def test_job_phase_without_pod_truth_treats_active_as_running():
    """No pod map supplied → fall back to the Job-level view."""
    assert job_phase(_job("j", active=1)) == "running"
    assert job_phase(_job("j", succeeded=1)) == "completed"
    assert job_phase(_job("j", failed=1)) == "failed"
    assert job_phase(_job("j")) == "pending"


def test_job_phase_active_pod_pending_is_pending():
    """An active Job whose pod is Pending must classify as pending, not running."""
    assert job_phase(_job("j", active=1), pod_phases={}) == "pending"
    assert job_phase(_job("j", active=1), pod_phases={"j": "Pending"}) == "pending"
    assert job_phase(_job("j", active=1), pod_phases={"j": "Running"}) == "running"


def test_job_phase_finished_pod_never_goes_back_to_pending():
    """The regression this file exists for: a Job's ``status.succeeded`` lags its pod's
    termination by however long the job controller takes to remove the pod finalizer.
    Reading "active, pod not Running" as pending sent every finishing job backwards —
    a finished run showed a ``pending`` chip in the campaign view. One pod per Job,
    never retried, so the pod's verdict is the Job's."""
    assert job_phase(_job("j", active=1), pod_phases={"j": "Succeeded"}) == "completed"
    assert job_phase(_job("j", active=1), pod_phases={"j": "Failed"}) == "failed"


def test_running_scenario_job_names_only_counts_running_pods():
    core = _Core([_pod("a", "Running"), _pod("b", "Pending"), _pod("c", "Running")])
    assert running_scenario_job_names(core, "ns", "sel") == {"a", "c"}


class _BoomCore:
    def list_namespaced_pod(self, namespace, label_selector):
        raise RuntimeError("boom")


def test_pod_refinement_raises_on_api_error():
    """A pod-list failure must propagate, not silently return empties (which read as
    'nothing blocked' and let a stuck batch hang). See _pod_signals."""
    import pytest
    with pytest.raises(RuntimeError, match="boom"):
        running_scenario_job_names(_BoomCore(), "ns", "sel")
    with pytest.raises(RuntimeError, match="boom"):
        blocked_job_reasons(_BoomCore(), "ns", "sel")


def test_list_jobs_with_phase_degrades_explicitly_on_pod_error():
    """The advisory listing tolerates a transient pod-list hiccup (Job-level view),
    without raising — the safety-critical escalation is handled elsewhere.

    The phase assertion is the point: an *empty* pod map is pod truth saying "this job
    has no pod", which reports every active job pending, so a single failed pod list
    painted a whole running batch as not-yet-started. The fallback must be Job-level."""
    class _Batch:
        def list_namespaced_job(self, namespace, label_selector):
            job = _job("a", active=1)
            return type("L", (), {"items": [job]})()

    # Does not raise; still lists the job (Job-level phase, no pod-derived detail).
    out = list_jobs_with_phase(_Batch(), _BoomCore(), "ns", "sel")
    assert [(j.metadata.name, p) for j, p, _d in out] == [("a", "running")]
    assert all(detail is None for _j, _p, detail in out)


def test_list_jobs_with_phase_uses_pod_truth():
    """The shared helper: an admitted-but-Pending Job reports pending."""
    jobs = [_job("running-job", active=1), _job("pending-job", active=1),
            _job("done-job", succeeded=1)]
    batch = _Batch(jobs)
    core = _Core([_pod("running-job", "Running"), _pod("pending-job", "Pending")])

    phases = dict((j.metadata.name, p)
                  for j, p, _ in list_jobs_with_phase(batch, core, "ns", "sel"))
    assert phases == {"running-job": "running", "pending-job": "pending",
                      "done-job": "completed"}


def test_list_jobs_with_phase_reports_finished_pod_of_still_active_job():
    """End to end over the seam the campaign view reads: the Job says active because
    the job controller has not caught up, but its pod is already Succeeded/Failed."""
    jobs = [_job("just-finished", active=1), _job("just-failed", active=1)]
    core = _Core([_pod("just-finished", "Succeeded"), _pod("just-failed", "Failed")])
    result = {j.metadata.name: p
              for j, p, _ in list_jobs_with_phase(_Batch(jobs), core, "ns", "sel")}
    assert result == {"just-finished": "completed", "just-failed": "failed"}


def test_blocked_job_reasons_reports_image_pull_failures():
    """A pod stuck pulling its image is reported with Kubernetes' reason + message."""
    core = _Core([
        _pod("good", "Running"),
        _pod("bad", waiting=("ImagePullBackOff",
                             'Back-off pulling image "ghcr.io/x/y:nope"')),
    ])
    reasons = blocked_job_reasons(core, "ns", "sel")
    assert reasons == {
        "bad": 'ImagePullBackOff: Back-off pulling image "ghcr.io/x/y:nope"'}


def test_list_jobs_with_phase_marks_stuck_job_blocked_with_detail():
    """A Job whose pod can't start is reported ``blocked`` (its own status, not
    ``failed`` or a forever-``pending``) with a detail carrying the Kubernetes
    message — the signal every consumer surfaces."""
    jobs = [_job("running-job", active=1), _job("stuck-job", active=1)]
    batch = _Batch(jobs)
    core = _Core([
        _pod("running-job", "Running"),
        _pod("stuck-job", waiting=("ErrImagePull", "not found")),
    ])
    result = {j.metadata.name: (p, d)
              for j, p, d in list_jobs_with_phase(batch, core, "ns", "sel")}
    assert result["running-job"] == ("running", None)
    assert result["stuck-job"] == ("blocked", "ErrImagePull: not found")


def test_pod_termination_reason_reports_oom_and_eviction():
    """OOM-kill and eviction are the infra causes a scenario log can't explain."""
    oom = _pod("j", terminated=("robovast", "OOMKilled"))
    assert pod_termination_reason(oom) == (
        "OOMKilled", "container robovast exceeded its memory limit")
    evicted = _pod("j", pod_reason=("Evicted", "The node was low on resource: memory."))
    assert pod_termination_reason(evicted) == (
        "Evicted", "The node was low on resource: memory.")
    # A clean run reports nothing (no false positives).
    assert pod_termination_reason(_pod("j", "Running")) is None


def test_list_jobs_with_phase_explains_oom_killed_failure():
    """A failed job whose pod was OOM-killed carries the reason as its detail, so the
    truncated scenario log is no longer a dead end."""
    jobs = [_job("oom-job", failed=1)]
    batch = _Batch(jobs)
    core = _Core([_pod("oom-job", terminated=("robovast", "OOMKilled"))])
    result = {j.metadata.name: (p, d)
              for j, p, d in list_jobs_with_phase(batch, core, "ns", "sel")}
    assert result["oom-job"] == (
        "failed", "OOMKilled: container robovast exceeded its memory limit")


def test_list_jobs_with_phase_marks_kueue_suspended_job_waiting():
    """A Kueue-suspended Job has no pod, so the pod probe cannot see it and it used to
    report ``pending`` — indistinguishable from a job about to start. It is ``waiting``
    (its own phase, not ``blocked``: queueing for capacity is healthy) with Kueue's own
    wait message."""
    jobs = [_job("queued-job", suspend=True), _job("running-job", active=1)]
    core = _Core([_pod("running-job", "Running")])
    with mock.patch(
        "robovast.execution.cluster_execution.kubernetes_kueue.workload_wait_reasons",
        return_value={"queued-job": "insufficient unused quota for cpu"},
    ):
        result = {j.metadata.name: (p, d)
                  for j, p, d in list_jobs_with_phase(_Batch(jobs), core, "ns", "sel")}
    assert result["queued-job"] == ("waiting", "insufficient unused quota for cpu")
    assert result["running-job"] == ("running", None)


def test_suspended_job_without_workload_still_reports_waiting():
    """An unreadable / absent Workload must not downgrade the job back to pending: the
    status is what makes a batch that never starts visible, the message only explains
    it."""
    jobs = [_job("queued-job", suspend=True)]
    with mock.patch(
        "robovast.execution.cluster_execution.kubernetes_kueue.workload_wait_reasons",
        return_value={},
    ):
        result = {j.metadata.name: (p, d)
                  for j, p, d in list_jobs_with_phase(_Batch(jobs), _Core([]), "ns", "sel")}
    phase, detail = result["queued-job"]
    assert phase == "waiting"
    assert "Kueue admission" in detail


def test_suspended_job_with_unstartable_pod_stays_blocked():
    """``blocked`` outranks ``waiting``: a pod-level error is the reason a human is
    needed, and it must not be softened into "queued for capacity"."""
    jobs = [_job("queued-job", suspend=True)]
    core = _Core([_pod("queued-job", waiting=("ErrImagePull", "not found"))])
    with mock.patch(
        "robovast.execution.cluster_execution.kubernetes_kueue.workload_wait_reasons",
        return_value={"queued-job": "insufficient unused quota for cpu"},
    ):
        result = {j.metadata.name: (p, d)
                  for j, p, d in list_jobs_with_phase(_Batch(jobs), core, "ns", "sel")}
    assert result["queued-job"] == ("blocked", "ErrImagePull: not found")


def test_unsuspended_jobs_never_query_kueue():
    """The common case must not pay for a Workload list on every poll."""
    jobs = [_job("a", active=1)]
    with mock.patch(
        "robovast.execution.cluster_execution.kubernetes_kueue.workload_wait_reasons",
    ) as wl:
        list_jobs_with_phase(_Batch(jobs), _Core([_pod("a", "Running")]), "ns", "sel")
    wl.assert_not_called()


def test_unschedulable_pod_is_blocked_with_the_schedulers_own_message():
    """A pod Kueue admitted but the scheduler cannot place.

    This is the shape every capacity or quota mistake takes, and before it was reported
    the batch logged "still running" until activeDeadlineSeconds fired an hour later and
    then blamed the scenario. The scheduler's message names the missing resource, which
    is the entire diagnosis, so it is carried through verbatim.
    """
    msg = "0/1 nodes are available: 1 Insufficient nvidia.com/gpu."
    core = _Core([_pod("gpu-job", unschedulable=("Unschedulable", msg))])
    assert blocked_job_reasons(core, "ns", "sel") == {
        "gpu-job": f"Unschedulable: {msg}"}


def test_unschedulable_without_node_sizes_shows_as_blocked_in_the_listing():
    """"Insufficient cpu" alone does not say whether the node is busy or the request is
    impossible, and this `_Core` serves no node list -- so the listing cannot rule the
    latter out and must not soften the row. The busy case, where node sizes ARE readable
    and the job lists as `pending`, lives in test_scheduling_contention.py.
    """
    batch = _Batch([_job("gpu-job", active=1)])
    core = _Core([_pod("gpu-job", unschedulable=("Unschedulable", "Insufficient cpu."))])
    result = {j.metadata.name: (p, d)
              for j, p, d in list_jobs_with_phase(batch, core, "ns", "sel")}
    assert result["gpu-job"] == ("blocked", "Unschedulable: Insufficient cpu.")


def test_a_schedulable_pod_is_not_reported_blocked():
    """PodScheduled=True must not trip the check -- every healthy pod has that condition."""
    pod = _pod("j", "Running")
    pod.status.conditions = [types.SimpleNamespace(
        type="PodScheduled", status="True", reason=None, message=None)]
    assert blocked_job_reasons(_Core([pod]), "ns", "sel") == {}


def test_a_restarted_container_is_reported_while_the_pod_still_runs():
    """The simulator is a native sidecar, so the kubelet restarts it without failing the
    pod and the scenario carries on against a simulator that lost all its state. The
    restart is therefore the signal -- not the pod phase, which is still Running."""
    pod = _pod("j", restarts=("simulation", 1, "Error", 139))
    assert pod_invalidating_restart(pod) == (
        "ContainerRestarted",
        "container simulation restarted 1x after Error (exit 139, SIGSEGV)")
    assert restarted_job_reasons(_Core([pod]), "ns", "sel") == {
        "j": "ContainerRestarted: container simulation restarted 1x after Error "
             "(exit 139, SIGSEGV)"}


def test_a_clean_pod_reports_no_restart():
    assert pod_invalidating_restart(_pod("j", "Running")) is None
    assert restarted_job_reasons(_Core([_pod("j", "Running")]), "ns", "sel") == {}


def test_a_sidecar_that_finished_its_work_is_not_an_invalidating_restart():
    """The recorded regression: a PASSING run marked failed by `container sut restarted 1x
    after Completed (exit 0)`. A native sidecar whose workload ends exits 0 and the kubelet
    restarts it, because that is what restartPolicy Always means -- the trial is untouched.
    Reading that as a crash is a guard that shrinks a sweep silently."""
    pod = _pod("j", restarts=("sut", 1, "Completed", 0))
    assert pod_invalidating_restart(pod) is None
    assert restarted_job_reasons(_Core([pod]), "ns", "sel") == {}


def test_the_rollout_watcher_still_sees_every_restart():
    """`pod_restarted_containers` keeps the unconditional reading: on a long-lived service
    replica a container that exits 0 and comes back IS a crash-loop."""
    pod = _pod("j", restarts=("sut", 1, "Completed", 0))
    assert pod_restarted_containers(pod) == (
        "ContainerRestarted", "container sut restarted 1x after Completed (exit 0)")


def test_exit_135_is_reported_as_sigbus():
    """128 + 7. The campaign that motivated this had the translation done by hand, from a
    log line, after its pod was gone."""
    record, = pod_container_failures(_pod("j", restarts=("sut", 1, "Error", 135)))
    assert (record["signal"], record["signal_name"]) == (7, "SIGBUS")
    assert record["role"] == "sut"
    assert record["invalidating"] is True


def test_a_container_with_no_memory_limit_records_that_as_a_finding():
    """None means NO limit, and the absence is the finding: such a container is told by the
    downward API that it has the whole node."""
    record, = pod_container_failures(_pod("j", restarts=("sut", 1, "Error", 135)))
    assert record["memory_limit"] is None
    assert record["cpu_limit"] is None
    with_limits = _pod("j", restarts=("sut", 1, "Error", 135), limits={"memory": "4Gi"})
    assert pod_container_failures(with_limits)[0]["memory_limit"] == "4Gi"


def test_a_one_shot_init_container_restart_does_not_invalidate_a_trial():
    """The pod is restartPolicy Never, so `s3-init` cannot be restarted by it at all. If
    one ever is, record it -- but it is not the trial that was invalidated."""
    pod = _pod("j", restarts=("s3-init", 1, "Error", 1), sidecar=False)
    record, = pod_container_failures(pod)
    assert record["role"] == "init"
    assert record["invalidating"] is False
    assert pod_invalidating_restart(pod) is None


def test_every_restarted_container_is_recorded_not_just_the_first():
    """Their order is declaration order, not causal order. Stopping at the first reports a
    coin toss between the cause and the consequence."""
    pod = _pod("j", restarts=("sut", 1, "Error", 135))
    pod.status.init_container_statuses.append(
        types.SimpleNamespace(
            name="simulation", image="i", image_id="i@sha", restart_count=2,
            state=types.SimpleNamespace(waiting=None, terminated=None),
            last_state=types.SimpleNamespace(terminated=types.SimpleNamespace(
                reason="OOMKilled", exit_code=137, signal=None, message=None,
                started_at=None, finished_at=None))))
    pod.spec.init_containers.append(
        _container("simulation", restart_policy="Always"))
    records = pod_container_failures(pod)
    assert [r["container"] for r in records] == ["sut", "simulation"]
    assert [r["signal_name"] for r in records] == ["SIGBUS", "SIGKILL"]
    _, detail = pod_invalidating_restart(pod)
    assert detail.endswith("; and 1 other container")


def test_restarts_are_scoped_to_the_jobs_asked_about():
    """Jobs linger for ttlSecondsAfterFinished and the label selector is campaign-wide, so
    an earlier batch's pod is still listed while a later batch runs. Without the scope, one
    restart is re-reported to -- and acted on by -- every batch that follows it."""
    old = _pod("previous-batch-job", restarts=("sut", 1, "Error", 135))
    mine = _pod("this-batch-job", restarts=("sut", 1, "Error", 135))
    core = _Core([old, mine])
    assert set(restarted_job_reasons(core, "ns", "sel")) == {
        "previous-batch-job", "this-batch-job"}
    assert set(restarted_job_reasons(core, "ns", "sel",
                                     job_names=["this-batch-job"])) == {"this-batch-job"}


def test_an_unreadable_pod_spec_is_read_strictly():
    """No spec means no workload set. Treat that as no filter rather than no containers:
    an unreadable cluster yields the stricter answer, as it does for a blocked job."""
    pod = _pod("j", restarts=("sut", 1, "Error", 135))
    pod.spec = None
    record, = pod_container_failures(pod)
    assert record["invalidating"] is True


def test_a_restart_the_kubelet_has_not_explained_is_treated_as_a_crash():
    """A missing last_state means the kubelet has not said what the previous instance died
    of. Treating silence as a clean exit is how a broken trial gets believed."""
    pod = _pod("j", restarts=("sut", 1, "Error", 135))
    pod.status.init_container_statuses[0].last_state = None
    record, = pod_container_failures(pod)
    assert record["exit_code"] is None
    assert record["invalidating"] is True


def test_a_restart_surfaces_in_the_listing_even_though_the_job_looks_healthy():
    """The case worth catching: a job on its way to a plausible result its simulator can
    no longer justify."""
    batch = _Batch([_job("j", active=1)])
    core = _Core([_pod("j", restarts=("simulation", 2, "OOMKilled", 137))])
    result = {jb.metadata.name: (p, d)
              for jb, p, d in list_jobs_with_phase(batch, core, "ns", "sel")}
    phase, detail = result["j"]
    assert phase == "running"
    assert "restarted 2x after OOMKilled" in detail
