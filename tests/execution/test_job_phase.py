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
                                                                    pod_termination_reason,
                                                                    running_scenario_job_names)


def _job(name, *, succeeded=0, active=0, failed=0, suspend=False):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels={}, annotations={}),
        spec=types.SimpleNamespace(suspend=suspend),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed))


def _pod(job_name, phase="Running", *, waiting=None, terminated=None, pod_reason=None):
    """A pod for *job_name*.

    ``waiting=(reason, message)`` puts its container in that ``waiting`` state (as
    ImagePullBackOff would), phase Pending. ``terminated=(container, reason)`` puts a
    container in that ``terminated`` state (as OOMKilled would), phase Failed.
    ``pod_reason=(reason, message)`` sets a pod-level termination reason (Evicted /
    DeadlineExceeded), phase Failed.
    """
    container_statuses = None
    reason_attr = None
    message_attr = None
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
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"{job_name}-pod", labels={"batch.kubernetes.io/job-name": job_name}),
        status=types.SimpleNamespace(
            phase=phase, container_statuses=container_statuses,
            init_container_statuses=None, reason=reason_attr, message=message_attr))


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
