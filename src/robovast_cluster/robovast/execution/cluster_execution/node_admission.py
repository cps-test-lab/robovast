# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Deciding WHEN a campaign's jobs are created, so that the cluster is never handed more
than it can run.

RoboVAST used to create every Job of a batch in one loop and let something else cope. That
failed on large campaigns, which is why Kueue was adopted; a typical campaign creates ~1435
Jobs in one shot (``runs_per_job`` defaults to 1). This module takes that duty back, with the
one property that matters preserved: **a job is created only when there is room for it**, so
nothing ever reaches the scheduler that the scheduler cannot place.

**It is a queue, not a per-caller reservation service, and that is the load-bearing decision.**
Every campaign runs on its own thread, so if each asked "may I go?" for itself the order would
be decided by which thread won the lock. That is precisely the failure the per-campaign
Kueue priority class was written to fix, back when Kueue ordered admission: a search
campaign submits its batches one after another, so ordering by
submission makes an older campaign's later batches look younger than a newer campaign, and
"the two end up taking turns instead of the older one finishing first". Here the order is a
property of the queue -- ``(priority, campaign start)`` -- and no thread can change it by
being quick.

**No thread of its own, deliberately.** :meth:`AdmissionController.drain` is called by the
campaign threads that already exist, and it works the *global* queue rather than the caller's
own items, so whichever campaign happens to be awake advances everybody. Nothing here can die
separately from the service, there is no supervisor to write, and one campaign's crash cannot
starve another's admission for as long as any other campaign is still polling.

**Values in, values out.** No Kubernetes object crosses this boundary and no callback reaches
back into it, so the whole module is testable without a cluster -- and extracting it into a
process of its own later means replacing the class with a stub, not untangling it.
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

#: A planned job that has not been created yet.
PLANNED = "planned"
#: Created in the cluster; its reservation is held until it finishes.
CREATED = "created"

#: How long a capacity reading may be reused. Shorter than ``LocalTransport._USAGE_CACHE_TTL``
#: (10 s) because this gates a create loop rather than a status chip: a stale reading here
#: means either idle capacity or over-admission, where there it only means a slightly old
#: number on a screen.
BUDGET_TTL_S = 3.0

#: How many jobs may be outstanding **unpinned** at once on a cluster that can grow.
#:
#: An unpinned job is one admitted against capacity that exists on no node yet, so nothing
#: subtracts it from anything -- it is charged to the node it lands on only once the autoscaler
#: has produced that node and the pod is bound. Between those two moments the queue has no
#: figure to spend against, and without a cap that is not a gap but the whole batch: every
#: pending item "fits" a growable cluster, so one ``drain`` created all of them, unaccounted,
#: which is precisely the flood this module was written to end.
#:
#: A cap restores the property without giving up the autoscaler. Growth is driven by pods the
#: scheduler cannot place, and a handful of them says "add nodes" exactly as loudly as a
#: thousand do; what the extra thousand add is unaccounted reservations, not signal. As each
#: lands on a new node it becomes counted and another may go, so this bounds the work in
#: flight rather than the size the cluster reaches.
GROWTH_UNPINNED_LIMIT = 16

#: How many times a create may fail before the item is given up on, with its cause.
#:
#: A create that raises is left PLANNED, because the common causes are transient -- an API
#: blip, a node going away between the reading and the call. The causes that are *not*
#: transient look identical from here and are at least as likely: an RBAC change, a validating
#: webhook, a ``ResourceQuota``, a node-pool label the deployment cannot parse. Retrying those
#: forever gave the campaign no error and no cause; it simply never created anything and ended
#: when the no-progress deadline eventually called it stalled -- a failure reported as a
#: symptom, hours from the thing that caused it.
#:
#: Deliberately generous. At the batch loop's two-second cadence this is under a minute of
#: retrying, which covers an API restart while still ending long before a stall deadline.
CREATE_ATTEMPT_LIMIT = 20


