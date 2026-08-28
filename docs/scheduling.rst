.. _scheduling:

Scheduling reference
====================

The four modules that decide **when** a campaign's jobs are created, **where** they run and
**how big** they are. This page is the map and the interface; the reasoning lives elsewhere
and is not repeated here:

* :ref:`cluster-admission` — what an operator sees and configures.
* :doc:`developer_guide` §*Job admission internals* — why creation is the admission point,
  and why the queue is global rather than per caller.

.. contents::
   :local:
   :depth: 1


The modules
-----------

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Module
     - Answers
   * - ``node_admission``
     - *When.* The global queue, the in-flight ledger, and the create-on-admit loop.
   * - ``cluster_capacity``
     - *How much room.* Reads the live cluster; the only thing that talks to the API for
       capacity.
   * - ``node_placement``
     - *Where.* Node identity labels, the campaign node pool, the taint job pods tolerate.
   * - ``node_calibration``
     - *How big.* Per-node sizing measured by a probe run, per campaign.

They are layered, not circular: ``node_admission`` knows only the
:class:`~robovast.execution.cluster_execution.node_admission.BudgetProvider` protocol, and
never a Kubernetes object. That is what lets the whole queue be tested without a cluster.


The interface
-------------

**What a job needs, and what a node has.** Both frozen, both plain numbers::

    JobSizing(cpu: float, memory: int, gpu: int = 0)      # summed over a pod's containers
    Capacity(cpu, memory, gpu)                            # what one node holds when empty
    NodeBudget(node_id, free_cpu, free_memory, free_gpu)  # what one node has free NOW
    Budget(nodes: tuple[NodeBudget], counted_jobs: frozenset, growable: bool)

``counted_jobs`` is the double-counting fix: the provider reports which Jobs its reading
already saw, so a reservation stops being subtracted the instant the real pod starts being
subtracted. No timers, and no window where cores are counted twice or not at all.

**The seam.** One protocol, two methods, and the reason multi-tenancy can be added later
without touching the loop::

    class BudgetProvider(Protocol):
        def budget(self) -> Budget: ...        # free NOW  — excludes unusable nodes
        def capacities(self) -> list[Capacity]: ...  # could EVER — includes them

The asymmetry is deliberate and was a bug when it was absent. "Free now" must exclude a node
that is cordoned, ``NotReady`` or carrying an untolerated taint, because counting it promises
room that cannot be spent. "Could ever" must include it, because
:meth:`~robovast.execution.cluster_execution.node_admission.AdmissionController.preflight`
raises a *permanent* error and a rebooting node is coming back. Both take the per-node
headroom off: a reserve that is never spendable is not part of either answer.

**The controller.** Everything a campaign does with the queue:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Call
     - Meaning
   * - ``submit(owner, items, *, started_at, priority=0, sizing_for_node=None, accepts_node=None, pin=None)``
     - Enqueue a whole plan. ``items`` are ``(key, JobSizing, create_fn)``; ``create_fn``
       takes the chosen ``node_id`` (or ``None`` when unpinned). ``started_at`` is the
       **campaign's** start, not the batch's.
   * - ``drain(*, limit=None) -> int``
     - Create as many globally-highest-priority items as currently fit. Returns how many.
       ``0`` means "nothing fits now" — normal, never an error.
   * - ``finished(key)``
     - Release that job's reservation.
   * - ``cancel(owner) -> int``
     - Drop an owner's planned items and release its holds. Runs per **batch**.
   * - ``forget_calibration(owner) -> bool``
     - End a campaign's per-node figures. Runs per **campaign**.
   * - ``preflight(sizing)``
     - Raise ``AdmissionRefused`` when no node could *ever* hold it.
   * - ``states(owner) -> dict``
     - ``key -> PLANNED | CREATED``, for progress reporting.
   * - ``refusal(owner) -> str``
     - One line saying what this owner is waiting for.
   * - ``calibration(owner, factory=None)``
     - The campaign's :class:`NodeCalibration`, created once and kept for its life.
   * - ``node_ids()`` / ``growable()``
     - Pinnable node identities; whether the cluster can add nodes.

``cancel`` and ``forget_calibration`` are separate because their lifetimes are: reservations
belong to a batch, calibration to a campaign. Merging them made a search re-probe every node
every round.


Workflows
---------

