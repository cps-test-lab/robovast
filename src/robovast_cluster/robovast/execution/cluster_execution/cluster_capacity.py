# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""How much room the cluster has, measured rather than remembered.

The :class:`~.node_admission.BudgetProvider` the admission queue reads. It answers two
questions and keeps them apart on purpose:

* **free now** -- node ``allocatable`` minus the requests of every pod actually bound to a
  node, minus a reserve. What may be handed out this instant.
* **could ever** -- what each node would hold if it were empty. Only used to tell "wait" from
  "impossible", and it must stay that question: a request larger than any node is a
  configuration error to raise on, while a request larger than what is *free* is an ordinary
  wait.

**Measured every time, never accumulated.** A counter that tracks admissions and completions
drifts -- against evictions, node drains, a campaign killed mid-flight, and anything running
in the cluster that is not ours. Reading the truth costs two list calls and cannot drift, so
completion and eviction need no handling at all: the next reading simply does not see the pod.

``allocatable``, not ``capacity``: the former is what the scheduler may hand out, after the
kubelet's own reservations. Sizing against ``capacity`` would promise cores that were never
available to workloads.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional, Tuple

from .kube_client import parse_resource, pod_workload_containers
from .node_admission import Budget, Capacity, NodeBudget

logger = logging.getLogger(__name__)

#: Held back on the node RoboVAST's own transient work runs on -- the build daemon, which
#: bursts past its own request, and the aux composition and ``exec_in_container`` pods, which
#: prefer that node -- so a campaign that filled the cluster exactly does not squeeze it out.
#: Where no node is labelled the build node that work can land anywhere, and every node keeps
#: the reserve (:func:`reserved_node_names`).
#:
#: **Not a ``.vast`` knob.** The reserve protects tenants no single campaign owns, so letting
#: one campaign shrink it would let it take capacity every other campaign depends on.
HEADROOM_CPU_ENV = "ROBOVAST_NODE_HEADROOM_CPU"
HEADROOM_MEMORY_ENV = "ROBOVAST_NODE_HEADROOM_MEMORY"
DEFAULT_HEADROOM_CPU = "1"
DEFAULT_HEADROOM_MEMORY = "2Gi"


#: What this cluster may grow to, written into the service's environment at ``setup`` and
#: ``upgrade`` rather than queried from inside the pod.
#:
#: The figure decides :meth:`ClusterBudgetProvider._growable`, and its only other source is a
#: provider's ``get_cluster_allocatable_resources`` -- which on every cloud provider means a
#: CLI the service image does not ship. That call therefore never answers where it is read,
#: so the cluster looked static and admission held it at the size it happened to be. Setup
#: runs on a workstation that *does* have the provider's tooling, so it asks there and records
#: the answer here, where the pod can read it.
#:
#: The cost of recording rather than querying is that the figure ages: resizing a node pool
#: does not reach a running deployment. Re-run ``vast cluster upgrade`` after such a change,
#: the same lifecycle the node identity labels already have.
MAX_CPU_ENV = "ROBOVAST_CLUSTER_MAX_CPU"
MAX_MEMORY_ENV = "ROBOVAST_CLUSTER_MAX_MEMORY"


def recorded_maximum() -> "Tuple[Optional[float], Optional[int]]":
    """The recorded growth ceiling as ``(cpu, memory)``, or ``(None, None)`` when unset.

    Unparseable raises rather than reading as "unset": a typo would leave an elastic cluster
    silently pinned to its current size, and the symptom -- a campaign that never grows the
    cluster -- points nowhere near the cause. Same policy as :func:`headroom`, for the same
    reason.
    """
    raw_cpu = (os.environ.get(MAX_CPU_ENV) or "").strip()
    raw_mem = (os.environ.get(MAX_MEMORY_ENV) or "").strip()
    if not raw_cpu or not raw_mem:
        return None, None
    cpu = parse_resource(raw_cpu)
    mem = int(parse_resource(raw_mem))
    if not cpu or not mem:
        raise ValueError(
            f"{MAX_CPU_ENV}={raw_cpu!r} {MAX_MEMORY_ENV}={raw_mem!r}: not resource "
            "quantities. Use e.g. '256' and '1024Gi'.")
    return cpu, mem


