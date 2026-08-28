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

import logging
import os
from typing import List, Optional, Tuple

from .kube_client import parse_resource, pod_workload_containers
from .node_admission import Budget, Capacity

logger = logging.getLogger(__name__)

#: Held back from every node so RoboVAST's own transient work -- the build daemon, the aux
#: discovery pod, an ``exec_in_container`` session -- is not squeezed out by a campaign that
#: filled the cluster exactly.
#:
#: **Not a ``.vast`` knob.** The reserve protects tenants no single campaign owns, so letting
#: one campaign shrink it would let it take capacity every other campaign depends on.
HEADROOM_CPU_ENV = "ROBOVAST_NODE_HEADROOM_CPU"
HEADROOM_MEMORY_ENV = "ROBOVAST_NODE_HEADROOM_MEMORY"
DEFAULT_HEADROOM_CPU = "1"
DEFAULT_HEADROOM_MEMORY = "2Gi"


def _headroom() -> "Tuple[float, int]":
    """The cluster-wide reserve, from the service's environment.

    Unparseable raises rather than falling back: a typo that silently became "no headroom"
    would over-admit on every node, and the symptom -- occasional unschedulable pods under
    load -- points nowhere near the cause.
    """
    raw_cpu = os.environ.get(HEADROOM_CPU_ENV, DEFAULT_HEADROOM_CPU)
    raw_mem = os.environ.get(HEADROOM_MEMORY_ENV, DEFAULT_HEADROOM_MEMORY)
    cpu = parse_resource(raw_cpu)
    mem = int(parse_resource(raw_mem))
    if (raw_cpu and not cpu) or (raw_mem and not mem):
        raise ValueError(
            f"{HEADROOM_CPU_ENV}={raw_cpu!r} {HEADROOM_MEMORY_ENV}={raw_mem!r}: not resource "
            "quantities. Use e.g. '1' and '2Gi'.")
    return cpu, mem


class ClusterBudgetProvider:
    """Reads the live cluster. The only implementation today; see ``BudgetProvider``."""

    def __init__(self, core_api_factory, *, node_selector: "Optional[dict]" = None,
                 cluster_config=None, kube_context=None):
        self._core_api_factory = core_api_factory
        self._node_selector = node_selector or {}
        # An autoscaling cluster knows a size it is not currently at. See _declared_total.
        self._cluster_config = cluster_config
        self._kube_context = kube_context

    # -- the two questions -------------------------------------------------------------

    def capacities(self) -> "List[Capacity]":
        return [Capacity(cpu=parse_resource(a.get("cpu")),
                         memory=int(parse_resource(a.get("memory"))),
                         gpu=int(parse_resource(a.get("nvidia.com/gpu"))))
                for a in self._allocatables().values()]

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

        Failure is an absence, not an error -- the override shells out to ``gcloud`` and a
        cluster that cannot answer should fall back to counting its nodes, not stop admitting.
        """
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
        """Free capacity across the cluster, and the Jobs this reading already accounts for.

        Cluster-wide rather than per node, because with one uniform request the scheduler does
        the placing and admission only has to answer "is there room somewhere". Per-node
        budgets are what per-node *sizing* would need, and this returns early precisely so
        that change is additive.
        """
        alloc = self._allocatables()
        used_cpu, used_mem, used_gpu, seen = self._committed(set(alloc))
        head_cpu, head_mem = _headroom()
        total_cpu = sum(parse_resource(a.get("cpu")) for a in alloc.values())
        total_mem = sum(int(parse_resource(a.get("memory"))) for a in alloc.values())
        declared = self._declared_total()
        if declared is not None:
            # Never smaller than what is actually there: an override that under-reports must
            # not shrink a cluster below the nodes it already has.
            total_cpu = max(total_cpu, declared[0])
            total_mem = max(total_mem, declared[1])
        total_gpu = sum(int(parse_resource(a.get("nvidia.com/gpu"))) for a in alloc.values())
        return Budget(
            free_cpu=max(0.0, total_cpu - used_cpu - head_cpu),
            free_memory=max(0, total_mem - used_mem - head_mem),
            free_gpu=max(0, total_gpu - used_gpu),
            counted_jobs=seen)

    # -- readings ----------------------------------------------------------------------

    def _allocatables(self) -> dict:
        core = self._core_api_factory()
        kwargs = {}
        if self._node_selector:
            kwargs["label_selector"] = ",".join(f"{k}={v}"
                                                for k, v in self._node_selector.items())
        nodes = core.list_node(**kwargs)
        return {n.metadata.name: (n.status.allocatable or {}) for n in nodes.items}

    def _committed(self, node_names: set):
        """Requests of every pod bound to *node_names*, and the Job names among them.

        Filtered server-side to non-terminal pods: a Succeeded pod still exists as an object
        but holds nothing, and counting it would shrink the cluster by everything that ever
        ran on it.

        Bound pods only. A pod that is still Pending has been *promised* nothing -- counting
        it would double-charge the very reservation the ledger is already holding for it.

        Every workload container, native sidecars included, because Kubernetes adds their
        requests to the pod's effective total and so does the scheduler.
        """
        core = self._core_api_factory()
        pods = core.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed")
        from .cluster_execution import _pod_job_name  # noqa: PLC0415 - avoids a cycle

        cpu = 0.0
        mem = 0
        gpu = 0
        seen = set()
        for pod in pods.items:
            if getattr(pod.spec, "node_name", None) not in node_names:
                continue
            job_name = _pod_job_name(pod)
            if job_name:
                seen.add(job_name)
            for container in pod_workload_containers(pod):
                requests = (container.resources.requests if container.resources else None) or {}
                cpu += parse_resource(requests.get("cpu"))
                mem += int(parse_resource(requests.get("memory")))
                gpu += int(parse_resource(requests.get("nvidia.com/gpu")))
        return cpu, mem, gpu, frozenset(seen)
