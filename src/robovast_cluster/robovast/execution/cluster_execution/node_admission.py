# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Deciding WHEN a campaign's jobs are created, so that the cluster is never handed more
than it can run.

A typical campaign's plan is upwards of a thousand Jobs (one per run),
and creating them in one loop overwhelms both the cluster and the kubelets pulling their
images. The property this module exists to hold: **a job is created only when there is room
for it**, so nothing ever reaches the scheduler that the scheduler cannot place.

**It is a queue, not a per-caller reservation service, and that is the load-bearing decision.**
Every campaign runs on its own thread, so if each asked "may I go?" for itself the order would
be decided by which thread won the lock. A search campaign submits its batches one after
another, so ordering by submission makes an older campaign's later batches look younger than
a newer campaign, and the two end up taking turns instead of the older one finishing first.
Here the order is a
property of the queue -- ``(priority, campaign rank, campaign start)`` -- and no thread can
change it by being quick.

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

#: How long a capacity reading may be reused. Shorter than ``ServiceBase._USAGE_CACHE_TTL``
#: (10 s) because this gates a create loop rather than a status chip: a stale reading here
#: means either idle capacity or over-admission, where there it only means a slightly old
#: number on a screen.
BUDGET_TTL_S = 3.0

#: How a refusal for want of disk space begins, so a reader -- the batch loop putting it on
#: the campaign's status -- can tell it from a wait for a node. The rest is the reserve's own sentence (:mod:`robovast.common.disk_reserve`).
DISK_WAIT = "waiting for disk space: "

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
    be looked up: a batch runner deep in the controller can order itself against campaigns
    it has never heard of.

    Parsed **naively**, never through an epoch conversion: this only has to be monotone in
    the wall-clock label, and going via epoch seconds folds the repeated hour of a DST
    fall-back onto itself and inverts two campaigns' order.

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


def describe_resources(cpu: float, memory: int, ephemeral: int = 0) -> str:
    """``"4 cpu / 4096Mi"``, with ``" / 150Gi disk"`` where there is a disk figure.

    Disk is named only where it is asked for: most jobs request none, and a refusal that
    prints a disk figure of nothing sends the reader to check a dimension that is fine.
    """
    text = f"{cpu:g} cpu / {memory // (1024 ** 2)}Mi"
    return f"{text} / {ephemeral // (1024 ** 3)}Gi disk" if ephemeral else text


def _asked(sizing: "JobSizing") -> str:
    return describe_resources(sizing.cpu, sizing.memory, sizing.ephemeral)


def _held(capacity: "Capacity", sizing: "JobSizing") -> str:
    """What *capacity* holds, in the dimensions *sizing* asks for."""
    return describe_resources(capacity.cpu, capacity.memory,
                              capacity.ephemeral if sizing.ephemeral else 0)


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
    #: ``ephemeral-storage`` bytes. The scheduler places a pod on this as it does on cpu and
    #: memory, so a pin that ignores it sends a pod to a node that cannot take it: admission
    #: grants the node, the pin excludes every other one, and the pod waits Unschedulable
    #: for disk that another node had free.
    ephemeral: int = 0


@dataclass(frozen=True)
class Capacity:
    """What one node could hold if it were empty. Used only to answer "ever", never "now".

    *node_id* is optional and exists for ONE question: whether a **pinned** item could ever
    fit the node it is pinned to. "Ever" was a cluster-wide question while every item could
    go anywhere -- ``preflight`` only asks whether SOME node is large enough -- but a pinned
    item has exactly one candidate, and for it "no node is that large" and "that node is not
    that large" are different facts with different remedies. It stays optional because a
    caller that only asks the cluster-wide question has no use for it, and every existing
    construction site passes positionally.
    """
    cpu: float
    memory: int
    gpu: int = 0
    node_id: "str | None" = None
    ephemeral: int = 0

    def holds(self, sizing: JobSizing) -> bool:
        return (self.cpu >= sizing.cpu
                and self.memory >= sizing.memory
                and self.gpu >= sizing.gpu
                and self.ephemeral >= sizing.ephemeral)