def _requests(container) -> dict:
    """A container spec's resource requests, ``{}`` where it declares none."""
    return (container.resources.requests if container.resources else None) or {}


def headroom() -> "Tuple[float, int]":
    """The cluster-wide reserve, from the service's environment.

    Unparseable raises rather than falling back: a typo that silently became "no headroom"
    would over-admit on every node, and the symptom -- occasional unschedulable pods under
    load -- points nowhere near the cause. Empty is the default, not zero: setup writes the
    variable into the Deployment on every deploy, stated even when the operator named
    nothing, and an unnamed reserve must stay the reserve rather than vanish.

    ``0`` is a value, and a legitimate one: on a single node whose control plane already sits
    in the requests admission subtracts, the tenants the reserve protects are counted once
    already.
    """
    from robovast.common.quantity import to_bytes, to_cores  # noqa: PLC0415

    raw_cpu = (os.environ.get(HEADROOM_CPU_ENV) or "").strip() or DEFAULT_HEADROOM_CPU
    raw_mem = (os.environ.get(HEADROOM_MEMORY_ENV) or "").strip() or DEFAULT_HEADROOM_MEMORY
    # The strict parsers, which answer ``None`` for what is not a quantity, where
    # ``parse_resource`` would answer ``0`` -- and ``0`` is the one value this must never
    # invent.
    cpu = to_cores(raw_cpu)
    mem = to_bytes(raw_mem)
    if cpu is None or mem is None:
        raise ValueError(
            f"{HEADROOM_CPU_ENV}={raw_cpu!r} {HEADROOM_MEMORY_ENV}={raw_mem!r}: not resource "
            "quantities. Use e.g. '1' and '2Gi'.")
    return float(cpu), int(mem)


def reserved_node_names(nodes) -> "Optional[set]":
    """The names of the nodes the reserve is held on, or ``None`` for every node.

    The build node, labelled by setup, is where the transient work lands. A cluster with no
    such label has placed that work nowhere in particular, so the reserve stays on every node
    -- not a fallback, but the same rule: hold it where the work can run.
    """
    from .node_placement import BUILD_NODE_LABEL, LABEL_VALUE  # noqa: PLC0415

    labelled = {node.metadata.name for node in nodes
                if (node.metadata.labels or {}).get(BUILD_NODE_LABEL) == LABEL_VALUE}
    return labelled or None


def _reserve_on(name, reserved) -> "Tuple[float, int]":
    """``(cpu, memory)`` held back on node *name*: the reserve, or nothing.

    One rule for both readings, as the reserve itself is (see ``capacities``).
    """
    return headroom() if reserved is None or name in reserved else (0.0, 0)


#: The kubelet's own default hard-eviction threshold for its node filesystem
#: (``evictionHard: nodefs.available``), taken where a node's configuration cannot be read.
DEFAULT_NODEFS_EVICTION = "10%"


def eviction_threshold_bytes(value: str, capacity_bytes: int) -> int:
    """``nodefs.available`` as bytes on a filesystem of *capacity_bytes*: ``5%`` or ``10Gi``."""
    from robovast.common.quantity import to_bytes  # noqa: PLC0415

    text = str(value).strip()
    if text.endswith("%"):
        return int(capacity_bytes * float(text[:-1]) / 100)
    parsed = to_bytes(text)
    if parsed is None:
        raise ValueError(f"nodefs.available={value!r} is neither a percentage nor a quantity")
    return int(parsed)