**A batch.** The campaign thread owns no admit loop — it enqueues, then polls its own items::

    submit(campaign, plan, started_at=..., priority=0)
    while True:
        reap     -> finished(key) for each vanished CREATED job
        exit     -> if every plan entry is FINISHED: break
        drain()  -> may create OTHER campaigns' jobs; that is the point
        publish  -> waiting_for_capacity from states(), not from pods
        sleep 2
    finally:
        cancel(campaign)                # and cancel(campaign#probes)

Step 4 is what makes ordering global without a controller thread: whichever campaign happens
to be awake advances everybody, in ``(priority, campaign start)`` order.

Two traps the loop must respect, both of which cost a live campaign when they were missed:

* **Only CREATED names may be asked about.** ``get_remaining_jobs`` treats a 404 as finished,
  which is right for a reaped Job and catastrophic for one not yet created — the batch
  "finishes" on its first poll having produced nothing.
* **``waiting_for_capacity`` comes from ``states()``, not from pods.** A queued campaign has
  no pods, so a pod-based probe cannot see it and the stall verdict fires on a healthy
  campaign.

**A campaign with per-node sizing.** The probe is one ordinary run, pinned, at the declared
sizing, before any work is placed on that node::

    batch 0:  for each uncalibrated node -> claim_probe(node) -> submit(campaign#probes, pin=node)
              accepts_work(node) is False while its probe is out
              probe finishes -> record(node, measured, completed=<scenario reached a verdict>)
                             -> node accepts work, sized from its own figures
    batch 1+: every node already calibrated -> no probes, work placed immediately
    campaign end: forget_calibration(campaign)

Probes queue under a **second owner** (``<campaign>#probes``) so they stay out of the
campaign's progress counts — which means the batch's ``finally`` must cancel both, or a probe
still ``PLANNED`` holds its node out of the campaign for good.

A probe that dies without a verdict is abandoned: the node stays uncalibrated and its runs use
the declared sizing, which is what a cluster with calibration switched off does anyway — a
worse allocation, never a wrong result.

**Sizing is per role**, and this is a validity rule rather than a tuning one:

* the **system under test** takes the measured peak as request *and* limit, so its budget is
  one it never throttles against;
* everything else takes the sustained figure as its request and keeps its declared ceiling,
  because a simulator's peak-to-mean ratio is ~18 and reserving its peak would cost more than
  no calibration at all;
* memory is never re-sized — it does not vary with machine speed, and exceeding a memory limit
  is an OOM kill rather than a slowdown;
* a calibrated figure is **clamped to the declared value**. Calibration sizes a node's jobs
  down to what they need; raising a ceiling the author set is not what it is for, and
  ``preflight`` ran once on the declared sizing and is never re-asked per node.


Failure modes worth knowing
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Symptom
     - Cause
   * - Campaign creates nothing, forever
     - A sizing that fits ``allocatable`` but not ``allocatable - headroom``. ``preflight``
       now takes the reserve off, so this raises instead of hanging.
   * - Runs discarded in a loop
     - A dead node read as fully free once its pods were evicted. ``budget()`` now excludes
       unschedulable nodes.
   * - The whole batch created at once
     - Every item "fits" a growable cluster. Bounded by ``GROWTH_UNPINNED_LIMIT``; a handful
       of unplaceable pods signals an autoscaler as loudly as a thousand.
   * - Per-node sizing silently off
     - ``growable`` compared an autoscaler maximum for the whole cluster against a
       pool-filtered reading, so configuring a node pool made it permanently true.
   * - A create retried forever
     - An RBAC change, a webhook or a quota looks identical to an API blip from here.
       Bounded, then dropped with its cause.


Where the numbers come from
---------------------------

Nothing is remembered between deployments. Capacity is measured each cycle from what the nodes
advertise, minus bound pod requests, minus the per-node headroom
(``ROBOVAST_NODE_HEADROOM_CPU`` / ``ROBOVAST_NODE_HEADROOM_MEMORY``, set on the service
Deployment). Calibration is measured per campaign and deliberately never inherited by the next
one — figures taken under one campaign's contention describe a load the next never meets.

That is also why there is no cached per-node factor anywhere: measured across two campaigns on
the same cluster, a container's cost per simulated second moved up to 40% and the ranking
between nodes inverted, so a factor that transfers does not exist.