def campaign_start_key(campaign_id: str) -> float:
    """A sortable campaign start time, read out of the campaign id.

    The id carries it already (``<name>-YYYY-MM-DD-HHMMSScc``), which is why nothing has to
    be looked up: a batch runner deep in the cluster lane can order itself against campaigns
    it has never heard of.

    Parsed **naively**, never through an epoch conversion, for the reason
    ``campaign_priority_value`` records: this only has to be monotone in the wall-clock label,
    and going via epoch seconds folds the repeated hour of a DST fall-back onto itself and
    inverts two campaigns' order.

    An unparseable id sorts last rather than raising. Ordering is a preference, and refusing to
    run a campaign because its name is unusual would be a much worse failure than running it
    after its neighbours.
    """
    parts = campaign_id.rsplit("-", 4)
    if len(parts) == 5:
        try:
            y, mo, d, hms = parts[1], parts[2], parts[3], parts[4]
            if not (len(y) == 4 and len(mo) == 2 and len(d) == 2 and hms.isdigit()):
                raise ValueError(campaign_id)
            return float(f"{y}{mo}{d}{hms:0<8s}")
        except (TypeError, ValueError):
            pass
    logger.debug("campaign id %r carries no timestamp; ordering it last", campaign_id)
    return float("inf")


@dataclass(frozen=True)
class JobSizing:
    """What one job needs, summed over its containers.

    Summed rather than per-container because that is what the scheduler bin-packs by: a pod's
    request is the sum of its containers', and admission has to answer the same question the
    scheduler will.
    """
    cpu: float
    memory: int
    gpu: int = 0


@dataclass(frozen=True)
class Capacity:
    """What one node could hold if it were empty. Used only to answer "ever", never "now"."""
    cpu: float
    memory: int
    gpu: int = 0

    def holds(self, sizing: JobSizing) -> bool:
        return (self.cpu >= sizing.cpu
                and self.memory >= sizing.memory
                and self.gpu >= sizing.gpu)


@dataclass(frozen=True)
class NodeBudget:
    """What is free on ONE node right now.

    Per node rather than cluster-wide because a pod runs on one machine: a cluster with room
    in total and none on any single node is the state where jobs are admitted and then sit
    ``Unschedulable``. Measured on 2026-08-26: 11.31 cores free across the cluster and no node
    holding the 4.75 a pod needed.

    *node_id* is the value of ``robovast.io/node-id`` -- the same hash ``runs.node_label``
    records -- so it can be used directly as a ``nodeSelector`` and appears in no manifest as
    a hostname. ``None`` for a node that has not been labelled yet (one that joined after the
    last ``setup``): such a node is still *counted*, because its pods are real and its
    capacity is real, but nothing can be pinned to it.
    """
    node_id: "str | None"
    free_cpu: float
    free_memory: int
    free_gpu: int = 0

    def holds(self, sizing: "JobSizing") -> bool:
        return (self.free_cpu >= sizing.cpu
                and self.free_memory >= sizing.memory
                and self.free_gpu >= sizing.gpu)


@dataclass(frozen=True)
class Budget:
    """What is free right now, per node, and which jobs the reading already accounts for.

    ``counted_jobs`` is the whole of the double-counting fix and the reason this is not just a
    pair of numbers. A Job created a moment ago has no pod bound to a node yet, so the next
    reading does not see its requests; handing the same cores out again is exactly the
    over-admission a stale quota produces. The provider reports which Jobs it *did* see, and a
    reservation stops being subtracted the instant the measurement starts subtracting the real
    pod -- with no timer, and no window where the cores are counted twice or not at all.

    ``growable`` is the autoscaler's exception, and it is narrow on purpose. A cluster that
    can add nodes has room that exists on no node yet, so a strict per-node test would refuse
    exactly the pods whose pending state is what makes an autoscaler grow -- self-defeating,
    and invisible except on a cluster nobody here runs. When it is set, a job that fits no
    current node may still be created **unpinned**, leaving the placement to kube-scheduler.
    A static cluster leaves it ``False`` and per-node is authoritative.
    """
    nodes: tuple = ()
    counted_jobs: frozenset = frozenset()
    growable: bool = False

    @property
    def free_cpu(self) -> float:
        """Cluster-wide free cores. For reporting only -- never for deciding placement,
        which is the confusion this whole type exists to prevent."""
        return sum(n.free_cpu for n in self.nodes)

    @property
    def free_memory(self) -> int:
        return sum(n.free_memory for n in self.nodes)

    @property
    def free_gpu(self) -> int:
        return sum(n.free_gpu for n in self.nodes)


