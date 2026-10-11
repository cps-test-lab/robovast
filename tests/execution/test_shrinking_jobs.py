# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Units of work in Jobs that shrink a level when one is OOM-killed, and never grow."""

import types

import pytest

from robovast.execution.cluster_execution.node_admission import (AdmissionController, Budget,
                                                                 Capacity, JobSizing,
                                                                 NodeBudget)
from robovast.execution.cluster_execution.shrinking_jobs import (Item, ShrinkingFailed,
                                                                 ShrinkingJobs)

MIB = 1024 * 1024


class _Provider:
    def __init__(self, cpu):
        self.cpu = cpu

    def budget(self):
        return Budget(nodes=(NodeBudget("n1", self.cpu, 1024 * MIB),))

    def capacities(self):
        return [Capacity(64.0, 64 * 1024 * MIB)]


class _Cluster:
    """Jobs that end on their first listing, as *outcome(units, level)* says."""

    def __init__(self, outcome, blocked=None, wedged=()):
        self.outcome = outcome
        self.wedged = set(wedged)   # jobs whose pod waits behind an OOM-killed init step
        self.jobs = {}              # name -> status
        self.created = []           # (name, level, units)
        self.deleted = []
        self.oom = set()
        self.blocked = blocked or {}

    def create(self, name, level, units):
        self.created.append((name, level, list(units)))
        result = self.outcome(list(units), level)
        if result == "oom":
            self.oom.add(name)
        self.jobs[name] = types.SimpleNamespace(
            active=None, succeeded=1 if result == "succeeded" else None,
            failed=1 if result in ("oom", "failed") else None,
            completion_time="t" if result == "succeeded" else None)
        if result == "running":
            self.jobs[name].active = 1

    # the batch API
    def list_namespaced_job(self, namespace, label_selector):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(metadata=types.SimpleNamespace(name=n), status=s)
            for n, s in self.jobs.items()])

    def delete_namespaced_job(self, name, namespace, propagation_policy):
        self.deleted.append(name)
        self.jobs.pop(name, None)

    # the core API: what blocked_and_contended_reasons reads
    def list_namespaced_pod(self, namespace, label_selector=None, **_kw):
        pods = []
        for name, reason in self.blocked.items():
            pods.append(types.SimpleNamespace(
                metadata=types.SimpleNamespace(name=f"{name}-pod", labels={"job-name": name},
                                               owner_references=None),
                status=types.SimpleNamespace(
                    phase="Pending", container_statuses=None, init_container_statuses=None,
                    conditions=[types.SimpleNamespace(type="PodScheduled", status="False",
                                                      reason="Unschedulable",
                                                      message=reason)])))
        for name in self.wedged & set(self.jobs):
            stage = types.SimpleNamespace(name="stage", state=types.SimpleNamespace(
                waiting=None, running=None,
                terminated=types.SimpleNamespace(reason="OOMKilled", exit_code=0)))
            pods.append(types.SimpleNamespace(
                metadata=types.SimpleNamespace(name=f"{name}-pod", labels={"job-name": name},
                                               owner_references=None),
                spec=types.SimpleNamespace(node_name="n1"),
                status=types.SimpleNamespace(phase="Pending", init_container_statuses=[stage],
                                             container_statuses=None, conditions=None,
                                             start_time=None, reason=None)))
        return types.SimpleNamespace(items=pods)


def _work(cluster, admission=None, per_job=4, clock=None):
    def make_items(units, level):
        size = max(1, per_job >> level)
        items = []
        for i in range(0, len(units), size):
            chunk = units[i:i + size]
            name = f"t-g{level}-{i // size}"
            items.append(Item(name, JobSizing(1.0, MIB),
                              lambda node_id=None, n=name, c=chunk, lv=level:
                              cluster.create(n, lv, c), chunk))
        return items

    return ShrinkingJobs(admission=admission, owner_prefix="camp#tables", batch_api=cluster,
                         core_api=cluster, namespace="ns", label_selector="jobgroup=tables",
                         make_items=make_items,
                         at_floor=lambda item, level: len(item.units) == 1,
                         submit_kwargs={"started_at": 0.0} if admission else {},
                         sleep=lambda s: None,
                         find_oom=lambda names: {n for n in names if n in cluster.oom},
                         clock=clock)


