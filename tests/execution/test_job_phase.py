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

from robovast.execution.cluster_execution.cluster_execution import (
    blocked_job_reasons, job_phase, list_jobs_with_phase,
    pod_termination_reason, running_scenario_job_names)


def _job(name, *, succeeded=0, active=0, failed=0):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels={}, annotations={}),
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
    """No pod set supplied → fall back to the Job-level view."""
    assert job_phase(_job("j", active=1)) == "running"
    assert job_phase(_job("j", succeeded=1)) == "completed"
    assert job_phase(_job("j", failed=1)) == "failed"
    assert job_phase(_job("j")) == "pending"


def test_job_phase_active_pod_pending_is_pending():
    """An active Job whose pod is Pending must classify as pending, not running."""
    assert job_phase(_job("j", active=1), running_job_names=set()) == "pending"
    assert job_phase(_job("j", active=1), running_job_names={"j"}) == "running"


def test_running_scenario_job_names_only_counts_running_pods():
    core = _Core([_pod("a", "Running"), _pod("b", "Pending"), _pod("c", "Running")])
    assert running_scenario_job_names(core, "ns", "sel") == {"a", "c"}


def test_running_scenario_job_names_swallows_api_errors():
    class _Boom:
        def list_namespaced_pod(self, namespace, label_selector):
            raise RuntimeError("boom")

    assert running_scenario_job_names(_Boom(), "ns", "sel") == set()


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