class ClusterBudgetProvider:
    """Reads the live cluster. The only implementation today; see ``BudgetProvider``."""

    def __init__(self, core_api_factory, *, node_selector: "Optional[dict]" = None,
                 cluster_config=None, kube_context=None):
        self._core_api_factory = core_api_factory
        self._node_selector = node_selector or {}
        # An autoscaling cluster knows a size it is not currently at. See _declared_total.
        self._cluster_config = cluster_config
        self._kube_context = kube_context
        #: ``node name -> nodefs.available`` as the kubelet is configured; it changes only
        #: with the node's kubelet configuration, so it is read once per node.
        self._eviction_setting: dict = {}

    # -- the two questions -------------------------------------------------------------

    def capacities(self) -> "List[Capacity]":
        """What each node could hold if it were empty -- **headroom already taken off**.

        Subtracted here for the same reason it is subtracted from ``budget()``: the reserve is
        never spendable by a campaign, so a reserved node's usable size is
        ``allocatable - headroom`` in **both** answers, and they must agree. If this reported
        the raw figure, a sizing above ``allocatable - headroom`` but at or below
        ``allocatable`` would pass
        :meth:`~.node_admission.AdmissionController.preflight` -- which exists precisely to
        tell "wait" from "impossible" -- and then no drain could ever place it. The campaign
        would sit in the admit loop having created ZERO jobs, invisible to every diagnosis
        path downstream, all of which read pods. The calibration probe cannot rescue it
        either: it runs at the declared sizing and is pinned, so it is unadmittable for the
        same reason.

        Never below zero: a node smaller than the reserve reports as holding nothing, which is
        the truth, rather than a negative that would read as room.
        """
        ids = self._node_identities()
        # Carrying the node id, so a PINNED item can be asked whether the one node it may use
        # could ever hold it. Without it the only answerable question is the cluster-wide one,
        # and a probe pinned to the smallest machine of a mixed cluster is judged against the
        # biggest.
        reserved = self._reserved_nodes()
        out = []
        for name, a in self._allocatables().items():
            head_cpu, head_mem = _reserve_on(name, reserved)
            out.append(Capacity(cpu=max(0.0, parse_resource(a.get("cpu")) - head_cpu),
                                memory=max(0, int(parse_resource(a.get("memory"))) - head_mem),
                                gpu=int(parse_resource(a.get("nvidia.com/gpu"))),
                                node_id=ids.get(name, (None, False))[0],
                                ephemeral=int(parse_resource(a.get("ephemeral-storage")))))
        return out

    def _reserved_nodes(self) -> "Optional[set]":
        """:func:`reserved_node_names` over the live node list, read once per reading."""
        return reserved_node_names(self._core_api_factory().list_node().items)

    def _declared_total(self):
        """The cluster's own idea of how big it can get, or ``None``.

        **This is what keeps an autoscaling cluster able to grow.** A provider such as GKE
        reports its autoscaler's *max*, which is larger than the nodes that exist right now.
        Sizing admission to the current nodes instead would be quietly self-defeating: pods
        that cannot be placed are exactly what makes an autoscaler add a node, so a scheduler
        that never creates them keeps the cluster at whatever size it happens to be. The
        effect is visible only on a cluster with an autoscaler, which is why it is stated
        here rather than left to be rediscovered.

        It qualifies the "never create what cannot be placed" rule rather than breaking it:
        the rule is about what can *never* be placed, and on an autoscaler "not yet" is not
        never.

        Failure is an absence, not an error -- a cluster that cannot answer should fall back
        to counting its nodes, not stop admitting.

        Read from the environment first (:data:`MAX_CPU_ENV`), because that is the only source
        that answers in the pod this runs in: a provider override reaches for the cloud's CLI,
        which the service image does not carry. The provider is still asked when nothing was
        recorded, so a driver running on a workstation keeps the live figure.
        """
        cpu, memory = recorded_maximum()
        if cpu and memory:
            return cpu, memory
        config = self._cluster_config
        if config is None or not hasattr(config, "get_cluster_allocatable_resources"):
            return None
        try:
            cpu, memory = config.get_cluster_allocatable_resources(self._kube_context)
        except Exception:  # noqa: BLE001 - see docstring
            logger.debug("cluster_config could not report its maximum size", exc_info=True)
            return None
        if not cpu or not memory:
            return None
        return parse_resource(cpu), int(parse_resource(memory))

    def budget(self) -> Budget:
        """Free capacity **per node**, and the Jobs this reading already accounts for.

        Per node because a pod runs on one machine. A cluster-wide figure cannot see
        fragmentation, and admitting against it is how jobs end up ``Unschedulable`` while the
        cluster reports room -- the free cores are spread across nodes and no single node
        holding the 4.75 a pod needed.

        Headroom is subtracted **per node**, on the nodes the transient work runs on
        (:func:`reserved_node_names`), not once from the total: a reserve taken off the sum
        protects no machine in particular.

        Free disk is the smaller of two figures: ``allocatable - requested`` and what the
        kubelet measures free, less the node's eviction threshold (:meth:`_measured_free_disk`).
        Requests alone count only what pods declared, and a node's disk also holds its images,
        its logs and every write nobody requested -- so a job asking for more disk than a node
        actually had left was placed there, filled it past the kubelet's threshold, and was
        evicted with everything else on the node.
        """
        alloc = self._allocatables(schedulable_only=True)
        ids = self._node_identities()
        per_node, seen = self._committed(set(alloc))
        reserved = self._reserved_nodes()
        nodes = []
        for name, a in alloc.items():
            head_cpu, head_mem = _reserve_on(name, reserved)
            used = per_node.get(name, (0.0, 0, 0, 0))
            identity, pinnable = ids.get(name, (None, False))
            free_disk = max(0, int(parse_resource(a.get("ephemeral-storage"))) - used[3])
            measured = self._measured_free_disk(name)
            if measured is not None:
                free_disk = min(free_disk, measured)
            nodes.append(NodeBudget(
                node_id=identity,
                pinnable=pinnable,
                free_cpu=max(0.0, parse_resource(a.get("cpu")) - used[0] - head_cpu),
                free_memory=max(0, int(parse_resource(a.get("memory"))) - used[1] - head_mem),
                free_gpu=max(0, int(parse_resource(a.get("nvidia.com/gpu"))) - used[2]),
                free_ephemeral=free_disk))
        return Budget(nodes=tuple(nodes), counted_jobs=seen, growable=self._growable())

    def _measured_free_disk(self, name) -> "Optional[int]":
        """Bytes node *name* can still take before the kubelet evicts, or ``None`` if unknown.

        The kubelet's measured ``nodefs`` free space less its hard-eviction threshold for it.
        ``None`` when the node cannot be read -- no ``nodes/proxy`` access, a kubelet that does
        not answer in time -- which leaves the request figure standing alone: an unread disk
        is unknown, not full, and refusing every job for it would stop a cluster over a meter.
        """
        from .kube_client import nodefs_used_available, read_node_summary  # noqa: PLC0415

        core = self._core_api_factory()
        try:
            used, available = nodefs_used_available(read_node_summary(core, name))
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.debug("no disk reading for node %s: %s", name, exc)
            return None
        if available is None:
            return None
        threshold = eviction_threshold_bytes(self._nodefs_eviction(core, name), used + available)
        return max(0, available - threshold)

    def _nodefs_eviction(self, core, name) -> str:
        """The node's ``evictionHard`` ``nodefs.available``, read once from its ``configz``.

        :data:`DEFAULT_NODEFS_EVICTION` when the configuration does not state one -- which is
        what the kubelet itself then applies -- or cannot be read.
        """
        if name not in self._eviction_setting:
            setting = DEFAULT_NODEFS_EVICTION
            try:
                resp = core.connect_get_node_proxy_with_path(
                    name, "configz", _request_timeout=2.0, _preload_content=False)
                try:
                    config = json.loads(resp.data)
                finally:
                    resp.release_conn()
                setting = (((config.get("kubeletconfig") or {}).get("evictionHard") or {})
                           .get("nodefs.available") or DEFAULT_NODEFS_EVICTION)
            except Exception as exc:  # noqa: BLE001 - the kubelet's default then applies
                logger.debug("no kubelet configuration for node %s: %s", name, exc)
            self._eviction_setting[name] = setting
        return self._eviction_setting[name]

    def _growable(self) -> bool:
        """Whether the cluster can add nodes -- the autoscaler's exception.

        **Compared against the WHOLE cluster, never against the job pool.** The declared
        maximum an autoscaler reports covers every node it may create; ``_allocatables`` is
        filtered to the pool and to what is schedulable right now. Comparing the two measured
        different sets: configure a job node pool and the declared max exceeds the pool's
        current total by construction, so ``growable`` was permanently true -- every job then
        created unpinned, per-node accounting bypassed, and ``calibration_applies`` switching
        per-node sizing off without saying so. Cordoning one node had a milder version of the
        same effect.

        A node that is down still counts towards the current total here, for the same reason
        it counts in ``capacities()``: it is coming back, and treating its absence as room the
        autoscaler must supply would ask for a machine to replace one that already exists.

        **``capacity``, not ``allocatable``, and this is the one place that is right.**
        Everything else here sizes against ``allocatable``, because that is what the scheduler
        may hand out. This comparison is not about what may be handed out: it asks whether the
        cluster is at its ceiling, and the ceiling a provider declares is a count of machines
        of some type -- a *capacity* figure, with no kubelet reservation taken off it.
        Comparing it against allocatable measures two different things, and the difference is
        the reservation itself: a cluster sitting exactly at its maximum then reads as growable
        forever, every job is created unpinned, per-node accounting is bypassed, and
        ``calibration_applies`` switches per-node sizing off without saying so.
        """
        declared = self._declared_total()
        if declared is None:
            return False
        core = self._core_api_factory()
        total_cpu = sum(parse_resource((n.status.capacity or {}).get("cpu"))
                        for n in core.list_node().items)
        return declared[0] > total_cpu

    def _node_identities(self) -> dict:
        """``node name -> (identity, pinnable)`` for every node.

        **Every node, always.** The identity is what the accounting keys on, so a node
        without one is not "a node that cannot be pinned" -- it is a node that collides with
        every other one that has none, and the collision is invisible: two unlabelled nodes
        are one slot in the controller's dicts, so half the cluster's free capacity is simply
        never offered. A four-node cluster that has not been re-``setup`` since the label was
        introduced therefore runs on one node's worth of room, with nothing anywhere saying
        so.

        So the label is read where it is there, and where it is not the same value is
        **computed** -- ``node_label`` is a plain digest of the node name with no salt, which
        is exactly why it can be recomputed rather than looked up. That also makes the
        identity stable across the ``setup`` that eventually stamps it: a reservation held
        while the labelling happens keeps its key.

        *pinnable* is the half that genuinely depends on the label: a ``nodeSelector`` can
        only name an identity a node actually carries, so a node labelled since the last
        ``setup`` holds work but is never pinned to.
        """
        from robovast.execution.data.collect_sysinfo import node_label  # noqa: PLC0415

        from .node_placement import NODE_ID_LABEL  # noqa: PLC0415

        core = self._core_api_factory()
        out = {}
        for node in core.list_node().items:
            name = node.metadata.name
            labelled = (node.metadata.labels or {}).get(NODE_ID_LABEL)
            out[name] = (labelled, True) if labelled else (node_label(name), False)
        return out

    # -- readings ----------------------------------------------------------------------

    def _allocatables(self, schedulable_only: bool = False) -> dict:
        """``{node name: allocatable}`` for the nodes in the job pool.

        *schedulable_only* is the difference between the module's two questions, and it has to
        be a parameter rather than a filter applied to both.

        **"Free now" must exclude an unusable node.** One that is cordoned, not ``Ready``, or
        carrying a taint a job pod does not tolerate cannot receive work, so counting its cores
        promises room that cannot be spent. Worse, a node that dies mid-campaign loses its pods
        once the eviction timeout passes and then reads as fully *free* -- so admission pinned
        job after job to it, each was refused by the scheduler for an untolerated ``not-ready``
        taint, and each was dropped as a fault rather than as contention. Runs were discarded
        in a loop for as long as the node stayed down, and a cordon for maintenance did the
        same.

        **"Could ever" must include it.** A cordoned or rebooting node is coming back, and
        :meth:`~.node_admission.AdmissionController.preflight` raises a *permanent* error --
        so filtering here would turn a maintenance window into a campaign that refuses to
        start rather than one that waits.

        The tolerations are the job pods' own (:data:`~.node_placement.CAMPAIGN_NODE_TOLERATIONS`),
        not an empty set: a deployment that taints its campaign nodes must still count them.
        """
        from .node_placement import (  # noqa: PLC0415 - avoids an import cycle
            CAMPAIGN_NODE_TOLERATIONS, node_is_schedulable)

        core = self._core_api_factory()
        kwargs = {}
        if self._node_selector:
            kwargs["label_selector"] = ",".join(f"{k}={v}"
                                                for k, v in self._node_selector.items())
        nodes = core.list_node(**kwargs)
        return {n.metadata.name: (n.status.allocatable or {}) for n in nodes.items
                if not schedulable_only
                or node_is_schedulable(n, CAMPAIGN_NODE_TOLERATIONS)}

    def _committed(self, node_names: set):
        """``{node: (cpu, memory, gpu, ephemeral)}`` requested on each, and the Job names.

        Filtered server-side to non-terminal pods: a Succeeded pod still exists as an object
        but holds nothing, and counting it would shrink the cluster by everything that ever
        ran on it.

        Bound pods only. A pod that is still Pending has been *promised* nothing -- counting
        it would double-charge the very reservation the ledger is already holding for it.

        Every workload container, native sidecars included, because Kubernetes adds their
        requests to the pod's effective total and so does the scheduler.

        **Disk is charged as the scheduler charges it, which cpu here is not.** A pod's
        effective request is the larger of its workload containers' sum and its largest
        one-shot init container, and for cpu and memory the init term is small enough to
        drop. For disk it is the whole figure: a postprocessing pod stages its campaign in
        an init container, whose request is the size of that campaign, while its workload
        container asks for a floor. Summing workload containers alone would report the
        node's disk as free while the scheduler holds it for that pod.
        """
        core = self._core_api_factory()
        pods = core.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed")
        from .cluster_execution import _pod_job_name  # noqa: PLC0415 - avoids a cycle

        per_node = {}
        seen = set()
        for pod in pods.items:
            node = getattr(pod.spec, "node_name", None)
            if node not in node_names:
                continue
            job_name = _pod_job_name(pod)
            if job_name:
                seen.add(job_name)
            cpu, mem, gpu, disk = per_node.get(node, (0.0, 0, 0, 0))
            workload_disk = 0
            for container in pod_workload_containers(pod):
                requests = _requests(container)
                cpu += parse_resource(requests.get("cpu"))
                mem += int(parse_resource(requests.get("memory")))
                gpu += int(parse_resource(requests.get("nvidia.com/gpu")))
                workload_disk += int(parse_resource(requests.get("ephemeral-storage")))
            one_shot = [int(parse_resource(_requests(c).get("ephemeral-storage")))
                        for c in (getattr(pod.spec, "init_containers", None) or [])
                        if getattr(c, "restart_policy", None) != "Always"]
            disk += max([workload_disk, *one_shot])
            per_node[node] = (cpu, mem, gpu, disk)
        return per_node, frozenset(seen)
