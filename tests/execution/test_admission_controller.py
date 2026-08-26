# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The admission queue, on its own: no Kubernetes, no BatchJobRunner, no cluster.

Two of these tests are the reason the module is shaped the way it is. ``test_two_grants...``
pins the in-flight ledger, without which a second job is handed cores the first already took
but whose pod is not yet visible. ``test_an_older_campaigns_second_batch...`` pins the
ordering, and is the regression guard for a bug the codebase already fixed once in Kueue's
priority class: order by submission and an older campaign's later batches lose to a younger
campaign, so the two take turns instead of the older one finishing.
"""

import pytest

from robovast.execution.cluster_execution.node_admission import (AdmissionController,
                                                                 AdmissionRefused, Budget,
                                                                 Capacity, JobSizing)

MiB = 1024 ** 2


class FakeProvider:
    """A budget that only changes when a test says so."""

    def __init__(self, cpu=10.0, memory=10240 * MiB, gpu=0, nodes=None):
        self.free = Budget(free_cpu=cpu, free_memory=memory, free_gpu=gpu)
        self.nodes = nodes if nodes is not None else [Capacity(8.0, 8192 * MiB)]
        self.reads = 0

    def budget(self):
        self.reads += 1
        return self.free

    def capacities(self):
        return self.nodes

    def observe(self, *keys):
        """The pod pass now sees these Jobs -- i.e. their requests are in the reading."""
        self.free = Budget(self.free.free_cpu, self.free.free_memory, self.free.free_gpu,
                           frozenset(keys))


def _items(controller, owner, n, cpu=2.0, memory=1024 * MiB, started_at=0.0, priority=0,
           created=None):
    made = created if created is not None else []
    controller.submit(owner, [(f"{owner}-{i}", JobSizing(cpu, memory),
                               (lambda k=f"{owner}-{i}": made.append(k)))
                              for i in range(n)],
                      started_at=started_at, priority=priority)
    return made


def _controller(provider=None, **kw):
    return AdmissionController(provider or FakeProvider(), clock=lambda: 0.0, **kw)


# -- the ledger ---------------------------------------------------------------------------

def test_two_grants_subtract_cumulatively_before_any_pod_is_observed():
    """The hazard the ledger exists for. A Job created a moment ago has no pod bound, so the
    next reading does not see its requests -- and handing the same cores out again is the
    over-admission a stale quota produces."""
    p = FakeProvider(cpu=5.0)          # room for two 2-core jobs, not three
    c = _controller(p)
    made = _items(c, "a", 3, cpu=2.0)
    assert c.drain() == 2, "third job must not be handed cores the first two hold"
    assert made == ["a-0", "a-1"]
    assert c.drain() == 0, "still no room, and the reading still cannot see either pod"


def test_a_reservation_stops_being_charged_once_its_pod_is_observed():
    """And free capacity must not jump when it does -- the reading now carries what the ledger
    was standing in for."""
    p = FakeProvider(cpu=5.0)
    c = _controller(p)
    _items(c, "a", 3, cpu=2.0)
    c.drain()
    p.observe("a-0", "a-1")            # the pod pass now subtracts them itself
    p.free = Budget(free_cpu=1.0, free_memory=10240 * MiB,
                    counted_jobs=frozenset({"a-0", "a-1"}))
    assert c.drain() == 0, "1 core free is still not 2 -- no double credit"


def test_finished_returns_capacity():
    p = FakeProvider(cpu=5.0)
    c = _controller(p)
    made = _items(c, "a", 3, cpu=2.0)
    c.drain()
    c.finished("a-0")
    assert c.drain() == 1 and made[-1] == "a-2"


# -- ordering -----------------------------------------------------------------------------

def test_the_oldest_campaign_goes_first_whatever_the_submission_order():
    p = FakeProvider(cpu=2.0)          # one job at a time
    c = _controller(p)
    made = []
    _items(c, "young", 1, started_at=200.0, created=made)
    _items(c, "old", 1, started_at=100.0, created=made)
    c.drain()
    assert made == ["old-0"]


def test_an_older_campaigns_second_batch_still_beats_a_younger_campaigns_first():
    """The regression guard. A search submits batch after batch; ordering by submission makes
    the older campaign's later batches look younger, and the two take turns instead of the
    older one finishing."""
    p = FakeProvider(cpu=2.0)
    c = _controller(p)
    made = []
    c.submit("young", [("young-0", JobSizing(2.0, MiB), lambda: made.append("young-0"))],
             started_at=200.0)
    # The older campaign's SECOND batch, enqueued after the younger campaign's first.
    c.submit("old", [("old-1", JobSizing(2.0, MiB), lambda: made.append("old-1"))],
             started_at=100.0)
    c.drain()
    assert made == ["old-1"]


def test_priority_outranks_age():
    p = FakeProvider(cpu=2.0)
    c = _controller(p)
    made = []
    _items(c, "old", 1, started_at=100.0, created=made)
    _items(c, "urgent", 1, started_at=900.0, priority=10, created=made)
    c.drain()
    assert made == ["urgent-0"]


def test_a_job_that_does_not_fit_is_skipped_not_blocked_behind():
    """A large job must not hold the cluster idle while smaller ones could run."""
    p = FakeProvider(cpu=3.0)
    c = _controller(p)
    made = []
    c.submit("a", [("big", JobSizing(8.0, MiB), lambda: made.append("big")),
                   ("small", JobSizing(2.0, MiB), lambda: made.append("small"))],
             started_at=0.0)
    assert c.drain() == 1 and made == ["small"]


# -- refusal vs impossibility -------------------------------------------------------------

def test_no_room_now_is_an_ordinary_answer():
    c = _controller(FakeProvider(cpu=0.5))
    _items(c, "a", 1, cpu=2.0)
    assert c.drain() == 0
    assert "waiting" in c.refusal()


def test_a_job_no_node_could_ever_run_raises_instead():
    """Otherwise the campaign waits forever having created zero jobs, and every diagnosis path
    downstream is pod-based and so cannot see it."""
    c = _controller(FakeProvider(nodes=[Capacity(4.0, 4096 * MiB)]))
    with pytest.raises(AdmissionRefused, match="no node is that large"):
        c.preflight(JobSizing(9.0, MiB))
    c.preflight(JobSizing(4.0, MiB))          # exactly fits: allowed


def test_a_cluster_with_no_nodes_raises_rather_than_reporting_busy():
    c = _controller(FakeProvider(nodes=[]))
    with pytest.raises(AdmissionRefused, match="no nodes"):
        c.preflight(JobSizing(1.0, MiB))


# -- ownership ----------------------------------------------------------------------------

def test_cancel_drops_one_owners_work_and_not_anothers():
    p = FakeProvider(cpu=4.0)
    c = _controller(p)
    _items(c, "a", 2, cpu=2.0)
    _items(c, "b", 2, cpu=2.0, started_at=50.0)
    c.drain()
    assert c.cancel("a") >= 1
    assert set(c.states("a")) == set()
    assert set(c.states("b"))


def test_a_create_that_raises_leaves_the_job_planned_and_holds_no_reservation():
    """The caller owns the failure. A held reservation for a job that does not exist would
    shrink capacity for the life of the process."""
    p = FakeProvider(cpu=10.0)
    c = _controller(p)

    def boom():
        raise RuntimeError("api said no")

    c.submit("a", [("a-0", JobSizing(2.0, MiB), boom)], started_at=0.0)
    assert c.drain() == 0
    assert c.states("a") == {"a-0": "planned"}
    ok = []
    c.submit("a", [("a-1", JobSizing(2.0, MiB), lambda: ok.append(1))], started_at=0.0)
    assert c.drain() == 1, "the failure must not have consumed capacity"


def test_resubmitting_a_plan_does_not_double_it():
    c = _controller(FakeProvider(cpu=100.0))
    made = []
    for _ in range(2):
        c.submit("a", [("a-0", JobSizing(1.0, MiB), lambda: made.append("a-0"))],
                 started_at=0.0)
    assert c.drain() == 1 and made == ["a-0"]