@dataclass(frozen=True)
class NodeBudget:
    """What is free on ONE node right now.

    Per node rather than cluster-wide because a pod runs on one machine: a cluster with room
    in total and none on any single node is the state where jobs are admitted and then sit
    ``Unschedulable``: the free cores are spread across nodes and no single node
    holding the 4.75 a pod needed.

    *node_id* is this object's identity -- the same hash ``runs.node_label`` records, and the
    value ``robovast.io/node-id`` carries where the node is labelled. Every dict in the
    controller is keyed on it, so two nodes sharing one are a single node to the accounting
    and the other's capacity is offered to nobody. Never absent: a node with no label is
    still a node, and "no node at all" is a different question.

    *pinnable* is that other question: whether the identity is on the node as a label, so a
    ``nodeSelector`` can name it. An unpinnable node still holds work -- the job is created
    unpinned and kube-scheduler settles it.
    """
    node_id: str
    free_cpu: float
    free_memory: int
    free_gpu: int = 0
    free_ephemeral: int = 0
    pinnable: bool = True

    def holds(self, sizing: "JobSizing") -> bool:
        return (self.free_cpu >= sizing.cpu
                and self.free_memory >= sizing.memory
                and self.free_gpu >= sizing.gpu
                and self.free_ephemeral >= sizing.ephemeral)


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

    @property
    def free_ephemeral(self) -> int:
        return sum(n.free_ephemeral for n in self.nodes)


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
    #: The campaign this item belongs to, which is what its rank is read against. Distinct
    #: from *owner*, the cancellation scope: a campaign's probes queue under ``<campaign>#probes``
    #: so they stay out of its progress counts, and they must still rank with the campaign
    #: rather than as a stranger. Empty means the owner IS the campaign, which is true of a
    #: campaign's own trials.
    campaign: str = ""
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
    #: so placing it anywhere else answers a question about the wrong node; a campaign
    #: confined with ``execution.kubernetes.jobs.node`` may use its one node and no other.
    #: Everything else leaves it unset and is placed wherever it fits.
    pin: "str | None" = None

    #: Whether a pinned item that does not fit **claims its node** for the rest of the pass
    #: (see :meth:`AdmissionController.drain`). Meaningless without *pin*.
    #:
    #: ``True`` is right for a calibration probe: it is transient, one per node, and its node
    #: must drain for it or it is never placed. ``False`` is what a confined campaign submits,
    #: and it is not an optimisation. A campaign always has another job queued, so its claim
    #: would renew on every pass for the campaign's whole life: its node would take nothing
    #: from any lower-ranked campaign for as long as it ran, which is a denial of service
    #: against every other campaign, not a reservation.
    reserves: bool = True

    @property
    def ranks_under(self) -> str:
        """The campaign key this item's priority and pause are read from."""
        return self.campaign or self.owner

    def may_use(self, node: "NodeBudget") -> bool:
        """The pin AND the owner's gate, never either.

        A pin is a ``nodeSelector`` value, so it needs the label; a gate waits for a
        measurement, which an unpinnable node can never have.
        """
        if self.pin is not None:
            if not node.pinnable or node.node_id != self.pin:
                return False
        elif not node.pinnable:
            return True
        return self.accepts_node is None or self.accepts_node(node.node_id)

    def sizing_on(self, node_id) -> "JobSizing":
        """What this job needs *on that node*, falling back to what it declared."""
        if self.sizing_for_node is None or node_id is None:
            return self.sizing
        return self.sizing_for_node(node_id) or self.sizing


@dataclass
class _Held:
    """A granted reservation: charged against free capacity until its pod is observed.

    *node_id* is where it was granted, so the charge lands on the node that will carry it --
    including a node that could not be *pinned* to, where the grant is still the answer to
    "which node did we decide had room" even though kube-scheduler makes the final placement.
    The guess costs at most one budget TTL: the moment the pod is bound the reading counts it
    on its real node and this reservation stops being subtracted at all.

    ``None`` **only** for a job created unpinned on a growable cluster: it is charged nowhere,
    because it is going to a node that does not exist yet and charging an existing one would
    refuse work that node could still take. That is also what
    :meth:`AdmissionController._unpinned_outstanding_locked` counts, so anything else landing
    here would be read as work in flight towards a node the autoscaler has not produced.
    """
    owner: str
    cpu: float
    memory: int
    gpu: int
    node_id: "str | None" = None
    ephemeral: int = 0


