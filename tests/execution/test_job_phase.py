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
    job_phase, list_jobs_with_phase, running_scenario_job_names)


def _job(name, *, succeeded=0, active=0, failed=0):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels={}, annotations={}),
        status=types.SimpleNamespace(succeeded=succeeded, active=active, failed=failed))


def _pod(job_name, phase="Running"):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=f"{job_name}-pod", labels={"batch.kubernetes.io/job-name": job_name}),
        status=types.SimpleNamespace(phase=phase))


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
                  for j, p in list_jobs_with_phase(batch, core, "ns", "sel"))
    assert phases == {"running-job": "running", "pending-job": "pending",
                      "done-job": "completed"}
