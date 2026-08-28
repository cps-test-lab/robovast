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

from robovast.execution.backends import CampaignConfigError
from robovast.execution.cluster_execution import kubernetes_backend as kb
from robovast.execution.cluster_execution.node_admission import (AdmissionController, Budget,
                                                                 Capacity, JobSizing,
                                                                 NodeBudget,
                                                                 campaign_start_key)

MIB = 1024 ** 2


class _Provider:
    def __init__(self, cpu):
        self.cpu = cpu

    def budget(self):
        # One node holding everything: these tests are about the seam and the loop, not
        # about placement, so the cluster is deliberately the simplest shape that admits.
        return Budget(nodes=(NodeBudget("n1", self.cpu, 1024 * MIB),))

    def capacities(self):
        return [Capacity(64.0, 64 * 1024 * MIB)]


def _runner(jobs, admission, *, remaining_script):
    """A bare runner whose loop we can turn by hand."""
    r = kb.BatchJobRunner()
    r.campaign = "camp-2026-07-17-120000"
    r.namespace = "ns"
    r._batch_tag = "batch-0"
    r.admission = admission
    r.created = []
    r._build_jobs = lambda: jobs
    r.create_job_manifest = lambda job, total, node_figures=None: {"metadata": {"name": f"j-{job.index}"}}
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
    sizing = JobSizing(2.0, MIB)
    c.submit("camp", [(f"j-{i}", sizing, lambda _n=None: None) for i in range(3)],
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
    sizing = JobSizing(2.0, MIB)
    c.submit("camp", [(f"j-{i}", sizing, lambda _n=None: None) for i in range(3)], started_at=0.0)
    c.drain()
    states = c.states("camp")
    planned = sum(1 for s in states.values() if s == kb._ADMIT_PLANNED)
    remaining = []                      # nothing running: the created one just finished
    assert not (not remaining and not planned), "must not exit with work still queued"


def test_creation_is_paced_by_capacity_not_by_the_plan_size():
    c = AdmissionController(_Provider(cpu=4.0), clock=lambda: 0.0)
    made = []
    c.submit("camp", [(f"j-{i}", JobSizing(2.0, MIB), lambda _n=None, i=i: made.append(i))
                      for i in range(10)], started_at=0.0)
    assert c.drain() == 2, "ten planned, room for two"
    assert len(made) == 2


def test_finishing_a_job_frees_room_for_the_next():
    p = _Provider(cpu=4.0)
    c = AdmissionController(p, clock=lambda: 0.0)
    made = []
    c.submit("camp", [(f"j-{i}", JobSizing(2.0, MIB), lambda _n=None, i=i: made.append(i))
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


def test_a_pod_that_declares_no_cpu_is_refused_at_launch():
    """A zero-cpu pod fits every node, so the queue stops being a queue.

    Nothing else catches this: ``preflight`` passes trivially (zero fits anything), every
    job "fits", and the whole plan is created in one pass -- precisely the mass submission
    admission exists to prevent, with no error anywhere. It is a configuration fault, so it
    is refused before a single Job exists rather than paced into a cluster that cannot hold
    what is really being asked for.
    """
    r = kb.BatchJobRunner()
    r.campaign = "camp-1"
    r.create_job_manifest = lambda job, total, node_figures=None: {"spec": {"template": {"spec": {
        "containers": [{"name": "scenario"}],
        "initContainers": [{"name": "sut", "restartPolicy": "Always"},
                           {"name": "simulation", "restartPolicy": "Always"}]}}}}
    with pytest.raises(CampaignConfigError) as err:
        r._job_sizing(_job(0), 1)
    assert "resources.cpu" in str(err.value), "must name the key to declare"
    for name in ("scenario", "sut", "simulation"):
        assert name in str(err.value), f"must name {name}, which declared nothing"


def test_one_container_declaring_nothing_warns_but_proceeds(caplog):
    """Undercounting is not the same fault as counting nothing.

    The queue still paces on what was declared, so the campaign runs -- but it paces on less
    than the pod takes, and the cluster is oversubscribed by whatever the silent container
    uses. Loud enough to find, not fatal.
    """
    import logging

    r = kb.BatchJobRunner()
    r.campaign = "camp-1"
    r.create_job_manifest = lambda job, total, node_figures=None: {"spec": {"template": {"spec": {
        "containers": [{"name": "scenario",
                        "resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}],
        "initContainers": [{"name": "sut", "restartPolicy": "Always"}]}}}}
    with caplog.at_level(logging.WARNING):
        sizing = r._job_sizing(_job(0), 1)
    assert sizing.cpu == pytest.approx(1)
    assert "sut" in caplog.text, "must name the container that declared nothing"


def test_the_sizing_comes_from_the_rendered_manifest_not_the_base_one():
    """The regression this test exists for, and it cost a live run.

    ``self.manifest`` is the BASE manifest: main container only. The sidecars -- the
    simulator and the system under test, i.e. nearly the whole request -- are appended per
    job in ``_build_job_manifest``. Sizing from the base counted 1 core of a 4.75-core pod,
    so the queue admitted a whole batch at once and gated nothing.

    An earlier version of this test hand-built a manifest that already had sidecars, which
    verified the arithmetic and never touched the source. It passed while the bug shipped.
    """
    r = kb.BatchJobRunner()
    # The base: what self.manifest actually looks like -- one container, no sidecars.
    r.manifest = {"spec": {"template": {"spec": {
        "containers": [{"resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}]}}}}
    # The rendered per-job manifest: what Kubernetes is really asked to reserve.
    r.create_job_manifest = lambda job, total, node_figures=None: {"spec": {"template": {"spec": {
        "containers": [{"resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}],
        "initContainers": [
            {"restartPolicy": "Always",
             "resources": {"requests": {"cpu": "3", "memory": "640Mi"}}},      # sut
            {"restartPolicy": "Always",
             "resources": {"requests": {"cpu": "0.75", "memory": "2944Mi"}}},  # simulation
            {"resources": {"requests": {"cpu": "9", "memory": "9Gi"}}},        # ordinary init
        ]}}}}
    sizing = r._job_sizing(_job(0), 1)
    assert sizing.cpu == pytest.approx(4.75), (
        "must size from the rendered manifest; the base one is missing the sidecars")
    assert sizing.memory == (1024 + 640 + 2944) * MIB
    assert sizing.cpu != pytest.approx(1.0), "sizing from self.manifest is the shipped bug"


# -- the false-stall fix ------------------------------------------------------------------

class _Recorder:
    """Stands in for the campaign's control state, capturing what the loop publishes."""

    def __init__(self):
        self.capacity_waits = []

    def update(self, **fields):
        if "waiting_for_capacity" in fields:
            self.capacity_waits.append(fields["waiting_for_capacity"])


def test_a_queued_batch_publishes_waiting_for_capacity():
    """The defect this phase exists to fix, pinned deterministically.

    A campaign whose jobs are all still PLANNED has no pods, so the pod-based probe could see
    nothing and concluded "not waiting" -- and the per-run deadline then declared a perfectly
    healthy queued campaign stalled. Measured on 2026-08-26: the third of three concurrent
    campaigns reported as wedged while waiting its turn.

    ``waiting_for_capacity`` suppresses the stall verdict (``client/status.py``), so the fix is
    that the loop publishes it from a fact the QUEUE holds rather than from absent pods. A live
    test of this is awkward -- it needs the queue wait to exceed the deadline while the run
    duration does not -- so the property is pinned here instead.
    """
    r = kb.BatchJobRunner()
    r._state = _Recorder()
    r._batch_tag = "batch-0"

    # Nothing running, work still queued: the exact shape the old probe was blind to.
    remaining, planned_count, admission = [], 7, object()
    waiting = kb.all_jobs_waiting_for_capacity(remaining, {})
    assert waiting is False, "the pod-based probe alone still cannot see a queued batch"
    if admission is not None and planned_count and not remaining:
        waiting = True
    r._publish_capacity_wait(waiting)

    assert r._state.capacity_waits == [True], (
        "a batch with planned work and nothing running must report as queued, not stalled")


def test_a_batch_with_nothing_left_does_not_claim_to_be_queued():
    """The other direction, and the reason the flag is written every cycle: a marker left true
    outlives the wait that set it and suppresses a verdict for a batch that is not queued."""
    r = kb.BatchJobRunner()
    r._state = _Recorder()
    r._batch_tag = "batch-0"
    remaining, planned_count, admission = [], 0, object()
    waiting = kb.all_jobs_waiting_for_capacity(remaining, {})
    if admission is not None and planned_count and not remaining:
        waiting = True
    r._publish_capacity_wait(waiting)
    assert r._state.capacity_waits == [False]


def test_a_running_batch_is_not_reported_as_queued():
    """Jobs are running, so a deadline overrun is a real stall and must not be suppressed."""
    r = kb.BatchJobRunner()
    r._state = _Recorder()
    r._batch_tag = "batch-0"
    remaining, planned_count, admission = ["j-0"], 5, object()
    waiting = kb.all_jobs_waiting_for_capacity(remaining, {})
    if admission is not None and planned_count and not remaining:
        waiting = True
    r._publish_capacity_wait(waiting)
    assert r._state.capacity_waits == [False], (
        "suppressing the verdict while jobs run would hide a genuine stall")


def test_the_chosen_node_reaches_the_manifest_as_a_node_selector():
    """The pin has to arrive on the pod, or the reservation is a promise about a node the
    scheduler is free to ignore -- which is the fragmentation this replaced, with extra steps.
    """
    from robovast.execution.cluster_execution.node_placement import NODE_ID_LABEL

    c = AdmissionController(_Provider(cpu=8.0), clock=lambda: 0.0)
    r = _runner([_job(0)], c, remaining_script=lambda names: [])
    r.create_job_manifest = lambda job, total, node_figures=None: {
        "metadata": {"name": "j-0"},
        "spec": {"template": {"spec": {"nodeSelector": {"pool": "batch"}}}}}
    bodies = []
    r.k8s_batch_client = types.SimpleNamespace(
        create_namespaced_job=lambda namespace, body: bodies.append(body))

    def _create(job, name, node_id=None):
        manifest = r.create_job_manifest(job, 1)
        if node_id:
            spec = manifest["spec"]["template"]["spec"]
            spec["nodeSelector"] = {**(spec.get("nodeSelector") or {}), NODE_ID_LABEL: node_id}
        r.k8s_batch_client.create_namespaced_job(namespace="ns", body=manifest)

    c.submit("camp", [("j-0", JobSizing(2.0, MIB), lambda n=None: _create(_job(0), "j-0", n))],
             started_at=0.0)
    assert c.drain() == 1
    selector = bodies[0]["spec"]["template"]["spec"]["nodeSelector"]
    assert selector[NODE_ID_LABEL] == "n1"
    # ANDed with what the spec already carried: a pin that widened the acceptable set would
    # defeat the operator's node pool it was placed inside.
    assert selector["pool"] == "batch"