class AdmissionRefused(Exception):
    """A sizing no node in this cluster could ever run. Permanent, so it raises.

    Distinct from "no room now", which is an ordinary answer (``drain`` simply creates
    nothing). Conflating the two is how a campaign ends up waiting forever for capacity that
    cannot exist, with no error anywhere.
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

    def __init__(self, provider: BudgetProvider, *, clock=None, budget_ttl: float = BUDGET_TTL_S,
                 space_gate: "Optional[Callable[[], Optional[str]]]" = None):
        self._provider = provider
        #: Asked before every drain: the sentence saying the disk the campaigns land on is
        #: below its free-space reserve, or ``None``. While it has one nothing is created --
        #: a Job admitted into a disk that cannot take its results computes them for
        #: nothing, and on a node-directory deployment the next write past the kubelet's
        #: eviction threshold evicts the service with every campaign it drives. Jobs
        #: already running are unaffected: the reserve is the room their results land in.
        self._space_gate = space_gate
        self._space_short: Optional[str] = None
        self._space_unmeasured_logged = False
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
        #: ``campaign -> priority``, absent meaning the default 0. Held here rather than
        #: copied onto every item so that changing it is one write that reaches the items
        #: already queued AND the batches a campaign has not submitted yet.
        self._priorities: "Dict[str, int]" = {}
        #: Campaigns admitting nothing. A paused campaign's created jobs run to completion --
        #: pausing orders the queue, exactly as priority does, and never stops work already
        #: placed.
        self._paused: "set" = set()
        self._seq = itertools.count()
        self._budget: Optional[Budget] = None
        self._budget_at = 0.0
        #: Per-node "could it EVER hold this", cached on the same TTL as the budget and read
        #: only when a pinned item does not fit -- which is rare, and is the one moment the
        #: question is worth a cluster read. ``capacities()`` lists the same nodes
        #: ``budget()`` does, so caching them together keeps the two answers from describing
        #: different clusters.
        self._capacities: "Optional[list]" = None
        self._capacities_at = 0.0
        #: ``owner -> why nothing was created for it last time``. Per owner, not one string:
        #: ``drain`` works the global queue, so a single slot was overwritten by whichever
        #: campaign's item happened to be next -- and campaign B would have read campaign A's
        #: job sizes as the reason for its own wait.
        self._refusals: "Dict[str, str]" = {}

    # -- queue -------------------------------------------------------------------------

    def submit(self, owner: str,
               items: "Iterable[Tuple[str, JobSizing, Callable[[Optional[str]], None]]]",
               *, started_at: float, priority: int = 0, campaign: str = "",
               sizing_for_node=None, accepts_node=None, pin=None,
               reserves: bool = True) -> int:
        """Enqueue a campaign's whole plan. Returns how many were accepted.

        *started_at* is the CAMPAIGN's start, not this batch's: a search submits batch after
        batch, and ordering by submission would let a newer campaign overtake an older one
        between its rounds.

        *campaign* is which campaign's rank these items take, and defaults to *owner*, which
        is what a campaign's own trials submit under. An owner that is a sub-scope of a
        campaign -- ``<campaign>#probes`` -- must name the campaign, or its work would rank as
        a stranger to the campaign it belongs to and a demoted campaign's probes would outrank
        its own runs.

        *priority* stays what it has always been: the ordering WITHIN a campaign, which puts
        a probe ahead of the work it gates. The campaign's
        own rank (:meth:`set_scheduling`) is the more significant key, so setting one never
        disturbs the other.

        *pin* restricts these items to one node. A calibration probe measures a particular
        machine, so placing it elsewhere answers a question about the wrong one -- and it
        waits for that node rather than settling for another, which is the opposite of how
        ordinary work is placed. It composes with *accepts_node* rather than replacing it:
        a pinned item goes to its node only once that node accepts its work.

        *reserves* is whether a pinned item that does not fit holds its node open against
        lower-ranked work -- see :attr:`WorkItem.reserves`. ``False`` for a campaign confined
        to one node, which always has more work queued and would otherwise hold that node
        for its whole life.

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
                                            campaign=campaign, priority=priority,
                                            started_at=started_at, seq=next(self._seq),
                                            sizing_for_node=sizing_for_node,
                                            accepts_node=accepts_node, pin=pin,
                                            reserves=reserves)
                added += 1
            return added

    def drain(self, *, limit: Optional[int] = None) -> int:
        """Create as many of the globally-highest-priority jobs as currently fit.

        Works the whole queue, not the caller's own items: that is what makes the ordering
        global while keeping this thread-free. Returns the number created.

        A job that does not fit is **skipped, not blocked behind** -- with mixed sizes a large
        job must not hold the cluster idle while smaller ones could run. Within a campaign the
        jobs are the same shape, so this costs nothing there.

        **A PINNED item is the exception, and it has to be.** Skipping assumes the item can be
        served later from somewhere; a pinned item has one candidate, so "later" only arrives
        if that node is left room. It is also the largest pod a calibrated campaign asks for --
        a probe runs at the declared sizing while the calibrated jobs behind it run at a
        measured fraction of it -- so skipping it hands its node to the smaller work it
        outranks, every pass, forever. Priority ordered the queue but reserved nothing, and on
        the smallest node of a mixed cluster that means a probe is never placed at all -- the
        campaign then ends having measured every node but that one.

        So a pinned item that does not fit **claims its node** for the rest of the pass:
        nothing further is placed there, the node drains as its work finishes, and the item
        goes on the pass where it fits. Only where the wait can end -- see
        :meth:`_could_ever_hold_locked` -- and only for an item that :attr:`~WorkItem.reserves`.
        A campaign confined to one node is pinned but does not reserve: it waits for room on
        its node like any other work, because a claim that renews for as long as it has jobs
        queued would shut every lower-ranked campaign out of that node for the campaign's life.

        On a growable cluster a job that fits no node may still be created unpinned, but only
        up to :data:`GROWTH_UNPINNED_LIMIT` of them at a time -- see there for why the cap is
        what keeps the autoscaler exception from being a hole.
        """
        created = 0
        with self._lock:
            self._record_paused_refusals_locked()
            pending = self._pending_in_order()
            if not pending:
                return 0
            short = self._check_space_locked()
            if short:
                for owner in {item.owner for item in pending}:
                    self._refusals[owner] = f"{DISK_WAIT}{short}"
                return 0
            nodes, growable = self._effective_free_locked(force=True)
            by_id = {n.node_id: n for n in nodes}
            unpinned = self._unpinned_outstanding_locked()
            failed: "List[WorkItem]" = []
            # Nodes a pinned item is waiting for. Built as the pass walks the queue in
            # priority order, so it only ever shuts out work that ranks BELOW the item
            # holding the node -- which is what makes it a reservation rather than a
            # cluster-wide stall.
            held_for_pin: "set" = set()
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
                        if item.may_use(n)
                        and (not n.pinnable or n.node_id not in held_for_pin)
                        and n.holds(item.sizing_on(n.node_id))]
                chosen = max(fits, key=lambda n: n.free_cpu) if fits else None
                if chosen is not None:
                    need = item.sizing_on(chosen.node_id)
                # Never for a pinned item: created unpinned, a calibration probe lands on any
                # node and its output is still recorded as the pinned node's measurement.
                may_grow = (item.pin is None and growable
                            and unpinned < GROWTH_UNPINNED_LIMIT)
                if chosen is None and not may_grow:
                    # **This owner's items, not the queue's.** The count spanned every owner,
                    # so a campaign with a handful of jobs queued was told the whole cluster's
                    # queue depth, reported into its own log as though it were its own.
                    # The refusal SLOT was made per owner for exactly this confusion; the
                    # number inside the string was not. `state` is mutated as items are
                    # created, so counting PLANNED here is accurate mid-pass.
                    own = sum(1 for i in pending
                              if i.owner == item.owner and i.state == PLANNED)
                    waiting = f"{own} job(s) waiting"
                    # Which of the two filters emptied the list, because they need opposite
                    # responses and the message is the only thing an operator sees. A node
                    # excluded by `may_use` is being measured, or is outside the configured
                    # pool -- reporting "no node has that free" over an idle cluster sent the
                    # reader to look for capacity that was never the problem. Observed saying
                    # "no node has that free (most free: 89 cpu)" for a job needing 4.25,
                    # while all four nodes were simply out for calibration.
                    usable = [n for n in by_id.values() if item.may_use(n)]
                    # What it would need on the node it would actually go to. `need` was left
                    # at the DECLARED sizing whenever nothing fit -- so a calibrated campaign
                    # was told its job needs the declared figure while the nodes it was being
                    # tested against had measured ones asking for substantially less. The fit
                    # test already used the per-node figure; only the message did not.
                    if usable:
                        emptiest = max(usable, key=lambda n: n.free_cpu)
                        need = item.sizing_on(emptiest.node_id)
                    if item.pin is None and growable:
                        self._refusals[item.owner] = (
                            f"{waiting}: {unpinned} already created for a node the "
                            f"autoscaler has not produced yet (limit "
                            f"{GROWTH_UNPINNED_LIMIT})")
                    elif not usable and item.pin is not None and item.pin in by_id:
                        # Pinned, and its one node is present but not accepting this work.
                        # Named as "its node" and never by id: the refusal reaches the
                        # campaign's log, which travels with its results.
                        self._refusals[item.owner] = (
                            f"{waiting}: the one node it may use is being measured before "
                            f"work is placed on it")
                    elif not usable and item.pin is not None:
                        self._refusals[item.owner] = (
                            f"{waiting}: the one node it may use is not among the "
                            f"{len(by_id)} node(s) this queue currently measures for work")
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
                    # **Claim the node, so the wait can end.** Only for a pinned item that
                    # reserves, only where the node could hold it empty, and only if nothing
                    # has claimed it already -- the first claimant is the highest-priority
                    # one, since the queue is walked in priority order, and a second claim on
                    # the same node would change nothing but the bookkeeping.
                    #
                    # `reserves` gates it because a confined campaign always has another job
                    # behind this one: its claim would renew every pass for its whole life and
                    # hold its node against every lower-ranked campaign. See WorkItem.reserves.
                    if item.pin is not None and item.reserves \
                            and item.pin not in held_for_pin \
                            and self._could_ever_hold_locked(item.pin, item.sizing_on(item.pin)):
                        held_for_pin.add(item.pin)
                        self._refusals[item.owner] = (
                            f"{self._refusals.get(item.owner, '')} -- holding that node open "
                            f"for it, so nothing further is placed there until it drains")
                    continue
                try:
                    # ``None`` means create unpinned, and there are two ways to get here: a
                    # growable cluster whose room is not on any node yet, and a node that has
                    # no identity label to select it by. Both are "we know there is room, we
                    # cannot name where" -- and in both the scheduler places it, which is
                    # exactly what happened before per-node admission existed.
                    item.create(chosen.node_id if chosen and chosen.pinnable else None)
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
                # The node the grant is CHARGED to, which is not the same question as the
                # node the pod was pinned to: an unpinnable node still gets the charge, so
                # the rest of this pass does not hand its cores out twice. ``None`` here is
                # reserved for the growable case, where there is genuinely no node yet.
                node_id = chosen.node_id if chosen else None
                self._held[item.key] = _Held(item.owner, need.cpu, need.memory, need.gpu,
                                             node_id, need.ephemeral)
                if chosen is None:
                    unpinned += 1
                else:
                    by_id[node_id] = NodeBudget(
                        node_id=node_id,
                        free_cpu=chosen.free_cpu - need.cpu,
                        free_memory=chosen.free_memory - need.memory,
                        free_gpu=chosen.free_gpu - need.gpu,
                        free_ephemeral=chosen.free_ephemeral - need.ephemeral,
                        pinnable=chosen.pinnable)
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
        """Drop an owner's items and release its held reservations; return how many were
        still planned.

        Only the planned ones are counted, because that is the number a caller can report as
        released: work that will now never exist. A created item is a Job that exists or
        existed, and counting it would have a batch stopped mid-run report its running jobs as
        released "never created".

        Called from a ``finally``, because a campaign that raises on its way out would
        otherwise leak its reservations for the life of the process -- shrinking every other
        campaign's usable capacity, invisibly and cumulatively.

        **The calibration survives, and that is the point.** This runs at the end of every
        BATCH -- a search builds a fresh runner per batch -- so dropping the calibration here
        would make every batch re-probe every node, and since a figure moves between probes,
        runs in different batches of the same campaign would be sized differently. That
        defeats the property calibration exists to provide, which is that every run of a
        campaign meets the same allocation.
        :meth:`forget_calibration` is what ends it, at the end of the campaign.
        """
        with self._lock:
            keys = [k for k, i in self._items.items() if i.owner == owner]
            planned = sum(1 for k in keys if self._items[k].state == PLANNED)
            for key in keys:
                self._items.pop(key, None)
                self._held.pop(key, None)
            for key in [k for k, h in self._held.items() if h.owner == owner]:
                self._held.pop(key, None)
            self._refusals.pop(owner, None)
            return planned

    def drop_planned(self, owner: str) -> "List[str]":
        """Drop *owner*'s items that are not created yet, keeping what it already holds.

        For an owner that has nothing left for a planned item to do. A planned item keeps its
        place in the global queue and is created the moment room appears, so leaving it there
        spends a node's capacity on work whose submitter has no use for the result.

        Not :meth:`cancel`, which also releases what the owner's CREATED items hold -- those
        are pods that exist, and forgetting their reservation would let the queue spend the
        same capacity twice.
        """
        with self._lock:
            keys = [k for k, i in self._items.items()
                    if i.owner == owner and i.state == PLANNED]
            for key in keys:
                self._items.pop(key, None)
            if not any(i.owner == owner for i in self._items.values()):
                # A reason that outlived the wait it described reads to an operator as an
                # owner still stuck -- the same defect a create already clears.
                self._refusals.pop(owner, None)
            return keys

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

    def set_scheduling(self, campaign: str, *, priority=None, paused=None) -> None:
        """Set how the queue treats *campaign*: its rank, whether it admits at all, or both.

        Takes effect on the next :meth:`drain`, and applies to the items already queued as
        well as to the batches the campaign has not submitted yet -- which is why the pair is
        held per campaign here rather than copied onto each item as it is enqueued.

        Neither setting stops work already created. A campaign demoted or paused keeps the
        jobs it has until they finish, and gives up only the slots they release: the queue
        orders admission and has never been able to take a running job back, which is what
        makes both safe to use on a campaign whose results matter.

        ``None`` leaves that half alone, so a pause does not disturb the rank it will come
        back at.
        """
        with self._lock:
            if priority is not None:
                self._priorities[campaign] = int(priority)
            if paused is not None:
                if paused:
                    self._paused.add(campaign)
                else:
                    self._paused.discard(campaign)

    def scheduling(self, campaign: str) -> "Tuple[int, bool]":
        """``(priority, paused)`` for *campaign* -- the default ``(0, False)`` when unset."""
        with self._lock:
            return self._priorities.get(campaign, 0), campaign in self._paused

    def forget_scheduling(self, campaign: str) -> None:
        """Drop a campaign's rank and pause, once it is over.

        A campaign's lifetime, like :meth:`forget_calibration` and unlike :meth:`cancel` --
        the rank has to survive the end of each batch, since a search submits batch after
        batch under the same campaign and would otherwise come back at the default halfway
        through. Kept separate from the calibration for the reason recorded there: one call
        that means two lifetimes is how the probe leak got in.
        """
        with self._lock:
            self._priorities.pop(campaign, None)
            self._paused.discard(campaign)

    def node_ids(self) -> list:
        """The identity of every node that can currently be pinned to.

        Whoever starts calibration probes needs to know which machines exist, and this is
        already measuring them every cycle. Excludes a node with no identity label: it can
        hold work but cannot be selected, so probing it would produce a figure nothing could
        ever be pinned to.
        """
        with self._lock:
            nodes, _ = self._effective_free_locked()
            return sorted(n.node_id for n in nodes if n.pinnable)

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

    def preflight(self, sizing: JobSizing, node_id: "str | None" = None) -> None:
        """Raise if no node could ever run this, however empty the cluster gets.

        With *node_id*, the question is about that one node: a campaign confined to it can
        use no other, so "some node is large enough" says nothing about whether it will ever
        run. It matters more there than for a probe, because a confined campaign does not
        claim its node (:attr:`WorkItem.reserves`) and so has no drain-side guard either --
        a job its node could never hold would simply never be placed. Permissive when the
        provider carries no node ids, as :meth:`_could_ever_hold_locked` is: that is an
        unknowable answer, not a verdict. A provider that does carry them and does not list
        the node is refused, since nothing could then be placed on it.

        Checked once before a batch is enqueued. Without it a campaign sits in the admit loop
        forever having created **zero** jobs, and every diagnosis path downstream is pod-based
        and therefore blind to it.

        A cluster with **no nodes at all** is the one case a growable cluster is excused,
        because there is then nothing to judge a size against. A pool scaled to zero is the
        ordinary resting state of an autoscaled cluster, and refusing a batch there would make
        that state permanent: the pending pods are what would have grown it. ``drain`` places
        such an item unpinned, under its own limit, so passing here hands out no unbounded plan.

        A cluster that **has** nodes is judged by them even when it can grow, and that is
        deliberate. An autoscaler adds machines from a pool of some fixed shape, so on the
        ordinary homogeneous cluster "no node is that large" stays true however many are
        added -- and excusing it would trade a loud, immediate refusal for a campaign that
        waits forever having created zero jobs, which is the exact failure this check exists
        to prevent. The cost is a heterogeneous cluster whose one large pool is scaled to
        zero: a job only that pool could hold is refused, naming the biggest node currently
        present. Loud and wrong beats silent and stuck, and the message says what it measured.
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
        if node_id is not None and any(getattr(c, "node_id", None) for c in capacities):
            # Named as "the node it is confined to", never by id: this becomes a campaign
            # error, which the campaign's record carries.
            own = [c for c in capacities if getattr(c, "node_id", None) == node_id]
            if not own:
                raise AdmissionRefused(
                    "the node this campaign is confined to (execution.kubernetes.jobs.node) "
                    "is not among the nodes this cluster offers for campaign jobs, so none "
                    "of its jobs could ever be placed. Check that the node is ready and "
                    "inside the job node pool.")
            if own[0].holds(sizing):
                return
            raise AdmissionRefused(
                f"a job needs {_asked(sizing)} and the node this campaign is confined to "
                f"(execution.kubernetes.jobs.node) holds {_held(own[0], sizing)}. Reduce "
                "execution.containers.*.resources, or confine it to a larger node.")
        if any(c.holds(sizing) for c in capacities):
            return
        if not capacities:
            if self.growable():
                return
            raise AdmissionRefused(
                "no nodes are available to size against; the cluster reported none")
        # Largest in the dimension that does not fit: a disk refusal that quoted the node
        # with the most cores would name a disk figure smaller than the cluster's largest.
        disk_short = sizing.ephemeral and not any(c.ephemeral >= sizing.ephemeral
                                                  for c in capacities)
        biggest = max(capacities, key=(lambda c: c.ephemeral) if disk_short
                      else (lambda c: c.cpu))
        raise AdmissionRefused(
            f"a job needs {_asked(sizing)} and no node is that large -- the biggest holds "
            f"{_held(biggest, sizing)}. Reduce execution.containers.*.resources, or run "
            "where a node can hold it.")

    def _could_ever_hold_locked(self, node_id, sizing: JobSizing) -> bool:
        """Could *node_id* run *sizing* if it were empty? ``True`` when unknowable.

        The guard on the pin reservation in :meth:`drain`. Holding a node open for a pinned
        item that could never fit it drains that machine and keeps it drained -- strictly
        worse than not reserving at all, because today such a node at least runs other
        campaigns' work. So the reservation is only taken where waiting can actually end.

        **Unknowable answers ``True``**, deliberately: a provider that does not carry node
        ids, or a node absent from the capacity reading, must not silently disable the
        reservation. Erring towards reserving keeps the behaviour the same for every existing
        provider, and the batch limit in ``node_calibration`` is the backstop that stops a
        wrong ``True`` from lasting more than a batch.
        """
        now = self._clock()
        if self._capacities is None or now - self._capacities_at >= self._budget_ttl:
            try:
                self._capacities = list(self._provider.capacities() or [])
            except Exception:  # noqa: BLE001 - a failed read is not a verdict
                self._capacities = None
                return True
            self._capacities_at = now
        for capacity in self._capacities:
            if getattr(capacity, "node_id", None) == node_id:
                return capacity.holds(sizing)
        return True

    def space_shortfall(self) -> Optional[str]:
        """Why nothing is admitted for want of disk space, as of the last drain, or ``None``."""
        with self._lock:
            return self._space_short

    def refusal(self, owner: str) -> str:
        """Why nothing was created for *owner* last time, for its campaign's log.

        ``""`` when the last drain had nothing to refuse it. The caller decides how often to
        say it; this only records the most recent answer.
        """
        with self._lock:
            return self._refusals.get(owner, "")

    # -- internals ---------------------------------------------------------------------

    def _check_space_locked(self) -> Optional[str]:
        """Ask the space gate, and remember its answer for :meth:`space_shortfall`.

        A gate that raises is not a full disk: admission goes on, and the failure is logged
        once rather than every drain. Holding every campaign because free space could not be
        measured would make a launch depend on a reading it never needed.
        """
        if self._space_gate is None:
            return None
        try:
            short = self._space_gate()
        except Exception as e:  # noqa: BLE001 - unmeasured is not full; see above
            if not self._space_unmeasured_logged:
                logger.warning("free space could not be measured, so admission goes on "
                               "without the reserve: %s", e)
                self._space_unmeasured_logged = True
            self._space_short = None
            return None
        self._space_unmeasured_logged = False
        if short and short != self._space_short:
            logger.warning("admitting no Jobs: %s", short)
        elif not short and self._space_short:
            logger.info("admitting Jobs again: the disk is above its reserve")
        self._space_short = short or None
        return self._space_short

    def _record_paused_refusals_locked(self) -> None:
        """Say *paused* for every owner holding back, rather than letting it read as a wait.

        A paused campaign is filtered out before the placement walk, so it would otherwise
        keep whatever its last drain said -- "no node has that free" -- and a campaign nobody
        is admitting would be indistinguishable from a campaign the cluster is too full for.
        Those need opposite responses: one is waiting for a machine, the other for a person.
        """
        for owner in {i.owner for i in self._items.values()
                      if i.state == PLANNED and i.ranks_under in self._paused}:
            own = sum(1 for i in self._items.values()
                      if i.owner == owner and i.state == PLANNED)
            self._refusals[owner] = (
                f"paused: {own} job(s) held, and nothing is admitted until it is resumed. "
                f"Jobs already running are unaffected.")

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
        """Priority first, then campaign rank, then oldest campaign, then submission order.

        ``priority`` leads, and it has to. It is not a preference but a campaign's own
        sequence: a calibration probe measures the node its work will be sized from. Probes
        are bounded -- a few per campaign, short -- and they are *preconditions*, so ranking
        ordinary work ahead of them does not make the queue fairer, it makes the campaign
        behind them fail.
        A demoted campaign whose probe keeps losing its node is refused outright after
        ``UNMEASURED_BATCH_LIMIT`` batches, so a rank that reached its probes would turn
        "let other campaigns past" into "end this campaign", which is not what anyone setting
        it asked for.

        The campaign's rank comes next, and is what a person actually sets: it orders the
        *runs*, which is where a campaign spends all but a moment of its time and the whole of
        what another campaign is waiting for.

        ``started_at`` before ``seq`` is the whole point: sequence is when this *batch* was
        enqueued, and an older campaign's second batch must still beat a younger campaign's
        first.

        A paused campaign is absent entirely: it is not a low rank but no rank, so nothing of
        it is created however idle the cluster is.
        """
        return sorted((i for i in self._items.values()
                       if i.state == PLANNED and i.ranks_under not in self._paused),
                      key=lambda i: (-i.priority,
                                     -self._priorities.get(i.ranks_under, 0),
                                     i.started_at, i.seq))

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
        free = {n.node_id: [n.free_cpu, n.free_memory, n.free_gpu, n.free_ephemeral,
                            n.pinnable]
                for n in budget.nodes}
        if len(free) != len(budget.nodes):
            # Said out loud rather than absorbed: nodes sharing an id are ONE node to
            # everything below, so the difference is capacity that is silently never offered.
            # See :class:`NodeBudget` -- ``None`` for every unlabelled node is how this
            # happened, and it cost a whole cluster's worth of throughput without a log line.
            logger.warning("the budget reports %d node(s) under %d distinct id(s); nodes "
                           "sharing an id are counted once and the rest of their capacity is "
                           "not offered", len(budget.nodes), len(free))
        for key, held in self._held.items():
            if key in budget.counted_jobs:
                continue  # the reading already subtracted its real pod
            if held.node_id not in free:
                continue  # unpinned, or a node that has since gone away
            free[held.node_id][0] -= held.cpu
            free[held.node_id][1] -= held.memory
            free[held.node_id][2] -= held.gpu
            free[held.node_id][3] -= held.ephemeral
        return ([NodeBudget(node_id=k, free_cpu=v[0], free_memory=v[1], free_gpu=v[2],
                            free_ephemeral=v[3], pinnable=v[4])
                 for k, v in free.items()], budget.growable)