class BudgetProvider(Protocol):
    """Where "how much room is there" comes from.

    **The multi-tenancy seam.** Today the only implementation measures the cluster. When
    RoboVAST has to share with a different tenant it can be swapped for one reading a
    cluster-side quota, and the admission loop does not change: RoboVAST can only ever
    arbitrate its own work, so a fair share against a neighbour has to be enforced by
    something neither owns, and this is where that plugs in.
    """

    def budget(self) -> Budget:
        """Free capacity now."""

    def capacities(self) -> "List[Capacity]":
        """What each node could hold if empty -- for :meth:`AdmissionController.preflight`."""


@dataclass
class WorkItem:
    """One job waiting to be created.

    *key* is the Job's name: unique cluster-wide, stable, and the same string the pod pass
    reports back in :attr:`Budget.counted_jobs`, so the ledger needs no mapping table.

    *create* is called with the node the item was granted -- ``(node_id: str | None) -> None``,
    where ``None`` means "create unpinned". It is called only when there is room, and at most
    once per successful create; a call that raises leaves the item PLANNED to be retried, up
    to :data:`CREATE_ATTEMPT_LIMIT`.
    """
    key: str
    sizing: JobSizing
    create: "Callable[[Optional[str]], None]"
    owner: str = ""
    priority: int = 0
    started_at: float = 0.0
    seq: int = 0
    state: str = PLANNED
    #: ``(node_id) -> JobSizing | None``. See :meth:`AdmissionController.submit`.
    sizing_for_node: "Callable | None" = None
    #: ``(node_id) -> bool``: may this owner's work go there *yet*. Symmetric with
    #: ``sizing_for_node`` and for the same reason -- the queue asks, and never learns why the
    #: answer is no. Today it is "that node is still being measured"; the queue knowing that
    #: would put calibration policy inside the scheduler.
    accepts_node: "Callable | None" = None

    #: Consecutive failed ``create`` calls, and the last one's message. See
    #: :data:`CREATE_ATTEMPT_LIMIT`.
    attempts: int = 0
    last_error: str = ""

    #: When set, the ONLY node this item may go to. A calibration probe measures one machine,
    #: so placing it anywhere else answers a question about the wrong node. Everything else
    #: leaves it unset and is placed wherever it fits.
    pin: "str | None" = None

    def may_use(self, node_id) -> bool:
        if self.pin is not None:
            return node_id == self.pin
        # An unlabelled node is allowed: it cannot be measured, so there is nothing for the
        # gate to wait for, and refusing it would make an unlabelled cluster unusable.
        return self.accepts_node is None or node_id is None or self.accepts_node(node_id)

    def sizing_on(self, node_id) -> "JobSizing":
        """What this job needs *on that node*, falling back to what it declared."""
        if self.sizing_for_node is None or node_id is None:
            return self.sizing
        return self.sizing_for_node(node_id) or self.sizing


@dataclass
class _Held:
    """A granted reservation: charged against free capacity until its pod is observed.

    *node_id* is where it was granted, so the charge lands on the node that will carry it.
    ``None`` for a job created unpinned on a growable cluster: it is charged nowhere, because
    it is going to a node that does not exist yet and charging an existing one would refuse
    work that node could still take.
    """
    owner: str
    cpu: float
    memory: int
    gpu: int
    node_id: "str | None" = None


class AdmissionRefused(Exception):
    """A sizing no node in this cluster could ever run. Permanent, so it raises.

    Distinct from "no room now", which is an ordinary answer (``drain`` simply creates
    nothing). Conflating the two is how a campaign ends up waiting forever for capacity that
    cannot exist, with no error anywhere -- the failure the Kueue admission preflight
    existed to prevent, and which this inherited when Kueue was retired.
    """


