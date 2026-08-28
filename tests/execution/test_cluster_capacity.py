# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The capacity reading behind admission, against fake Kubernetes objects.

SimpleNamespace graphs shaped like the client's model objects, no cluster.
"""

import types

import pytest

from robovast.execution.cluster_execution import cluster_capacity
from robovast.execution.cluster_execution.cluster_capacity import ClusterBudgetProvider

MIB = 1024 ** 2


def _node(name, cpu="8", memory="16Gi", gpu=None, node_id=True,
          ready=True, cordoned=False, taints=()):
    """A node, carrying its identity label unless *node_id* is False.

    ``node_id=False`` is a node that joined since the last ``setup``: still counted, because
    its pods and capacity are real, but nothing can be pinned to it.

    *ready*, *cordoned* and *taints* are the three tests the scheduler applies first. They
    default to a healthy node, so every existing test keeps meaning what it did.
    """
    from robovast.execution.cluster_execution.node_placement import NODE_ID_LABEL

    alloc = {"cpu": cpu, "memory": memory}
    if gpu:
        alloc["nvidia.com/gpu"] = gpu
    labels = {NODE_ID_LABEL: f"node-{name}"} if node_id else {}
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels=labels),
        spec=types.SimpleNamespace(unschedulable=cordoned, taints=list(taints)),
        status=types.SimpleNamespace(
            allocatable=alloc,
            conditions=[types.SimpleNamespace(
                type="Ready", status="True" if ready else "False")]))


def _taint(key, value, effect="NoSchedule"):
    return types.SimpleNamespace(key=key, value=value, effect=effect)


def _c(cpu=None, memory=None, gpu=None):
    req = {}
    if cpu:
        req["cpu"] = cpu
    if memory:
        req["memory"] = memory
    if gpu:
        req["nvidia.com/gpu"] = gpu
    return types.SimpleNamespace(resources=types.SimpleNamespace(requests=req),
                                 restart_policy=None)


def _pod(node, *containers, job=None, sidecars=(), phase="Running"):
    init = [types.SimpleNamespace(resources=c.resources, restart_policy="Always")
            for c in sidecars]
    labels = {"batch.kubernetes.io/job-name": job} if job else {}
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="p", namespace="default", labels=labels,
                                       owner_references=None),
        spec=types.SimpleNamespace(node_name=node, containers=list(containers),
                                   init_containers=init),
        status=types.SimpleNamespace(phase=phase))


def _provider(nodes, pods, monkeypatch, env=None):
    monkeypatch.delenv(cluster_capacity.HEADROOM_CPU_ENV, raising=False)
    monkeypatch.delenv(cluster_capacity.HEADROOM_MEMORY_ENV, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    core = types.SimpleNamespace(
        list_node=lambda **kw: types.SimpleNamespace(items=nodes),
        list_pod_for_all_namespaces=lambda **kw: types.SimpleNamespace(items=pods))
    core.calls = []
    return ClusterBudgetProvider(lambda: core), core


def test_free_is_allocatable_minus_committed_minus_headroom(monkeypatch):
    p, _ = _provider([_node("n1", cpu="8", memory="16Gi")],
                     [_pod("n1", _c(cpu="2", memory="4Gi"))], monkeypatch)
    b = p.budget()
    assert b.free_cpu == pytest.approx(8 - 2 - 1)          # default headroom 1 cpu
    assert b.free_memory == (16 - 4) * 1024 * MIB - 2 * 1024 * MIB   # default 2Gi


def test_native_sidecars_count(monkeypatch):
    """Kubernetes adds a restartPolicy:Always init container's requests to the pod total, and
    so does the scheduler -- missing them would over-admit by a whole simulator."""
    p, _ = _provider([_node("n1", cpu="8")],
                     [_pod("n1", _c(cpu="1"), sidecars=[_c(cpu="3")])], monkeypatch)
    assert p.budget().free_cpu == pytest.approx(8 - 4 - 1)


def test_a_pod_bound_to_no_node_is_not_counted(monkeypatch):
    """It has been promised nothing yet. Counting it would double-charge the reservation the
    admission ledger is already holding for it."""
    p, _ = _provider([_node("n1", cpu="8")],
                     [_pod(None, _c(cpu="4"))], monkeypatch)
    assert p.budget().free_cpu == pytest.approx(8 - 1)


def test_pods_on_unknown_nodes_are_ignored(monkeypatch):
    p, _ = _provider([_node("n1", cpu="8")], [_pod("elsewhere", _c(cpu="4"))], monkeypatch)
    assert p.budget().free_cpu == pytest.approx(8 - 1)


def test_terminal_pods_are_excluded_server_side(monkeypatch):
    """Asked for in the field selector rather than filtered here: a Succeeded pod still exists
    as an object, and counting them would shrink the cluster by everything that ever ran."""
    seen = {}
    core = types.SimpleNamespace(
        list_node=lambda **kw: types.SimpleNamespace(items=[_node("n1")]),
        list_pod_for_all_namespaces=lambda **kw: seen.update(kw) or types.SimpleNamespace(items=[]))
    monkeypatch.delenv(cluster_capacity.HEADROOM_CPU_ENV, raising=False)
    monkeypatch.delenv(cluster_capacity.HEADROOM_MEMORY_ENV, raising=False)
    ClusterBudgetProvider(lambda: core).budget()
    assert "status.phase!=Succeeded" in seen["field_selector"]
    assert "status.phase!=Failed" in seen["field_selector"]


def test_job_names_are_reported_so_the_ledger_can_stop_double_charging(monkeypatch):
    """counted_jobs is the whole double-counting fix: a reservation stops being subtracted the
    moment the reading starts subtracting the real pod."""
    p, _ = _provider([_node("n1")],
                     [_pod("n1", _c(cpu="1"), job="camp-7"),
                      _pod("n1", _c(cpu="1"))], monkeypatch)
    assert p.budget().counted_jobs == frozenset({"camp-7"})


def test_capacities_answer_could_ever_not_free_now(monkeypatch):
    """A full node still HOLDS its size -- that is what tells 'wait' from 'impossible'.

    Headroom is off both figures (default 1 cpu): it is never spendable, so it is not part of
    what a node "could hold" any more than of what is free on it.
    """
    p, _ = _provider([_node("n1", cpu="8"), _node("n2", cpu="4")],
                     [_pod("n1", _c(cpu="8"))], monkeypatch)
    assert sorted(c.cpu for c in p.capacities()) == [4.0 - 1, 8.0 - 1]


def test_free_never_goes_negative(monkeypatch):
    """An over-committed cluster reports nothing free, not a negative that would read as room
    after the ledger subtracts from it."""
    p, _ = _provider([_node("n1", cpu="2")], [_pod("n1", _c(cpu="8"))], monkeypatch)
    assert p.budget().free_cpu == 0


def test_headroom_is_configurable(monkeypatch):
    p, _ = _provider([_node("n1", cpu="8", memory="16Gi")], [], monkeypatch,
                     env={cluster_capacity.HEADROOM_CPU_ENV: "3",
                          cluster_capacity.HEADROOM_MEMORY_ENV: "1Gi"})
    b = p.budget()
    assert b.free_cpu == pytest.approx(5)
    assert b.free_memory == 15 * 1024 * MIB


def test_an_unparseable_headroom_raises_rather_than_meaning_none(monkeypatch):
    """A typo that silently became 'no headroom' over-admits every node, and the symptom --
    occasional unschedulable pods under load -- points nowhere near the cause."""
    p, _ = _provider([_node("n1")], [], monkeypatch,
                     env={cluster_capacity.HEADROOM_CPU_ENV: "lots"})
    with pytest.raises(ValueError, match="not resource quantities"):
        p.budget()


# -- autoscaling ---------------------------------------------------------------------------

class _Autoscaler:
    """A cluster_config that knows a size the cluster is not currently at."""

    def __init__(self, cpu="64", memory="256Gi", boom=False):
        self.cpu, self.memory, self.boom = cpu, memory, boom

    def get_cluster_allocatable_resources(self, kube_context=None):
        if self.boom:
            raise RuntimeError("gcloud not installed")
        return self.cpu, self.memory


def _with_config(nodes, pods, monkeypatch, config):
    monkeypatch.delenv(cluster_capacity.HEADROOM_CPU_ENV, raising=False)
    monkeypatch.delenv(cluster_capacity.HEADROOM_MEMORY_ENV, raising=False)
    core = types.SimpleNamespace(
        list_node=lambda **kw: types.SimpleNamespace(items=nodes),
        list_pod_for_all_namespaces=lambda **kw: types.SimpleNamespace(items=pods))
    return ClusterBudgetProvider(lambda: core, cluster_config=config)


def test_an_autoscaling_cluster_is_reported_as_growable_not_as_bigger_nodes(monkeypatch):
    """The override says the CLUSTER can grow, not that a node has room it does not have.

    Admission is otherwise self-defeating: pods that cannot be placed are exactly what makes
    an autoscaler add a node, so only ever creating what currently fits keeps the cluster at
    whatever size it happens to be. But per-node budgets cannot express that by inflating a
    node -- there is no node yet, and a pod pinned to a machine that does not exist is worse
    than one left pending. So the extra capacity is a flag, and the controller answers it by
    creating the job UNPINNED and letting kube-scheduler and the autoscaler settle it.
    """
    p = _with_config([_node("n1", cpu="8", memory="16Gi")], [], monkeypatch, _Autoscaler())
    b = p.budget()
    assert b.growable is True
    assert b.free_cpu == pytest.approx(8 - 1), "the real node is reported at its real size"


def test_an_override_never_shrinks_a_cluster_below_its_real_nodes(monkeypatch):
    """An override that under-reports must not take away capacity that demonstrably exists."""
    p = _with_config([_node("n1", cpu="32", memory="64Gi")], [], monkeypatch,
                     _Autoscaler(cpu="8", memory="16Gi"))
    b = p.budget()
    assert b.free_cpu == pytest.approx(32 - 1)
    assert b.growable is False, "an override below the real size does not make it growable"


def test_a_provider_that_cannot_answer_falls_back_to_counting_nodes(monkeypatch):
    """It shells out to gcloud. A cluster that cannot answer should keep admitting against
    the nodes it has, not stop."""
    p = _with_config([_node("n1", cpu="8", memory="16Gi")], [], monkeypatch,
                     _Autoscaler(boom=True))
    b = p.budget()
    assert b.free_cpu == pytest.approx(8 - 1)
    assert b.growable is False


def test_no_cluster_config_is_the_ordinary_case(monkeypatch):
    p = _with_config([_node("n1", cpu="8", memory="16Gi")], [], monkeypatch, None)
    assert p.budget().free_cpu == pytest.approx(8 - 1)


# -- fail loudly rather than inventing capacity --------------------------------------
# Inherited from the quota tests this replaced: the rule there was "never fall back to a
# hard-coded default", and measuring rather than provisioning does not retire it. It moves
# the failure from "a tiny default quota was provisioned" to "nothing can ever be admitted",
# which has to be an error a caller sees rather than a queue that silently never drains.


def test_a_cluster_with_no_allocatable_cpu_refuses_instead_of_admitting(monkeypatch):
    """Zero allocatable CPU must make admission impossible, not merely tight."""
    from robovast.execution.cluster_execution.node_admission import (AdmissionController,
                                                                     AdmissionRefused,
                                                                     JobSizing)

    p, _ = _provider([_node("n1", cpu="0", memory="0")], [], monkeypatch)
    assert p.budget().free_cpu == 0
    # AdmissionRefused here, not CampaignConfigError: the controller states the fact and the
    # backend seam is what turns a permanent refusal into the campaign-facing error.
    with pytest.raises(AdmissionRefused, match="no node is that large"):
        AdmissionController(p).preflight(JobSizing(cpu=1.0, memory=MIB, gpu=0))


def test_a_failed_node_query_propagates_rather_than_reading_as_an_empty_cluster(monkeypatch):
    """The two must never look alike. An empty cluster is "admit nothing"; an unreadable one
    is "we do not know", and the run loop is what decides how long to keep asking -- so this
    has to raise rather than return a budget of zero."""
    def _boom(**_kw):
        raise RuntimeError("api unreachable")

    core = types.SimpleNamespace(list_node=_boom,
                                 list_pod_for_all_namespaces=lambda **kw: None)
    monkeypatch.delenv(cluster_capacity.HEADROOM_CPU_ENV, raising=False)
    monkeypatch.delenv(cluster_capacity.HEADROOM_MEMORY_ENV, raising=False)
    with pytest.raises(RuntimeError, match="api unreachable"):
        ClusterBudgetProvider(lambda: core).budget()


# -- a node that cannot take work -----------------------------------------------------------

def test_a_dead_node_is_not_free_capacity(monkeypatch):
    """The reading that used to discard runs in a loop.

    A node that dies loses its pods once the eviction timeout passes, so nothing is committed
    against it any more and it read as FULLY free -- the most attractive node in the cluster.
    Admission pinned job after job to it; each was refused for an untolerated `not-ready`
    taint, which is correctly a fault rather than contention, so each was dropped on the short
    grace window. For as long as the node stayed down.
    """
    p, _ = _provider([_node("n1", cpu="8"),
                      _node("n2", cpu="8", ready=False,
                            taints=[_taint("node.kubernetes.io/not-ready", None,
                                           "NoSchedule")])],
                     [], monkeypatch)
    b = p.budget()
    assert [n.node_id for n in b.nodes] == ["node-n1"]
    assert b.free_cpu == pytest.approx(8 - 1), "only the live node's cores are spendable"


def test_a_cordoned_node_is_not_free_capacity(monkeypatch):
    """An operator draining a node for maintenance must not have work pinned onto it."""
    p, _ = _provider([_node("n1", cpu="8"), _node("n2", cpu="8", cordoned=True)],
                     [], monkeypatch)
    assert [n.node_id for n in p.budget().nodes] == ["node-n1"]


def test_a_node_tainted_against_campaign_pods_is_not_free_capacity(monkeypatch):
    """A taint the job pods do not carry a toleration for makes the node unusable to them."""
    p, _ = _provider([_node("n1", cpu="8"),
                      _node("n2", cpu="8", taints=[_taint("reserved", "someone-else")])],
                     [], monkeypatch)
    assert [n.node_id for n in p.budget().nodes] == ["node-n1"]


def test_the_campaign_taint_itself_is_still_counted(monkeypatch):
    """`dedicated=batch:NoSchedule` is the taint job pods DO tolerate.

    Filtering it out would empty the budget on exactly the clusters that dedicate nodes to
    campaigns -- the deployment the toleration exists for.
    """
    from robovast.execution.cluster_execution.node_placement import (
        CAMPAIGN_NODE_TOLERATIONS)

    tol = CAMPAIGN_NODE_TOLERATIONS[0]
    p, _ = _provider([_node("n1", cpu="8",
                            taints=[_taint(tol["key"], tol["value"], tol["effect"])])],
                     [], monkeypatch)
    assert [n.node_id for n in p.budget().nodes] == ["node-n1"]


def test_could_ever_still_counts_a_node_that_is_merely_down(monkeypatch):
    """`capacities()` answers "ever", and preflight raises PERMANENTLY on its answer.

    A cordoned or rebooting node is coming back, so excluding it here would turn a maintenance
    window into a campaign that refuses to start rather than one that waits -- swapping one
    failure for a worse one.
    """
    p, _ = _provider([_node("n1", cpu="4"), _node("n2", cpu="16", cordoned=True)],
                     [], monkeypatch)
    assert sorted(c.cpu for c in p.capacities()) == [4.0 - 1, 16.0 - 1]
    assert [n.node_id for n in p.budget().nodes] == ["node-n1"]


def test_preflight_and_drain_agree_about_headroom(monkeypatch):
    """The gap between "could ever" and "free now" was a silent forever-wait.

    A job needing 7.5 cores on an 8-core node passed preflight against the raw allocatable and
    then fit no node once the 1-core reserve came off -- so the campaign waited for capacity
    that could not exist, having created nothing, with no error anywhere. That is the exact
    failure preflight was written to raise on.
    """
    from robovast.execution.cluster_execution.node_admission import (
        AdmissionController, AdmissionRefused, JobSizing)

    p, _ = _provider([_node("n1", cpu="8", memory="16Gi")], [], monkeypatch)
    queue = AdmissionController(p)
    oversized = JobSizing(cpu=7.5, memory=1 * MIB)

    with pytest.raises(AdmissionRefused):
        queue.preflight(oversized)

    # And the two agree the other way: what preflight admits, a drain can place.
    fits = JobSizing(cpu=7.0, memory=1 * MIB)
    queue.preflight(fits)
    created = []
    queue.submit("c", [("j", fits, lambda node_id: created.append(node_id))],
                 started_at=0.0)
    assert queue.drain() == 1 and created == ["node-n1"]


def test_growable_is_judged_against_the_cluster_not_the_job_pool(monkeypatch):
    """A node pool is not evidence that the cluster can grow.

    `_allocatables` is filtered to the pool; the autoscaler's declared max covers every node
    it may create. Comparing them measured different sets, so configuring a job node pool made
    `growable` permanently true -- and a growable cluster creates jobs UNPINNED, bypassing the
    per-node accounting, and switches per-node sizing off through `calibration_applies`.
    """
    nodes = [_node("n1", cpu="8"), _node("n2", cpu="8"), _node("n3", cpu="8")]
    nodes[0].metadata.labels["pool"] = "campaigns"

    monkeypatch.delenv(cluster_capacity.HEADROOM_CPU_ENV, raising=False)
    monkeypatch.delenv(cluster_capacity.HEADROOM_MEMORY_ENV, raising=False)

    def list_node(**kw):
        selector = kw.get("label_selector")
        if not selector:
            return types.SimpleNamespace(items=nodes)
        key, value = selector.split("=", 1)
        return types.SimpleNamespace(
            items=[n for n in nodes if (n.metadata.labels or {}).get(key) == value])

    core = types.SimpleNamespace(
        list_node=list_node,
        list_pod_for_all_namespaces=lambda **kw: types.SimpleNamespace(items=[]))
    p = ClusterBudgetProvider(lambda: core, node_selector={"pool": "campaigns"},
                              cluster_config=_Autoscaler(cpu="24", memory="48Gi"))

    b = p.budget()
    assert [n.node_id for n in b.nodes] == ["node-n1"], "only the pool is spendable"
    assert b.growable is False, (
        "24 declared vs 24 real cores across the cluster: the pool being smaller is a "
        "confinement, not headroom an autoscaler will supply")


def test_a_cordoned_node_does_not_make_a_cluster_look_growable(monkeypatch):
    """A node that is down is coming back; it is not a machine the autoscaler must add."""
    p = _with_config([_node("n1", cpu="8"), _node("n2", cpu="8", cordoned=True)],
                     [], monkeypatch, _Autoscaler(cpu="16", memory="32Gi"))
    b = p.budget()
    assert [n.node_id for n in b.nodes] == ["node-n1"]
    assert b.growable is False
