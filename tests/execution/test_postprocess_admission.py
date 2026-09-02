# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Postprocessing waits for capacity instead of failing against a full cluster.

Its cpu request equals its limit, so on a cluster kept full by other campaigns' trials --
which pack by request and burst past it -- no node has that much free, and a pod created
regardless is ``Unschedulable``. Observed: a campaign reported ``postprocessing failed``
with its results intact, needing a manual re-run, on a cluster whose four nodes had 1.0,
1.5, 2.85 and 1.14 cores free against a four-core pod.
"""

from robovast.execution.cluster_execution import postprocess_job as pj
from robovast.execution.cluster_execution.node_admission import (AdmissionController, Budget,
                                                                 Capacity, JobSizing,
                                                                 NodeBudget)


class _Provider:
    """A cluster of one node, whose free capacity the test moves."""

    def __init__(self, free_cpu, node_cpu=8.0):
        self.free_cpu = free_cpu
        self.node_cpu = node_cpu

    def budget(self):
        return Budget(nodes=(NodeBudget(node_id="node-a", free_cpu=self.free_cpu,
                                        free_memory=64 * 1024 ** 3, free_gpu=0),),
                      counted_jobs=frozenset(), growable=False)

    def capacities(self):
        return [Capacity(node_id="node-a", cpu=self.node_cpu,
                         memory=64 * 1024 ** 3, gpu=0)]


def _manifest(cpu="4", memory="4Gi"):
    """Just the shape :func:`pod_sizing` and the pin read."""
    def _container(name, cpu, memory):
        return {"name": name,
                "resources": {"requests": {"cpu": cpu, "memory": memory},
                              "limits": {"cpu": cpu, "memory": memory}}}

    return {"metadata": {"name": "pp-job"},
            "spec": {"template": {"spec": {
                "initContainers": [_container("stage", "2", "1Gi"),
                                   _container("convert", cpu, memory)],
                "containers": [_container("host", cpu, memory)]}}}}


# -- what the queue is asked for -----------------------------------------------------


def test_the_queue_is_asked_for_the_max_over_steps_not_their_sum():
    """``JobSizing`` is documented as the sum over a pod's containers, which is right for a
    pod of ordinary containers and wrong for this one.

    Staging and conversion are initContainers, which run to completion one at a time before
    the main container starts, so Kubernetes charges ``max(max(init), sum(main))``. Asking
    for the sum would demand ten cores on behalf of a pod that requests four, and the queue
    would hold it out of a cluster with room -- the opposite of the point.
    """
    sizing = pj.pod_sizing(_manifest())
    assert sizing.cpu == 4.0
    assert sizing.memory == 4 * 1024 ** 3


def test_a_declared_figure_is_what_the_queue_is_asked_for():
    assert pj.pod_sizing(_manifest(cpu="8", memory="16Gi")).cpu == 8.0


# -- waiting rather than failing -----------------------------------------------------


def test_a_full_cluster_is_waited_out_rather_than_failed():
    """The behaviour this exists for: no node has four free cores now, one does later."""
    provider = _Provider(free_cpu=1.0)
    admission = AdmissionController(provider, budget_ttl=0.0)

    ok, _node, message = pj.await_admission(admission, "camp-1", "pp-job", _manifest(),
                                            timeout=0.2, poll=0.01)
    assert not ok and "stayed full" in message

    provider.free_cpu = 6.0
    ok, node_id, message = pj.await_admission(admission, "camp-1", "pp-job", _manifest(),
                                              timeout=2.0, poll=0.01)
    assert ok, message
    assert node_id == "node-a"


def test_the_wait_says_the_results_are_not_at_risk():
    """A campaign's runs are already published when this step runs, so a message that reads
    like data loss would send someone re-running trials that are fine."""
    admission = AdmissionController(_Provider(free_cpu=0.5), budget_ttl=0.0)
    _ok, _node, message = pj.await_admission(admission, "camp-1", "pp-job", _manifest(),
                                             timeout=0.05, poll=0.01)
    assert "runs are published" in message
    assert "re-run postprocessing" in message


def test_a_pod_no_node_could_ever_hold_is_refused_rather_than_waited_for():
    """Permanent, so it must not wait: waiting for a machine that does not exist would hold
    the campaign's results forever with nothing said. The message names the knob."""
    admission = AdmissionController(_Provider(free_cpu=0.0, node_cpu=2.0), budget_ttl=0.0)
    ok, _node, message = pj.await_admission(admission, "camp-1", "pp-job",
                                            _manifest(cpu="16", memory="32Gi"),
                                            timeout=5.0, poll=0.01)
    assert not ok
    assert "no node in this cluster is that large" in message
    assert "results_processing.resources" in message


