# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Jobs submitted under one owner, created as room appears, tracked round by round."""

import types

from robovast.execution.cluster_execution import admitted_jobs as aj
from robovast.execution.cluster_execution.node_admission import (AdmissionController, Budget,
                                                                 Capacity, JobSizing,
                                                                 NodeBudget)

MIB = 1024 * 1024


class _Provider:
    """One node holding everything, with *cpu* cores free."""

    def __init__(self, cpu):
        self.cpu = cpu

    def budget(self):
        return Budget(nodes=(NodeBudget("n1", self.cpu, 1024 * MIB),))

    def capacities(self):
        return [Capacity(64.0, 64 * 1024 * MIB)]


def _job(name, active=None, completed=False, failed=None):
    status = types.SimpleNamespace(active=active, failed=failed,
                                   completion_time="t" if completed else None)
    return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name), status=status)


class _Batch:
    def __init__(self, jobs):
        self.jobs = jobs
        self.selectors = []

    def list_namespaced_job(self, namespace, label_selector):
        self.selectors.append(label_selector)
        return types.SimpleNamespace(items=self.jobs)


def _tracker(admission=None, remaining=None, blocked=None, clock=None, **kw):
    tracker = aj.AdmittedJobs(admission=admission, owner="camp", batch_api=None, core_api=None,
                              namespace="ns", label_selector="jobgroup=g",
                              list_remaining=remaining or (lambda names: []),
                              clock=clock, **kw)
    state = {"blocked": blocked if blocked is not None else ({}, {})}
    tracker._blocked = lambda created: (  # pylint: disable=protected-access
        (None, {}, "unreadable") if state["blocked"] is None
        else ({k: v for k, v in state["blocked"][0].items() if k in created},
              dict(state["blocked"][1]), ""))
    return tracker, state


def test_without_a_queue_every_job_is_created_at_once_and_unpinned():
    made = []
    tracker, _ = _tracker(remaining=lambda names: list(names))
    tracker.submit([("a", None, lambda node_id=None: made.append(("a", node_id))),
                    ("b", None, lambda node_id=None: made.append(("b", node_id)))])
    rnd = tracker.poll()
    assert made == [("a", None), ("b", None)]
    assert rnd.created == ["a", "b"] and rnd.remaining == ["a", "b"] and not rnd.over


def test_the_queue_creates_what_has_room_and_the_rest_stays_planned():
    queue = AdmissionController(_Provider(cpu=2.0), clock=lambda: 0.0)
    made = []
    tracker, _ = _tracker(queue, remaining=lambda names: list(names))
    tracker.submit([(f"j{i}", JobSizing(2.0, MIB), lambda node_id=None, i=i: made.append(i))
                    for i in range(3)], started_at=0.0)
    rnd = tracker.poll()
    assert made == [0] and rnd.created == ["j0"] and rnd.planned == 2 and not rnd.over


def test_a_finished_job_releases_its_room_for_the_next():
    queue = AdmissionController(_Provider(cpu=2.0), clock=lambda: 0.0)
    running = {"j0"}
    tracker, _ = _tracker(queue, remaining=lambda names: [n for n in names if n in running])
    tracker.submit([(f"j{i}", JobSizing(2.0, MIB), lambda node_id=None: None)
                    for i in range(2)], started_at=0.0)
    assert tracker.poll().created == ["j0"]
    running.clear()
    rnd = tracker.poll()
    assert rnd.done == ["j0"]
    running.add("j1")
    rnd = tracker.poll()
    assert "j1" in rnd.created and rnd.planned == 0


def test_the_round_is_over_only_when_nothing_runs_and_nothing_is_planned():
    tracker, _ = _tracker()
    tracker.submit([("a", None, lambda node_id=None: None)])
    rnd = tracker.poll()
    assert rnd.done == ["a"] and rnd.over


def test_a_blocked_job_expires_after_its_grace_and_a_contended_one_waits_longer():
    now = [0.0]
    tracker, state = _tracker(remaining=lambda names: list(names), clock=lambda: now[0],
                              blocked_grace=10, contended_grace=100)
    tracker.submit([("a", None, lambda node_id=None: None), ("b", None, lambda node_id=None: None)])
    state["blocked"] = ({"a": "ErrImagePull", "b": "Unschedulable"}, {"b": "Unschedulable"})
    rnd = tracker.poll()
    assert sorted(rnd.fresh) == ["a", "b"] and rnd.expired == []
    now[0] = 11.0
    rnd = tracker.poll()
    assert rnd.fresh == [] and rnd.expired == ["a"]
    now[0] = 101.0
    assert sorted(tracker.poll().expired) == ["a", "b"]
    assert tracker.poll(ignore_blocked={"a"}).expired == ["b"]


def test_an_unreadable_round_keeps_every_timer():
    now = [0.0]
    tracker, state = _tracker(remaining=lambda names: list(names), clock=lambda: now[0],
                              blocked_grace=10)
    tracker.submit([("a", None, lambda node_id=None: None)])
    state["blocked"] = ({"a": "ErrImagePull"}, {})
    tracker.poll()
    state["blocked"] = None
    now[0] = 50.0
    rnd = tracker.poll()
    assert rnd.blocked is None and rnd.blocked_error == "unreadable"
    assert tracker.blocked_since == {"a": 0.0}


def test_a_job_that_started_after_all_clears_its_timer():
    tracker, state = _tracker(remaining=lambda names: list(names), clock=lambda: 0.0)
    tracker.submit([("a", None, lambda node_id=None: None)])
    state["blocked"] = ({"a": "ErrImagePull"}, {})
    tracker.poll()
    state["blocked"] = ({}, {})
    tracker.poll()
    assert tracker.blocked_since == {}


def test_running_jobs_reads_one_listing_and_counts_the_absent_as_finished():
    batch = _Batch([_job("run", active=1), _job("pending"), _job("ok", completed=True),
                    _job("bad", failed=1), _job("other", active=1)])
    seen = []
    running = aj.running_jobs(batch, "ns", "jobgroup=g", ["run", "pending", "ok", "bad", "gone"],
                              on_status=lambda name, status: seen.append(name))
    assert running == ["run", "pending"]
    assert batch.selectors == ["jobgroup=g"]
    assert "other" not in seen and "gone" not in seen


def test_no_names_asks_nothing():
    batch = _Batch([])
    assert not aj.running_jobs(batch, "ns", "jobgroup=g", [])
    assert not batch.selectors