def test_every_unit_is_delivered_when_nothing_runs_out_of_memory():
    cluster = _Cluster(lambda units, level: "succeeded")
    work = _work(cluster)
    delivered = work.run(list(range(10)))
    assert sorted(u for units in delivered.values() for u in units) == list(range(10))
    assert {level for _, level, _ in cluster.created} == {0} and not work.shrinks


def test_a_kill_takes_its_level_back_and_the_rest_goes_on_smaller():
    # Level 0 holds four units a job; a job holding unit 5 is killed there. What was
    # delivered stays delivered; everything else is done again two units a job.
    cluster = _Cluster(lambda units, level: "oom" if level == 0 and 5 in units else
                       "running" if level == 0 and 9 in units else "succeeded")
    work = _work(cluster)
    delivered = work.run(list(range(10)))
    assert sorted(u for units in delivered.values() for u in units) == list(range(10))
    level0 = {n: u for n, lv, u in cluster.created if lv == 0}
    assert delivered["t-g0-0"] == [0, 1, 2, 3]
    assert "t-g0-2" in cluster.deleted                       # the running one, taken back
    level1 = [u for _, lv, u in cluster.created if lv == 1]
    assert sorted(x for u in level1 for x in u) == [4, 5, 6, 7, 8, 9]
    assert all(len(u) <= 2 for u in level1)
    assert len(work.shrinks) == 1 and "t-g0-1" in work.shrinks[0]
    assert set(level0) == {"t-g0-0", "t-g0-1", "t-g0-2"}


def test_a_level_taken_back_creates_nothing_more_of_it():
    # One job fits at a time: the queue holds the rest of level 0 when its first job is
    # killed, and none of them is ever created.
    queue = AdmissionController(_Provider(cpu=1.0), clock=lambda: 0.0)
    cluster = _Cluster(lambda units, level: "oom" if level == 0 else "succeeded")
    work = _work(cluster, admission=queue)
    delivered = work.run(list(range(12)))
    assert [n for n, lv, _ in cluster.created if lv == 0] == ["t-g0-0"]
    assert sorted(u for units in delivered.values() for u in units) == list(range(12))


def test_a_kill_at_the_floor_fails_naming_the_unit():
    cluster = _Cluster(lambda units, level: "oom" if 3 in units else "succeeded")
    work = _work(cluster, per_job=2)
    with pytest.raises(ShrinkingFailed, match="3 ran out of memory at the smallest size"):
        work.run(list(range(4)))
    assert max(lv for _, lv, _ in cluster.created) == 1


def test_another_failure_is_not_retried():
    cluster = _Cluster(lambda units, level: "failed" if 2 in units else "succeeded")
    work = _work(cluster)
    with pytest.raises(ShrinkingFailed, match="failed"):
        work.run(list(range(8)))
    assert {lv for _, lv, _ in cluster.created} == {0}


def test_a_pod_stuck_behind_an_oom_killed_init_step_fails_the_work_and_is_deleted():
    # The kubelet starts nothing after an init container it recorded OOMKilled, and the pod
    # stays Pending with its Job active: past the blocked grace it is a pod that cannot
    # start, not a Job still working.
    cluster = _Cluster(lambda units, level: "running" if 5 in units else "succeeded",
                       wedged={"t-g0-1"})
    now = [0.0]

    def sleep(_seconds):
        now[0] += 30.0

    work = _work(cluster, clock=lambda: now[0])
    work.sleep = sleep
    with pytest.raises(ShrinkingFailed, match="t-g0-1: OOMKilled: init container stage"):
        work.run(list(range(8)))
    assert "t-g0-1" in cluster.deleted
    assert {lv for _, lv, _ in cluster.created} == {0}
