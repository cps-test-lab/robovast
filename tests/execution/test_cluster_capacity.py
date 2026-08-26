# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The capacity reading behind admission, against fake Kubernetes objects.

Follows tests/execution/test_kueue_quota_headroom.py: SimpleNamespace graphs shaped like the
client's model objects, no cluster.
"""

import types

import pytest

from robovast.execution.cluster_execution import cluster_capacity
from robovast.execution.cluster_execution.cluster_capacity import ClusterBudgetProvider

MiB = 1024 ** 2


def _node(name, cpu="8", memory="16Gi", gpu=None):
    alloc = {"cpu": cpu, "memory": memory}
    if gpu:
        alloc["nvidia.com/gpu"] = gpu
    return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name),
                                 status=types.SimpleNamespace(allocatable=alloc))


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
    assert b.free_memory == (16 - 4) * 1024 * MiB - 2 * 1024 * MiB   # default 2Gi


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
    """A full node still HOLDS its size -- that is what tells 'wait' from 'impossible'."""
    p, _ = _provider([_node("n1", cpu="8"), _node("n2", cpu="4")],
                     [_pod("n1", _c(cpu="8"))], monkeypatch)
    assert sorted(c.cpu for c in p.capacities()) == [4.0, 8.0]


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
    assert b.free_memory == 15 * 1024 * MiB


def test_an_unparseable_headroom_raises_rather_than_meaning_none(monkeypatch):
    """A typo that silently became 'no headroom' over-admits every node, and the symptom --
    occasional unschedulable pods under load -- points nowhere near the cause."""
    p, _ = _provider([_node("n1")], [], monkeypatch,
                     env={cluster_capacity.HEADROOM_CPU_ENV: "lots"})
    with pytest.raises(ValueError, match="not resource quantities"):
        p.budget()