def test_the_grant_is_pinned_to_the_node_it_was_granted_on():
    """The pin is what makes the grant mean something: the queue found room on a particular
    machine, and a pod free to land anywhere can still arrive at a full one."""
    manifest = _manifest()
    pj._pin_to(manifest, "node-a")
    selector = manifest["spec"]["template"]["spec"]["nodeSelector"]
    assert selector["robovast.io/node-id"] == "node-a"


def test_an_unpinned_grant_leaves_no_node_selector():
    """A growable cluster admits unpinned, and a selector naming no node must not appear."""
    manifest = _manifest()
    pj._pin_to(manifest, None)
    assert "robovast.io/node-id" not in (
        manifest["spec"]["template"]["spec"].get("nodeSelector") or {})


# -- where it sits in the queue ------------------------------------------------------


def test_postprocessing_outranks_trials_and_probes():
    """Higher number is earlier (``-priority`` in the queue's sort).

    A trial that waits is a campaign progressing more slowly; postprocessing that waits is a
    campaign that has already spent all its compute with nothing to show for it -- and being
    submitted last by construction, it would wait indefinitely behind a cluster kept full.
    """
    assert pj.POSTPROCESS_PRIORITY > 1  # a calibration probe
    assert pj.POSTPROCESS_PRIORITY > 0  # a campaign's own trials


def test_it_is_placed_ahead_of_work_already_queued():
    """The ordering that matters, exercised rather than asserted about a constant."""
    admission = AdmissionController(_Provider(free_cpu=4.0), budget_ttl=0.0)
    created = []
    admission.submit("other-campaign",
                     [(f"trial-{i}", JobSizing(cpu=4.0, memory=1024 ** 3),
                       lambda node_id, i=i: created.append(f"trial-{i}"))
                      for i in range(3)],
                     started_at=0.0, priority=0)

    ok, _node, _message = pj.await_admission(admission, "camp-1", "pp-job", _manifest(),
                                             timeout=2.0, poll=0.01)

    # Only one four-core slot existed, and postprocessing took it ahead of three trials
    # that were queued first.
    assert ok
    assert created == []


def test_the_reservation_is_released_when_it_is_not_granted():
    """A wait that times out must not leave the cluster smaller by a pod that never ran."""
    admission = AdmissionController(_Provider(free_cpu=0.5), budget_ttl=0.0)
    pj.await_admission(admission, "camp-1", "pp-job", _manifest(), timeout=0.05, poll=0.01)
    assert admission.states("camp-1" + pj._POSTPROCESS_OWNER_SUFFIX) == {}


def test_a_campaigns_postprocessing_is_a_separate_owner_from_its_trials():
    """One campaign has two kinds of work outstanding, and a refusal message has to name
    which -- the same device the calibration probe's owner suffix uses."""
    admission = AdmissionController(_Provider(free_cpu=8.0), budget_ttl=0.0)
    pj.await_admission(admission, "camp-1", "pp-job", _manifest(), timeout=2.0, poll=0.01)
    assert admission.states("camp-1") == {}
    assert admission.states("camp-1" + pj._POSTPROCESS_OWNER_SUFFIX)


# -- the lanes that have no queue ----------------------------------------------------


def test_no_queue_means_create_directly():
    """A local service and an off-cluster driver have no admission controller, and
    postprocessing must still run there."""
    import inspect  # noqa: PLC0415

    for func in (pj.run_conversion_job, pj.postprocess_campaign):
        assert inspect.signature(func).parameters["admission"].default is None
