# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Deciding WHEN a campaign's jobs are created, so that the cluster is never handed more
than it can run.

A typical campaign's plan is upwards of a thousand Jobs (``runs_per_job`` defaults to 1),
and creating them in one loop overwhelms both the cluster and the kubelets pulling their
images. The property this module exists to hold: **a job is created only when there is room
for it**, so nothing ever reaches the scheduler that the scheduler cannot place.

**It is a queue, not a per-caller reservation service, and that is the load-bearing decision.**
Every campaign runs on its own thread, so if each asked "may I go?" for itself the order would
be decided by which thread won the lock. A search campaign submits its batches one after
another, so ordering by submission makes an older campaign's later batches look younger than
a newer campaign, and the two end up taking turns instead of the older one finishing first.
Here the order is a
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
from dataclasses import dataclass
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
    *create* is called at most once, and only when there is room.
    """
    key: str
    sizing: JobSizing
    create: Callable[[], None]
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

    #: When set, the ONLY node this item may go to. A calibration probe measures one machine,
    #: so placing it anywhere else answers a question about the wrong node. Everything else
    #: leaves it unset and is placed wherever it fits.
    pin: "str | None" = None

    def may_use(self, node_id) -> bool:
        if self.pin is not None:
            return node_id == self.pin
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
    cannot exist, with no error anywhere.
    """


class AdmissionController:
    """The queue. Thread-safe; every public method takes the lock.

    Its own lock, never the service's ``_usage_lock``: that one is held across a resource
    reading that talks to every kubelet in turn, and sharing it would let one slow node block
    every campaign's job creation.
    """

    def __init__(self, provider: BudgetProvider, *, clock=None, budget_ttl: float = BUDGET_TTL_S):
        self._provider = provider
        self._clock = clock or (lambda: __import__("time").monotonic())
        self._budget_ttl = budget_ttl
        self._lock = threading.Lock()
        self._items: "Dict[str, WorkItem]" = {}
        self._held: "Dict[str, _Held]" = {}
        self._calibrations: dict = {}
        self._seq = itertools.count()
        self._budget: Optional[Budget] = None
        self._budget_at = 0.0
        self._last_refusal = ""

    # -- queue -------------------------------------------------------------------------

    def submit(self, owner: str, items: "Iterable[Tuple[str, JobSizing, Callable[[], None]]]",
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
        """
        created = 0
        with self._lock:
            pending = self._pending_in_order()
            if not pending:
                return 0
            nodes, growable = self._effective_free_locked(force=True)
            by_id = {n.node_id: n for n in nodes}
            for item in pending:
                if limit is not None and created >= limit:
                    break
                need = item.sizing
                # Emptiest-first, so a batch spreads rather than filling one machine and then
                # discovering the rest of the cluster cannot take the shape that is left.
                # A node with no identity label can hold work but cannot be pinned to, so it
                # is not a candidate -- its capacity still counts, via the reading.
                #
                # The fit is tested against what the job would need ON THAT NODE, which is
                # what makes per-node sizing an admission fact rather than a manifest detail:
                # a node calibrated smaller genuinely holds more of them.
                fits = [n for n in by_id.values()
                        if n.node_id and item.may_use(n.node_id)
                        and n.holds(item.sizing_on(n.node_id))]
                chosen = max(fits, key=lambda n: n.free_cpu) if fits else None
                if chosen is not None:
                    need = item.sizing_on(chosen.node_id)
                if chosen is None and not growable:
                    biggest = max((n.free_cpu for n in by_id.values()), default=0.0)
                    self._last_refusal = (
                        f"{len(pending) - created} job(s) waiting: next needs "
                        f"{need.cpu:g} cpu / {need.memory // (1024 ** 2)}Mi and no node has "
                        f"that free (most free: {biggest:g} cpu)")
                    continue
                try:
                    # None means unpinned: only reachable on a growable cluster, where the
                    # room is real but not on any node yet.
                    item.create(chosen.node_id if chosen else None)
                except Exception:
                    # The caller owns the failure; leave the item PLANNED so a later drain can
                    # retry, and never hold a reservation for a job that was not created.
                    logger.warning("admission: creating %s failed; left planned",
                                   item.key, exc_info=True)
                    continue
                item.state = CREATED
                node_id = chosen.node_id if chosen else None
                self._held[item.key] = _Held(item.owner, need.cpu, need.memory, need.gpu,
                                             node_id)
                if chosen is not None:
                    by_id[node_id] = NodeBudget(
                        node_id=node_id,
                        free_cpu=chosen.free_cpu - need.cpu,
                        free_memory=chosen.free_memory - need.memory,
                        free_gpu=chosen.free_gpu - need.gpu)
                created += 1
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
        """
        with self._lock:
            keys = [k for k, i in self._items.items() if i.owner == owner]
            for key in keys:
                self._items.pop(key, None)
                self._held.pop(key, None)
            for key in [k for k, h in self._held.items() if h.owner == owner]:
                self._held.pop(key, None)
            # Its calibration goes too: measured under this campaign's contention, for this
            # campaign's containers, and deliberately never reused by the next one.
            self._calibrations.pop(owner, None)
            return len(keys)

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
        # A zero-cpu pod fits everything, so the queue would stop gating and create the whole
        # plan at once. The caller that builds a sizing from a manifest refuses this first and
        # can name the containers; this is the backstop for every other caller, because a
        # controller that accepts a zero sizing is not a queue.
        if sizing.cpu <= 0:
            raise AdmissionRefused(
                "a job must declare how much cpu it needs; this one asks for none, which "
                "would admit the entire plan at once. Declare "
                "execution.containers.<name>.resources.cpu.")
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

    def refusal(self) -> str:
        """Why nothing was created last time, for the campaign's log."""
        with self._lock:
            return self._last_refusal

    # -- internals ---------------------------------------------------------------------

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
