# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The admission queue, on its own: no Kubernetes, no BatchJobRunner, no cluster.

Two of these tests are the reason the module is shaped the way it is. ``test_two_grants...``
pins the in-flight ledger, without which a second job is handed cores the first already took
but whose pod is not yet visible. ``test_an_older_campaigns_second_batch...`` pins the
ordering, and is the regression guard for a bug this codebase has already had once in its
priority class: order by submission and an older campaign's later batches lose to a younger
campaign, so the two take turns instead of the older one finishing.
"""

import pytest

from robovast.execution.cluster_execution import node_admission
from robovast.execution.cluster_execution.node_admission import (AdmissionController,
                                                                 AdmissionRefused, Budget,
                                                                 Capacity, JobSizing,
                                                                 NodeBudget)

MIB = 1024 ** 2


class FakeProvider:
    """A budget that only changes when a test says so.

    ``cpu``/``memory`` describe ONE node by default, which is what most of these tests want:
    they are about the ledger and the ordering, not about placement. Pass ``per_node`` for a
    cluster of several.
    """

    def __init__(self, cpu=10.0, memory=10240 * MIB, gpu=0, nodes=None, per_node=None,
                 growable=False):
        if per_node is None:
            per_node = [("n1", cpu, memory, gpu)]
        self._per_node = per_node
        self.growable = growable
        self.free = self._budget(frozenset())
        self.nodes = nodes if nodes is not None else [Capacity(8.0, 8192 * MIB)]
        self.reads = 0

    def _budget(self, counted):
        return Budget(nodes=tuple(NodeBudget(node_id=i, free_cpu=c, free_memory=m, free_gpu=g)
                                  for i, c, m, g in self._per_node),
                      counted_jobs=counted, growable=self.growable)

    def budget(self):
        self.reads += 1
        return self.free

    def capacities(self):
        return self.nodes

    def observe(self, *keys):
        """The pod pass now sees these Jobs -- i.e. their requests are in the reading."""
        self.free = Budget(self.free.nodes, frozenset(keys), self.growable)


def _items(controller, owner, n, cpu=2.0, memory=1024 * MIB, started_at=0.0, priority=0,
           created=None):
    made = created if created is not None else []
    controller.submit(owner, [(f"{owner}-{i}", JobSizing(cpu, memory),
                               (lambda _node=None, k=f"{owner}-{i}": made.append(k)))
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
    p.free = Budget(nodes=(NodeBudget("n1", 1.0, 10240 * MIB),),
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
    c.submit("young", [("young-0", JobSizing(2.0, MIB), lambda _n=None: made.append("young-0"))],
             started_at=200.0)
    # The older campaign's SECOND batch, enqueued after the younger campaign's first.
    c.submit("old", [("old-1", JobSizing(2.0, MIB), lambda _n=None: made.append("old-1"))],
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
    c.submit("a", [("big", JobSizing(8.0, MIB), lambda _n=None: made.append("big")),
                   ("small", JobSizing(2.0, MIB), lambda _n=None: made.append("small"))],
             started_at=0.0)
    assert c.drain() == 1 and made == ["small"]


# -- refusal vs impossibility -------------------------------------------------------------

def test_no_room_now_is_an_ordinary_answer():
    c = _controller(FakeProvider(cpu=0.5))
    _items(c, "a", 1, cpu=2.0)
    assert c.drain() == 0
    assert "waiting" in c.refusal("a")


def test_a_job_no_node_could_ever_run_raises_instead():
    """Otherwise the campaign waits forever having created zero jobs, and every diagnosis path
    downstream is pod-based and so cannot see it."""
    c = _controller(FakeProvider(nodes=[Capacity(4.0, 4096 * MIB)]))
    with pytest.raises(AdmissionRefused, match="no node is that large"):
        c.preflight(JobSizing(9.0, MIB))
    c.preflight(JobSizing(4.0, MIB))          # exactly fits: allowed


def test_a_cluster_with_no_nodes_raises_rather_than_reporting_busy():
    c = _controller(FakeProvider(nodes=[]))
    with pytest.raises(AdmissionRefused, match="no nodes"):
        c.preflight(JobSizing(1.0, MIB))


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

    c.submit("a", [("a-0", JobSizing(2.0, MIB), boom)], started_at=0.0)
    assert c.drain() == 0
    assert c.states("a") == {"a-0": "planned"}
    ok = []
    c.submit("a", [("a-1", JobSizing(2.0, MIB), lambda _n=None: ok.append(1))], started_at=0.0)
    assert c.drain() == 1, "the failure must not have consumed capacity"


def test_resubmitting_a_plan_does_not_double_it():
    c = _controller(FakeProvider(cpu=100.0))
    made = []
    for _ in range(2):
        c.submit("a", [("a-0", JobSizing(1.0, MIB), lambda _n=None: made.append("a-0"))],
                 started_at=0.0)
    assert c.drain() == 1 and made == ["a-0"]


# -- per-node placement -------------------------------------------------------------------

def test_a_job_is_pinned_to_a_node_that_can_hold_it():
    """The reservation and the placement are one decision. Admitting against a cluster total
    and letting the scheduler choose is what allows a job to be created and then sit
    Unschedulable, which is exactly what a per-node budget removes."""
    p = FakeProvider(per_node=[("small", 2.0, 10240 * MIB, 0),
                               ("big", 8.0, 10240 * MIB, 0)])
    c = _controller(p)
    seen = []
    c.submit("a", [("a-0", JobSizing(4.0, MIB), lambda n=None: seen.append(n))],
             started_at=0.0)
    assert c.drain() == 1
    assert seen == ["big"], "only one node could hold it"


def test_fragmentation_is_refused_rather_than_admitted_and_left_pending():
    """The failure this exists for: free cores spread across nodes and no single node
    holding the 4.75 a pod needed. A cluster-wide figure says yes and the scheduler then
    cannot place it."""
    p = FakeProvider(per_node=[("a", 3.0, 10240 * MIB, 0), ("b", 3.0, 10240 * MIB, 0),
                               ("c", 3.0, 10240 * MIB, 0), ("d", 3.0, 10240 * MIB, 0)])
    c = _controller(p)
    made = _items(c, "a", 1, cpu=4.0)
    assert p.budget().free_cpu == 12.0, "cluster-wide there is plenty"
    assert c.drain() == 0 and made == []
    assert "no node has that free" in c.refusal("a")


def test_the_ledger_charges_the_node_the_job_was_granted_on():
    """A job promised room on one machine must not appear to free capacity on another --
    otherwise two jobs are handed the same cores whenever the cluster has more than one node.
    """
    p = FakeProvider(per_node=[("a", 5.0, 10240 * MIB, 0), ("b", 1.0, 10240 * MIB, 0)])
    c = _controller(p)
    made = _items(c, "a", 3, cpu=3.0)
    # Only node "a" can hold a 3-core job, and only once: 5 - 3 = 2.
    assert c.drain() == 1 and len(made) == 1


def test_a_node_without_an_identity_label_takes_work_unpinned():
    """A node that joined since the last setup, or a whole cluster upgraded without re-running
    it. Its pods and capacity are real, so it is counted AND usable -- it simply cannot be
    named in a selector, so the job is created unpinned and the scheduler places it.

    This was the other way round once, and it was a hang waiting to happen: excluding
    unlabelled nodes left a cluster whose nodes predate the label with NO candidates at all,
    so nothing was ever admitted and every campaign waited forever reporting "queued for
    capacity" on an idle cluster. Observed exactly that way on a real deployment. A missing
    label costs the pin, never the run.
    """
    c = _controller(FakeProvider(
        per_node=[(None, 100.0, 10240 * MIB, 0), ("named", 5.0, 10240 * MIB, 0)]))
    seen = []
    c.submit("a", [("a-0", JobSizing(4.0, MIB), lambda n=None: seen.append(n))],
             started_at=0.0)
    assert c.drain() == 1
    assert seen == [None], "placed on the emptiest node, unpinned because it has no name"


def test_a_cluster_with_no_labels_at_all_still_runs():
    """The regression that took a live deployment down to zero throughput: upgrading the
    service does not re-run setup, so no node had the identity label, and per-node admission
    silently had nothing it was willing to place on."""
    p = FakeProvider(per_node=[(None, 96.0, 10240 * MIB, 0), (None, 32.0, 10240 * MIB, 0)])
    c = _controller(p)
    made = _items(c, "a", 5, cpu=4.0)
    assert c.drain() == 5, "an unlabelled cluster must still admit work"
    assert len(made) == 5


def test_a_growable_cluster_may_create_unpinned_when_no_node_fits():
    """The autoscaler's exception, and the reason it is narrow. Room that exists on no node
    yet cannot be pinned to, and refusing is self-defeating -- a pending pod is what makes an
    autoscaler grow. So the job is created without a selector and kube-scheduler settles it.
    """
    p = FakeProvider(per_node=[("a", 1.0, 10240 * MIB, 0)], growable=True)
    c = _controller(p)
    seen = []
    c.submit("a", [("a-0", JobSizing(4.0, MIB), lambda n=None: seen.append(n))],
             started_at=0.0)
    assert c.drain() == 1
    assert seen == [None], "unpinned, because the room is not on any node yet"


def test_a_growable_cluster_does_not_create_the_whole_batch_at_once():
    """The autoscaler exception was a hole, and this is the size of it.

    Every pending item "fits" a growable cluster, and an unpinned hold is charged to no node
    -- so one drain created all 1435 of a batch, unaccounted. That is exactly the flood this
    module was written to end, reintroduced by the one branch that skips the per-node test.

    A handful of unplaceable pods says "add nodes" as loudly as a thousand do; the thousand
    add unaccounted reservations, not signal.
    """
    p = FakeProvider(per_node=[("a", 1.0, 10240 * MIB, 0)], growable=True)
    c = _controller(p)
    made = _items(c, "a", node_admission.GROWTH_UNPINNED_LIMIT * 3, cpu=4.0)
    assert c.drain() == node_admission.GROWTH_UNPINNED_LIMIT
    assert len(made) == node_admission.GROWTH_UNPINNED_LIMIT
    # And it stays capped while nothing has been placed: draining again buys nothing.
    assert c.drain() == 0
    assert "autoscaler has not produced" in c.refusal("a")


def test_growth_resumes_as_the_new_nodes_take_the_work():
    """The cap bounds work in flight, not the size the cluster reaches.

    Once a pod is bound its requests are in the reading, so it is charged to a real node like
    any other and its slot frees. A cap that did not release would stall a campaign at
    GROWTH_UNPINNED_LIMIT jobs forever on a cluster that grew for it.
    """
    p = FakeProvider(per_node=[("a", 1.0, 10240 * MIB, 0)], growable=True)
    c = _controller(p)
    made = _items(c, "a", node_admission.GROWTH_UNPINNED_LIMIT + 3, cpu=4.0)
    assert c.drain() == node_admission.GROWTH_UNPINNED_LIMIT

    p.observe(*made)            # the autoscaler produced nodes; the pods are bound
    assert c.drain() == 3, "the rest go once the earlier ones stopped being in flight"


def test_a_static_cluster_never_creates_unpinned():
    """The same shape without the flag must refuse. Creating unpinned on a full static cluster
    is precisely the over-admission per-node budgets exist to prevent."""
    p = FakeProvider(per_node=[("a", 1.0, 10240 * MIB, 0)], growable=False)
    c = _controller(p)
    made = _items(c, "a", 1, cpu=4.0)
    assert c.drain() == 0 and made == []


def test_a_batch_spreads_rather_than_filling_one_node_first():
    """Emptiest-first. Packing one machine full and then discovering the rest of the cluster
    cannot take the shape that is left is how fragmentation is manufactured."""
    p = FakeProvider(per_node=[("a", 6.0, 10240 * MIB, 0), ("b", 6.0, 10240 * MIB, 0)])
    c = _controller(p)
    seen = []
    c.submit("a", [(f"a-{i}", JobSizing(3.0, MIB), lambda n=None: seen.append(n))
                   for i in range(4)], started_at=0.0)
    assert c.drain() == 4
    assert sorted(seen) == ["a", "a", "b", "b"], f"expected an even spread, got {seen}"


def test_a_zero_cpu_sizing_is_refused_by_preflight():
    """The controller must not accept a sizing of nothing, whoever built it.

    Zero fits every node, so the queue would admit an entire plan in one pass and stop
    gating. The manifest-side caller refuses this first and can name the containers; this
    is the backstop, because a controller that accepts a zero sizing is not a queue.
    """
    c = AdmissionController(FakeProvider())
    with pytest.raises(AdmissionRefused) as err:
        c.preflight(JobSizing(cpu=0, memory=0))
    assert "resources.cpu" in str(err.value)


def test_a_node_can_be_refused_without_the_queue_learning_why():
    """The gate calibration uses, expressed so the scheduler stays a scheduler.

    A node being measured must take no work until its figures are in, or the runs placed
    meanwhile are sized differently from every run that follows them. The queue is told only
    the answer: put the reason in here and calibration policy lives inside the scheduler.
    """
    p = FakeProvider(per_node=[("busy", 100.0, 10240 * MIB, 0),
                               ("open", 8.0, 10240 * MIB, 0)])
    c = _controller(p)
    seen = []
    c.submit("a", [("a-0", JobSizing(4.0, MIB), lambda n=None: seen.append(n))],
             started_at=0.0, accepts_node=lambda node: node != "busy")
    assert c.drain() == 1
    assert seen == ["open"], "the emptiest node was refused, so the other one took it"


def test_work_waits_when_every_node_is_refused():
    """Not an error and not a permanent refusal: the measurement finishes and the next drain
    places the work."""
    p = FakeProvider(per_node=[("n1", 100.0, 10240 * MIB, 0)])
    c = _controller(p)
    allowed = {"ok": False}
    made = []
    c.submit("a", [("a-0", JobSizing(4.0, MIB), lambda n=None: made.append(n))],
             started_at=0.0, accepts_node=lambda node: allowed["ok"])
    assert c.drain() == 0 and made == []
    allowed["ok"] = True
    assert c.drain() == 1 and made == ["n1"]


def test_a_job_is_sized_for_the_node_it_lands_on():
    """Per-node sizing has to be an admission fact, not a manifest detail: a node calibrated
    smaller genuinely holds more of them, and the arithmetic must agree with the manifest or
    the queue over- or under-admits."""
    p = FakeProvider(per_node=[("fast", 5.0, 10240 * MIB, 0)])
    c = _controller(p)
    made = []
    # Declared 4 cores, but this node was measured at 2 -- so two fit where one would.
    c.submit("a", [(f"a-{i}", JobSizing(4.0, MIB), lambda n=None, i=i: made.append(i))
                   for i in range(2)],
             started_at=0.0,
             sizing_for_node=lambda node: JobSizing(2.0, MIB) if node == "fast" else None)
    assert c.drain() == 2, "both fit at the calibrated size"
    assert made == [0, 1]


def test_calibration_outlives_a_batch_because_a_search_has_many():
    """A search runs batch after batch through a NEW BatchJobRunner each time. Calibration
    owned there would be discarded and re-measured every round -- four times the probe cost
    for the same answer on a four-batch search."""
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    c = _controller()
    first = c.calibration("camp", NodeCalibration)
    assert c.calibration("camp", NodeCalibration) is first, "a later batch reuses it"
    assert c.calibration("other-camp") is None, "and it is per campaign"


def test_cancelling_a_batch_keeps_the_campaign_calibration():
    """The bug this pins, and it shipped. ``cancel`` runs in the ``finally`` at the end of
    every BATCH -- a search builds a fresh runner per batch -- so dropping the calibration
    there made every batch re-probe every node.

    Measured on a live search: four probe runs per batch instead of per campaign, and the
    figures moved between batches (one node's system under test went 1.820 to 1.106 cores),
    so runs in different batches of the SAME campaign were sized differently. That defeats
    the property calibration exists to provide.
    """
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    c = _controller()
    first = c.calibration("camp", NodeCalibration)
    c.cancel("camp")
    assert c.calibration("camp") is first, "the next batch must reuse it, not re-measure"


def test_a_finished_campaign_takes_its_calibration_with_it():
    """Measured under this campaign's contention, for its containers. Handing it to the next
    campaign is the transferable factor this cluster's own data refuted -- so the campaign's
    end, not its batch's, is what ends it."""
    from robovast.execution.cluster_execution.node_calibration import NodeCalibration

    c = _controller()
    c.calibration("camp", NodeCalibration)
    assert c.forget_calibration("camp") is True
    assert c.calibration("camp") is None
    assert c.forget_calibration("camp") is False, "and forgetting twice is not an error"


def test_a_pinned_item_goes_to_its_node_or_waits_for_it():
    """A calibration probe measures a particular machine. Placed elsewhere it answers a
    question about the wrong node, so it waits rather than settling for another -- the
    opposite of how ordinary work is placed."""
    p = FakeProvider(per_node=[("busy", 1.0, 10240 * MIB, 0), ("idle", 99.0, 10240 * MIB, 0)])
    c = _controller(p)
    seen = []
    c.submit("a", [("probe-busy", JobSizing(4.0, MIB), lambda n=None: seen.append(n))],
             started_at=0.0, pin="busy")
    assert c.drain() == 0, "its node is full; the idle one is not a substitute"
    assert seen == []

    p._per_node = [("busy", 8.0, 10240 * MIB, 0), ("idle", 99.0, 10240 * MIB, 0)]
    p.free = p._budget(frozenset())
    assert c.drain() == 1 and seen == ["busy"]


def test_a_pin_overrides_the_accepts_node_gate():
    """The gate exists to keep campaign work off a node until its probe reports. The probe
    itself must be exempt, or it would be waiting for its own measurement."""
    p = FakeProvider(per_node=[("n1", 8.0, 10240 * MIB, 0)])
    c = _controller(p)
    seen = []
    c.submit("a", [("probe", JobSizing(2.0, MIB), lambda n=None: seen.append(n))],
             started_at=0.0, pin="n1", accepts_node=lambda node: False)
    assert c.drain() == 1 and seen == ["n1"]


def test_node_ids_lists_only_what_can_be_pinned_to():
    """An unlabelled node holds work but cannot be selected, so probing it would produce a
    figure nothing could ever be pinned to."""
    p = FakeProvider(per_node=[("named", 8.0, 10240 * MIB, 0),
                               (None, 8.0, 10240 * MIB, 0)])
    assert _controller(p).node_ids() == ["named"]


def test_the_sizing_callback_may_not_reenter_the_queue():
    """A callback that asks the queue something must not deadlock it.

    ``drain`` calls ``sizing_for_node`` and ``accepts_node`` WHILE HOLDING the queue's lock,
    so with a plain Lock any callback that touches the queue blocks on a lock its own thread
    already owns. That hung a live campaign with nothing to go on: the batch loop never
    finished its first iteration, so nothing was created, nothing was logged, and it sat
    there until the no-progress deadline called it stalled.

    The caller that did it has been changed not to, but the lock is reentrant now so the
    whole class is gone rather than the one instance. Pinned with a callback that does
    exactly what the broken one did; without the fix this hangs rather than fails, hence the
    watchdog.
    """
    import threading

    p = FakeProvider(per_node=[("n1", 8.0, 10240 * MIB, 0)])
    c = _controller(p)

    def _reenters(node_id):
        c.node_ids()          # any public method: they all take the lock

    c.submit("a", [("a-0", JobSizing(2.0, MIB), lambda n=None: None)],
             started_at=0.0, sizing_for_node=_reenters)

    done = threading.Event()
    err = []

    def _run():
        try:
            c.drain()
        except Exception as exc:  # noqa: BLE001
            err.append(exc)
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert done.wait(timeout=5), (
        "drain() did not return: the sizing callback re-entered the queue's lock")


# -- a create that will never work ----------------------------------------------------------

def test_a_create_that_keeps_failing_is_eventually_given_up_on():
    """Retrying forever reported the symptom hours from the cause.

    A create that raises is left PLANNED because the common causes are transient. The ones
    that are not -- an RBAC change, a validating webhook, a ResourceQuota, an unparseable node
    pool -- look identical from here, and were retried every two seconds with nothing said.
    The campaign created nothing and was eventually called *stalled*, which describes what an
    observer saw rather than what happened.
    """
    c = _controller()

    def _boom(_node=None):
        raise RuntimeError("admission webhook denied the request")

    c.submit("a", [("a-0", JobSizing(1.0, MIB), _boom)], started_at=0.0)
    for _ in range(node_admission.CREATE_ATTEMPT_LIMIT):
        assert c.drain() == 0

    assert c.states("a") == {}, "given up on, not retried by every later drain"
    reason = c.refusal("a")
    assert "admission webhook denied" in reason, "and the cause travels with the verdict"
    assert str(node_admission.CREATE_ATTEMPT_LIMIT) in reason


def test_a_transient_failure_does_not_count_against_a_later_success():
    """The counter is consecutive failures, not lifetime ones.

    An API blip on one drain must not bring an item closer to being abandoned on a drain
    weeks of trials later.
    """
    c = _controller()
    calls = []

    def _flaky(_node=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection reset")

    c.submit("a", [("a-0", JobSizing(1.0, MIB), _flaky)], started_at=0.0)
    assert c.drain() == 0
    assert c.drain() == 1
    assert c.states("a") == {"a-0": "created"}


def test_the_refusal_is_per_owner():
    """One global slot meant campaign B read campaign A's job sizes as its own reason."""
    p = FakeProvider(per_node=[("n1", 4.0, 4096 * MIB, 0)])
    c = _controller(p)
    _items(c, "big", 1, cpu=99.0, started_at=1.0)
    _items(c, "small", 1, cpu=1.0, started_at=2.0)

    assert c.drain() == 1, "the small one fits"
    assert "99 cpu" in c.refusal("big")
    assert c.refusal("small") == "", "a campaign that was served has nothing to explain"


# -- several campaign threads at once --------------------------------------------------------

def test_concurrent_drains_never_over_admit():
    """The central claim of the design, and nothing exercised it.

    Every campaign runs on its own thread and they all work the GLOBAL queue -- that is what
    makes the ordering cluster-wide without giving the controller a thread of its own. So the
    property that matters is that N threads draining at once hand out the same capacity ONE
    thread would: a node's cores can be granted once, however many callers ask.
    """
    import threading

    capacity = 40
    p = FakeProvider(per_node=[("n1", float(capacity), 1_000_000 * MIB, 0)])
    c = _controller(p)

    created = []
    guard = threading.Lock()

    def _record(_node=None, key=None):
        with guard:
            created.append(key)

    for owner in range(4):
        c.submit(f"c{owner}",
                 [(f"c{owner}-{i}", JobSizing(1.0, MIB),
                   (lambda n=None, k=f"c{owner}-{i}": _record(n, k)))
                  for i in range(25)],
                 started_at=float(owner))

    threads = [threading.Thread(target=c.drain) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == len(set(created)), "no job was created twice"
    assert len(created) == capacity, (
        "exactly the node's cores were spent -- not more by racing, not fewer by blocking")


def test_a_callback_may_ask_the_queue_from_another_thread_while_a_drain_holds_it():
    """The reentrant lock removes a deadlock class; it must not have introduced a livelock.

    `sizing_for_node` runs inside `drain`, under the lock, on the draining thread. A DIFFERENT
    campaign's thread asking the queue anything at that moment must simply wait its turn --
    not deadlock, and not be served stale state mid-drain.
    """
    import threading

    p = FakeProvider(per_node=[("n1", 8.0, 8192 * MIB, 0)])
    c = _controller(p)
    entered = threading.Event()
    release = threading.Event()
    answers = []

    def _sizing(_node_id):
        entered.set()
        release.wait(timeout=5)

    c.submit("a", [("a-0", JobSizing(1.0, MIB), lambda n=None: None)],
             started_at=0.0, sizing_for_node=_sizing)

    asker = threading.Thread(target=lambda: answers.append(c.states("b")))
    drainer = threading.Thread(target=c.drain)
    drainer.start()
    assert entered.wait(timeout=5), "the callback runs inside the drain"
    asker.start()
    release.set()
    drainer.join(timeout=5)
    asker.join(timeout=5)

    assert not drainer.is_alive() and not asker.is_alive(), "neither thread is stuck"
    assert answers == [{}]


def test_the_refusal_names_the_filter_that_actually_blocked_it():
    """Two filters empty the candidate list and they need opposite responses.

    Observed on a live campaign: "no node has that free (most free: 89.025 cpu)" for a job
    needing 4.25 cpu -- because all four nodes were out for calibration, and `may_use` was
    what excluded them. The message blamed capacity over an idle cluster, sending the reader
    to look for room that was never the problem. `biggest` was also computed over every node
    including the excluded ones, which is where the contradictory 89 came from.
    """

    c = _controller(FakeProvider(cpu=64.0))
    # Every node held: the calibration gate, or a node pool this campaign is outside.
    c.submit("camp", [("j-0", JobSizing(4.25, MIB), lambda _n=None: None)],
             started_at=0.0, accepts_node=lambda node_id: False)
    assert c.drain() == 0
    message = c.refusal("camp")
    assert "no node is accepting work yet" in message, message
    assert "being measured" in message
    assert "free" not in message, "capacity is not the reason and must not be offered as one"


def test_a_genuine_capacity_refusal_still_says_so():
    """The other branch, and it must count only nodes this campaign could actually use --
    reporting the free cores of a node it is excluded from is the same lie in reverse."""

    c = _controller(FakeProvider(cpu=2.0))
    c.submit("camp", [("j-0", JobSizing(99.0, MIB), lambda _n=None: None)], started_at=0.0)
    assert c.drain() == 0
    message = c.refusal("camp")
    assert "needs 99 cpu" in message and "usable" in message, message
