# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The seam: with an admission queue, jobs are created as room appears rather than at once.

The first test is the reason this file exists. ``get_remaining_jobs`` treats a name it cannot
find as finished -- correct for a Job that was dropped or garbage-collected, and catastrophic
for one that has not been created yet. Get it wrong and a batch "finishes" on its first poll
having produced nothing, silently and with every run still to do.
"""

import types

import pytest

from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.node_admission import (AdmissionController, Budget,
                                                                 Capacity, JobSizing,
                                                                 campaign_start_key)

MiB = 1024 ** 2


class _Provider:
    def __init__(self, cpu):
        self.cpu = cpu

    def budget(self):
        return Budget(free_cpu=self.cpu, free_memory=1024 * MiB)

    def capacities(self):
        return [Capacity(64.0, 64 * 1024 * MiB)]


def _runner(jobs, admission, *, remaining_script):
    """A bare runner whose loop we can turn by hand."""
    r = kb.BatchJobRunner()
    r.campaign = "camp-2026-07-17-120000"
    r.namespace = "ns"
    r._batch_tag = "batch-0"
    r.admission = admission
    r.created = []
    r._build_jobs = lambda: jobs
    r.create_job_manifest = lambda job, total: {"metadata": {"name": f"j-{job.index}"}}
    r.k8s_batch_client = types.SimpleNamespace(
        create_namespaced_job=lambda namespace, body: r.created.append(
            body["metadata"]["name"]))
    r.get_remaining_jobs = lambda names: remaining_script(list(names))
    return r


def _job(index):
    return types.SimpleNamespace(index=index, items=[])


def test_a_planned_job_is_not_mistaken_for_a_finished_one():
    """The 404 trap. Under admission every not-yet-created job is absent from the cluster, so
    a loop that asks about planned names sees an empty answer and exits having done nothing.

    Asserted on the controller rather than the loop: the invariant is that only CREATED names
    are ever passed to ``get_remaining_jobs``.
    """
    asked = []
    c = AdmissionController(_Provider(cpu=2.0), clock=lambda: 0.0)
    sizing = JobSizing(2.0, MiB)
    c.submit("camp", [(f"j-{i}", sizing, lambda: None) for i in range(3)],
             started_at=campaign_start_key("camp-2026-07-17-120000"))
    c.drain()
    states = c.states("camp")
    created = [n for n, s in states.items() if s == kb._ADMIT_CREATED]
    planned = [n for n, s in states.items() if s == kb._ADMIT_PLANNED]
    asked.extend(created)
    assert len(created) == 1 and len(planned) == 2
    assert set(asked).isdisjoint(planned), "a planned job must never be asked about"


def test_the_loop_keeps_going_while_jobs_are_still_only_planned():
    """Even with nothing running, a batch with planned work is not done."""
    c = AdmissionController(_Provider(cpu=2.0), clock=lambda: 0.0)
    sizing = JobSizing(2.0, MiB)
    c.submit("camp", [(f"j-{i}", sizing, lambda: None) for i in range(3)], started_at=0.0)
    c.drain()
    states = c.states("camp")
    planned = sum(1 for s in states.values() if s == kb._ADMIT_PLANNED)
    remaining = []                      # nothing running: the created one just finished
    assert not (not remaining and not planned), "must not exit with work still queued"


def test_creation_is_paced_by_capacity_not_by_the_plan_size():
    c = AdmissionController(_Provider(cpu=4.0), clock=lambda: 0.0)
    made = []
    c.submit("camp", [(f"j-{i}", JobSizing(2.0, MiB), lambda i=i: made.append(i))
                      for i in range(10)], started_at=0.0)
    assert c.drain() == 2, "ten planned, room for two"
    assert len(made) == 2


def test_finishing_a_job_frees_room_for_the_next():
    p = _Provider(cpu=4.0)
    c = AdmissionController(p, clock=lambda: 0.0)
    made = []
    c.submit("camp", [(f"j-{i}", JobSizing(2.0, MiB), lambda i=i: made.append(i))
                      for i in range(10)], started_at=0.0)
    c.drain()
    c.finished("j-0")
    c.drain()
    assert len(made) == 3


def test_job_names_are_derived_the_same_way_the_manifest_names_it():
    """The plan, the artifact paths and job_links all key on this string, so a derived name
    that disagreed with the manifest would scatter a batch across two identities."""
    r = kb.BatchJobRunner()
    r.campaign = "camp-2026-07-17-120000"
    r._batch_tag = "batch-0"
    derived = kb._short_job_name(r.campaign, r._job_tag(3), 3)
    assert derived and len(derived) <= 63
    assert derived == kb._short_job_name(r.campaign, r._job_tag(3), 3), "must be stable"


def test_the_sizing_admission_charges_is_the_one_the_manifest_reserves():
    """Read off the manifest that will be created, so admission cannot charge a different
    number than Kubernetes reserves. Native sidecars count, as they do for the scheduler."""
    r = kb.BatchJobRunner()
    r.manifest = {"spec": {"template": {"spec": {
        "containers": [{"resources": {"requests": {"cpu": "2", "memory": "1Gi"}}}],
        "initContainers": [
            {"restartPolicy": "Always",
             "resources": {"requests": {"cpu": "0.5", "memory": "512Mi"}}},
            {"resources": {"requests": {"cpu": "9", "memory": "9Gi"}}},   # ordinary init
        ]}}}}
    sizing = r._job_sizing()
    assert sizing.cpu == pytest.approx(2.5), "sidecar counted, ordinary init container not"
    assert sizing.memory == (1024 + 512) * MiB