class AdmissionController:
    """The queue. Thread-safe: every public method that touches the queue's state takes the
    lock. :meth:`preflight` is the exception and needs no lock -- it reads the provider and
    nothing of this object's, and taking one would hold every campaign's job creation behind
    a cluster read that answers a question about none of them.

    Its own lock, never the service's ``_usage_lock``: that one is held across a resource
    reading that talks to every kubelet in turn, and sharing it would let one slow node block
    every campaign's job creation.
    """

    def __init__(self, provider: BudgetProvider, *, clock=None, budget_ttl: float = BUDGET_TTL_S):
        self._provider = provider
        self._clock = clock or (lambda: __import__("time").monotonic())
        self._budget_ttl = budget_ttl
        # Reentrant, deliberately. ``drain`` calls the caller's ``sizing_for_node`` and
        # ``accepts_node`` callbacks WHILE HOLDING this lock, and a callback that asks the
        # queue anything -- the campaign's calibration, the node list -- would otherwise
        # block on a lock its own thread already owns. That deadlock hung a live campaign
        # with no error and no log line: the batch loop never finished its first iteration,
        # so nothing was created and nothing was said until the no-progress deadline called
        # it stalled. Making the lock reentrant removes the whole class rather than the one
        # callback that happened to do it.
        self._lock = threading.RLock()
        self._items: "Dict[str, WorkItem]" = {}
        self._held: "Dict[str, _Held]" = {}
        self._calibrations: dict = {}
        self._seq = itertools.count()
        self._budget: Optional[Budget] = None
        self._budget_at = 0.0
        #: ``owner -> why nothing was created for it last time``. Per owner, not one string:
        #: ``drain`` works the global queue, so a single slot was overwritten by whichever
        #: campaign's item happened to be next -- and campaign B would have read campaign A's
        #: job sizes as the reason for its own wait.
        self._refusals: "Dict[str, str]" = {}

    # -- queue -------------------------------------------------------------------------

    def submit(self, owner: str,
               items: "Iterable[Tuple[str, JobSizing, Callable[[Optional[str]], None]]]",
               *, started_at: float, priority: int = 0, sizing_for_node=None,
               accepts_node=None, pin=None) -> int:
        """Enqueue a campaign's whole plan. Returns how many were accepted.

        *started_at* is the CAMPAIGN's start, not this batch's: a search submits batch after
        batch, and ordering by submission would let a newer campaign overtake an older one
        between its rounds.

        *pin* restricts these items to one node. A calibration probe measures a particular
        machine, so placing it elsewhere answers a question about the wrong one -- and it
        waits for that node rather than settling for another, which is the opposite of how
        ordinary work is placed.

        *accepts_node* is ``(node_id) -> bool``: whether this owner's work may go there yet.
        A node being measured for this campaign answers ``False`` until its figures are in, so
        that every run on it is sized the same way -- but the queue is told only the answer,
        never the reason.

        *sizing_for_node* is how per-node sizing stays out of here. It is
        ``(node_id) -> JobSizing | None``, asked once per placement attempt, and ``None`` means
        "no figure for that node, use the declared one". The controller does arithmetic on
        sizes and must not learn what a container is: the moment it knows the difference
        between a simulator and a system under test, the per-node *policy* lives in the queue
        rather than beside the thing it is a policy about.
        """
        with self._lock:
            added = 0
            for key, sizing, create in items:
                if key in self._items:
                    continue  # re-submitting a plan must not double it
                self._items[key] = WorkItem(key=key, sizing=sizing, create=create, owner=owner,
                                            priority=priority, started_at=started_at,
                                            seq=next(self._seq),
                                            sizing_for_node=sizing_for_node,
                                            accepts_node=accepts_node, pin=pin)
                added += 1
            return added

    def drain(self, *, limit: Optional[int] = None) -> int:
        """Create as many of the globally-highest-priority jobs as currently fit.

        Works the whole queue, not the caller's own items: that is what makes the ordering
        global while keeping this thread-free. Returns the number created.

        A job that does not fit is **skipped, not blocked behind** -- with mixed sizes a large
        job must not hold the cluster idle while smaller ones could run. Within a campaign the
        jobs are the same shape, so this costs nothing there.

        On a growable cluster a job that fits no node may still be created unpinned, but only
        up to :data:`GROWTH_UNPINNED_LIMIT` of them at a time -- see there for why the cap is
        what keeps the autoscaler exception from being a hole.
        """
        created = 0
        with self._lock:
            pending = self._pending_in_order()
            if not pending:
                return 0
            nodes, growable = self._effective_free_locked(force=True)
            by_id = {n.node_id: n for n in nodes}
            unpinned = self._unpinned_outstanding_locked()
            failed: "List[WorkItem]" = []
            for item in pending:
                if limit is not None and created >= limit:
                    break
                need = item.sizing
                # Emptiest-first, so a batch spreads rather than filling one machine and then
                # discovering the rest of the cluster cannot take the shape that is left.
                #
                # The fit is tested against what the job would need ON THAT NODE, which is
                # what makes per-node sizing an admission fact rather than a manifest detail:
                # a node calibrated smaller genuinely holds more of them.
                #
                # **An unlabelled node is a candidate.** It cannot be *pinned* to -- there is
                # no selector for it -- but it can hold work, and excluding it was a hang
                # waiting to happen: a cluster whose nodes predate the identity label has NO
                # candidates at all, so nothing is ever admitted and every campaign waits
                # forever reporting "queued for capacity" on an idle cluster. Observed exactly
                # that way. A missing label now costs the pin, never the run.
                fits = [n for n in by_id.values()
                        if item.may_use(n.node_id)
                        and n.holds(item.sizing_on(n.node_id))]
                chosen = max(fits, key=lambda n: n.free_cpu) if fits else None
                if chosen is not None:
                    need = item.sizing_on(chosen.node_id)
                if chosen is None and not (growable and unpinned < GROWTH_UNPINNED_LIMIT):
                    waiting = f"{len(pending) - created} job(s) waiting"
                    # Which of the two filters emptied the list, because they need opposite
                    # responses and the message is the only thing an operator sees. A node
                    # excluded by `may_use` is being measured, or is outside the configured
                    # pool -- reporting "no node has that free" over an idle cluster sent the
                    # reader to look for capacity that was never the problem. Observed saying
                    # "no node has that free (most free: 89 cpu)" for a job needing 4.25,
                    # while all four nodes were simply out for calibration.
                    usable = [n for n in by_id.values() if item.may_use(n.node_id)]
                    if chosen is None and growable:
                        self._refusals[item.owner] = (
                            f"{waiting}: {unpinned} already created for a node the "
                            f"autoscaler has not produced yet (limit "
                            f"{GROWTH_UNPINNED_LIMIT})")
                    elif not usable:
                        self._refusals[item.owner] = (
                            f"{waiting}: no node is accepting work yet "
                            f"({len(by_id)} node(s) held: being measured before work is "
                            f"placed on them, or outside this campaign's node pool)")
                    else:
                        biggest = max((n.free_cpu for n in usable), default=0.0)
                        self._refusals[item.owner] = (
                            f"{waiting}: next needs {need.cpu:g} cpu / "
                            f"{need.memory // (1024 ** 2)}Mi and no node has that free "
                            f"(most free of {len(usable)} usable: {biggest:g} cpu)")
                    continue
                try:
                    # ``None`` means create unpinned, and there are two ways to get here: a
                    # growable cluster whose room is not on any node yet, and a node that has
                    # no identity label to select it by. Both are "we know there is room, we
                    # cannot name where" -- and in both the scheduler places it, which is
                    # exactly what happened before per-node admission existed.
                    item.create(chosen.node_id if chosen else None)
                except Exception as exc:  # noqa: BLE001 - see CREATE_ATTEMPT_LIMIT
                    # The caller owns the failure; leave the item PLANNED so a later drain can
                    # retry, and never hold a reservation for a job that was not created --
                    # but not forever. A cause that is not transient looks exactly like one
                    # that is, and retrying it silently is how a campaign creates nothing for
                    # hours and is then reported as stalled rather than as refused.
                    item.attempts += 1
                    item.last_error = f"{exc.__class__.__name__}: {exc}"
                    if item.attempts >= CREATE_ATTEMPT_LIMIT:
                        failed.append(item)
                        self._refusals[item.owner] = (
                            f"could not create {item.key} after {item.attempts} attempts: "
                            f"{item.last_error}")
                        logger.error("admission: giving up on %s after %d attempts: %s",
                                     item.key, item.attempts, item.last_error)
                    else:
                        logger.warning("admission: creating %s failed (attempt %d/%d); "
                                       "left planned", item.key, item.attempts,
                                       CREATE_ATTEMPT_LIMIT, exc_info=True)
                    continue
                item.state = CREATED
                node_id = chosen.node_id if chosen else None
                self._held[item.key] = _Held(item.owner, need.cpu, need.memory, need.gpu,
                                             node_id)
                if node_id is None:
                    unpinned += 1
                if chosen is not None:
                    by_id[node_id] = NodeBudget(
                        node_id=node_id,
                        free_cpu=chosen.free_cpu - need.cpu,
                        free_memory=chosen.free_memory - need.memory,
                        free_gpu=chosen.free_gpu - need.gpu)
                # A create clears the owner's stale reason: a refusal that outlived the wait
                # it described is the same defect as the capacity-wait flag that outlived
                # its own, and it reads to an operator as a campaign still stuck.
                self._refusals.pop(item.owner, None)
                item.attempts = 0
                created += 1
            for item in failed:
                # Dropped from the queue, not left to be retried by every later drain of every
                # other campaign. The owner learns why through ``refusal``; its progress count
                # then falls, which is what ends its wait.
                self._items.pop(item.key, None)
        return created

    def finished(self, key: str) -> None:
        """Release a created job's reservation. Idempotent."""
        with self._lock:
            self._held.pop(key, None)
            self._items.pop(key, None)

    def cancel(self, owner: str) -> int:
        """Drop an owner's planned items and release its held reservations.

        Called from a ``finally``, because a campaign that raises on its way out would
        otherwise leak its reservations for the life of the process -- shrinking every other
        campaign's usable capacity, invisibly and cumulatively.

        **The calibration survives, and that is the point.** This runs at the end of every
        BATCH -- a search builds a fresh runner per batch -- so dropping the calibration here
        made every batch re-probe every node. Measured on a live search: four probe runs per
        batch instead of per campaign, and the figures moved between batches (one node's
        system-under-test went 1.820 to 1.106 cores), so runs in different batches of the same
        campaign were sized differently. That defeats the property calibration exists to
        provide, which is that every run of a campaign meets the same allocation.
        :meth:`forget_calibration` is what ends it, at the end of the campaign.
        """
        with self._lock:
            keys = [k for k, i in self._items.items() if i.owner == owner]
            for key in keys:
                self._items.pop(key, None)
                self._held.pop(key, None)
            for key in [k for k, h in self._held.items() if h.owner == owner]:
                self._held.pop(key, None)
            self._refusals.pop(owner, None)
            return len(keys)

    def forget_calibration(self, owner: str) -> bool:
        """Drop an owner's calibration, once its campaign is over. Returns whether there was one.

        Separate from :meth:`cancel` because their lifetimes differ: reservations are a
        batch's, calibration is a campaign's. Kept rather than left to leak because the
        figures are deliberately not reusable -- measured under THIS campaign's contention,
        for THIS campaign's containers -- so a later campaign must measure afresh rather than
        inherit numbers taken under a load it never met.
        """
        with self._lock:
            return self._calibrations.pop(owner, None) is not None

    def node_ids(self) -> list:
        """The identity of every node that can currently be pinned to.

        Whoever starts calibration probes needs to know which machines exist, and this is
        already measuring them every cycle. Excludes a node with no identity label: it can
        hold work but cannot be selected, so probing it would produce a figure nothing could
        ever be pinned to.
        """
        with self._lock:
            nodes, _ = self._effective_free_locked()
            return sorted(n.node_id for n in nodes if n.node_id)

    def growable(self) -> bool:
        """Whether the cluster can add nodes. See :attr:`Budget.growable`."""
        with self._lock:
            _, growable = self._effective_free_locked()
            return growable

    def calibration(self, owner: str, factory=None):
        """This campaign's per-node calibration, created once and kept for its lifetime.

        Held here because this is the only object whose lifetime is the campaign's rather
        than the batch's. A search runs batch after batch through a NEW ``BatchJobRunner``
        each time, so calibration owned there would be thrown away and re-measured every
        round -- paying the probe cost per batch instead of once, which for a four-batch
        search is four times the price for the same answer.

        Opaque on purpose: the queue stores it and never reads it. What a calibration means is
        the caller's business, and the two callbacks on :meth:`submit` are the whole of what
        the queue is told about it.
        """
        with self._lock:
            if owner not in self._calibrations and factory is not None:
                self._calibrations[owner] = factory()
            return self._calibrations.get(owner)

    def states(self, owner: str) -> "Dict[str, str]":
        """``key -> PLANNED | CREATED`` for one owner, for its own progress reporting.

        This is what lets a campaign say "waiting for room" from a fact rather than inferring
        it from pods that do not exist yet -- the blindness that made a merely-queued campaign
        report as stalled.
        """
        with self._lock:
            return {k: i.state for k, i in self._items.items() if i.owner == owner}

    # -- invariants --------------------------------------------------------------------

    def preflight(self, sizing: JobSizing) -> None:
        """Raise if no node could ever run this, however empty the cluster gets.

        Checked once before a batch is enqueued. Without it a campaign sits in the admit loop
        forever having created **zero** jobs, and every diagnosis path downstream is pod-based
        and therefore blind to it.
        """
        capacities = self._provider.capacities()
        if not capacities:
            raise AdmissionRefused(
                "no nodes are available to size against; the cluster reported none")
        if any(c.holds(sizing) for c in capacities):
            return
        biggest = max(capacities, key=lambda c: c.cpu)
        raise AdmissionRefused(
            f"a job needs {sizing.cpu:g} cpu / {sizing.memory // (1024 ** 2)}Mi and no node is "
            f"that large -- the biggest holds {biggest.cpu:g} cpu / "
            f"{biggest.memory // (1024 ** 2)}Mi. Reduce execution.containers.*.resources, or "
            "run where a node can hold it.")

    def refusal(self, owner: str) -> str:
        """Why nothing was created for *owner* last time, for its campaign's log.

        ``""`` when the last drain had nothing to refuse it. The caller decides how often to
        say it; this only records the most recent answer.
        """
        with self._lock:
            return self._refusals.get(owner, "")

    # -- internals ---------------------------------------------------------------------

    def _unpinned_outstanding_locked(self) -> int:
        """How many created-but-unplaced unpinned jobs the queue is carrying.

        An unpinned hold stops counting the moment the reading sees its pod: it is charged to
        a real node from then on, exactly as a pinned one is. So this measures work in flight
        towards nodes that do not exist yet -- which is what :data:`GROWTH_UNPINNED_LIMIT`
        bounds.
        """
        counted = self._budget.counted_jobs if self._budget else frozenset()
        return sum(1 for key, held in self._held.items()
                   if held.node_id is None and key not in counted)

    def _pending_in_order(self) -> "List[WorkItem]":
        """Highest priority first, then oldest campaign, then submission order.

        ``started_at`` before ``seq`` is the whole point: sequence is when this *batch* was
        enqueued, and an older campaign's second batch must still beat a younger campaign's
        first.
        """
        return sorted((i for i in self._items.values() if i.state == PLANNED),
                      key=lambda i: (-i.priority, i.started_at, i.seq))

    def _effective_free_locked(self, *, force: bool = False):
        """``([NodeBudget], growable)`` with in-flight reservations already subtracted.

        The ledger is applied **per node**, to the node each reservation was granted on: a
        job promised room on one machine must not appear to free capacity on another.
        """
        now = self._clock()
        if force or self._budget is None or (now - self._budget_at) >= self._budget_ttl:
            self._budget = self._provider.budget()
            self._budget_at = now
        budget = self._budget
        free = {n.node_id: [n.free_cpu, n.free_memory, n.free_gpu] for n in budget.nodes}
        for key, held in self._held.items():
            if key in budget.counted_jobs:
                continue  # the reading already subtracted its real pod
            if held.node_id not in free:
                continue  # unpinned, or a node that has since gone away
            free[held.node_id][0] -= held.cpu
            free[held.node_id][1] -= held.memory
            free[held.node_id][2] -= held.gpu
        return ([NodeBudget(node_id=k, free_cpu=v[0], free_memory=v[1], free_gpu=v[2])
                 for k, v in free.items()], budget.growable)
