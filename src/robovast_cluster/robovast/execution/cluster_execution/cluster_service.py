# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""``ClusterService`` — the in-cluster service core (the cluster mode).

Runs inside the ``robovast-service`` Deployment and drives every cluster campaign
**in this process**, exactly as :class:`~robovast.service.client.LocalTransport`
already does for Docker: one worker thread per campaign runs the unified
``CampaignController`` against a :class:`KubernetesBackend`, which creates the
scenario Jobs. Cluster and local therefore share the whole driver-hosting shape —
only the backend differs — and everything below is expressed as overrides of
``LocalTransport``'s launch hooks.

There is **no per-campaign controller pod** any more. It predated this persistent
service and had become a redundant second copy of a process the service can host
itself; removing it also removed the project/plugin staging round-trip through the
object store, the duplicate ``controller.log`` (pod stdout *and* object store), and
the per-campaign HTTP control server the host had to reach over a pod IP. Live
status is now just a read of the in-process ``ControllerState``.

What still runs as its own Kubernetes workload — because each genuinely needs to:

* **scenario runs** and the **rosbag→CSV postprocessing** — Jobs (scheduled, queued);
* **auxiliary variation containers** — one aux Pod per campaign the driver execs
  into (see :mod:`..execution.cluster_execution.container_runner`).

Durability is unchanged: the object store is the campaign's home, so finished
campaigns survive a service restart untouched. A campaign still *running* when the
service restarts is interrupted (its Jobs and uploaded results persist) — the
accepted trade for running the driver in-process.
"""

import contextlib
import dataclasses
import json
import logging
import os
import shlex
import tempfile
import threading
import time
from pathlib import Path

from robovast.client import file_address
from robovast.common import file_view
from robovast.common.config import SCENARIO_CONTAINER
from robovast.execution.control_server import Phase, is_running
from robovast.service.client import LocalTransport
from robovast.service.interface import (ActionResult, FileListing, FileText, JobCounts, JobKind,
                                        JobSummary, ListJobsResponse, LogChunk, ResourceUsage,
                                        DiskSpace, UpgradeInfo, VersionInfo)

from .manifests import CALIBRATION_JOB_KIND, JOB_KIND_LABEL

logger = logging.getLogger(__name__)

AUX_LABEL = "app=robovast-aux"


def _object_entry(name: str, size):
    """One ``detail=True`` entry for an object-store listing.

    No ``modified`` or ``executable``: a directory here is a *common prefix*, which has
    neither, and the listing call does not carry per-object metadata. ``None`` says
    "this substrate did not report it" rather than fabricating a zero.
    """
    from robovast.service.interface import FileEntry
    return FileEntry(name=name.rstrip("/"), is_dir=name.endswith("/"), bytes=size)


class ClusterService(LocalTransport):
    """Interface implementation that drives campaigns in-process over Kubernetes."""

    #: A staged entrypoint must carry the *cluster* init and post-run blocks here, since
    #: that is where the exec actually runs. Copying a campaign's rendered entrypoint
    #: across lanes is what this flag exists to prevent.
    _EXEC_CLUSTER_LANE = True

    #: No screen to draw on: the work runs in pods, and the X socket a window would need
    #: belongs to whatever machine the service happens to sit on — never the caller's.
    #: ``_admit_show_gui`` turns this into an explicit refusal rather than a silent
    #: windowless run.
    _SUPPORTS_SHOW_GUI = False

    #: How long a kubelet Summary reading is reused -- deliberately longer than
    #: ``_USAGE_CACHE_TTL``. One Summary payload carries every pod's stats on that node
    #: (hundreds of KB on a busy one), and a disk fills over minutes, not seconds.
    _DISK_CACHE_TTL = 60.0
    #: Per-node and total wall-clock ceilings. ``/usage`` is polled by every open tab and
    #: is the app's answer to "is the backend there?", so an unresponsive kubelet must not
    #: turn it into a hang, and N nodes must not make its cost proportional to N unbounded.
    _DISK_NODE_TIMEOUT = 2.0
    _DISK_BUDGET_SECONDS = 5.0

    #: Wall-clock ceiling on the metrics-server read, for the same reason as
    #: ``_DISK_NODE_TIMEOUT``: ``/usage`` answers "is the backend there?" and must not hang
    #: because an aggregated API is slow.
    _METRICS_TIMEOUT = 2.0
    #: How long "this cluster cannot answer metrics.k8s.io" is remembered. A missing
    #: metrics-server, or a ClusterRole that was never reconciled, changes only when someone
    #: installs a component or runs ``vast service upgrade`` -- so retrying every 10 s
    #: window would spend a round trip and an audit-log line six times a minute to learn the
    #: same thing. Only *failures* are memoised; a working cluster is read fresh each window.
    _METRICS_ABSENT_TTL = 600.0

    def __init__(self, namespace=None, cluster_config_name=None,
                 cluster_config_kwargs=None, store=None,
                 reap_on_start=True, kube_context=None, results_dir=None):
        # Where the campaigns this service DRIVES keep their working root. Not a cache: a
        # batch downloads its own results into it, extraction reads it through a path, and
        # postprocessing derives data.db from it -- so it has to outlive a container, and the
        # deployment mounts the directory it names (see serve_backend / service_deploy).
        super().__init__(store=store, results_dir=results_dir)
        self.namespace = namespace or os.environ.get("ROBOVAST_NAMESPACE", "default")
        # Which kubeconfig context to dispatch into. None off-cluster means the
        # active context; in-cluster the incluster config is used for the API
        # client, but the context *name* still resolves per-cluster resource
        # lists — deploy stamps it into ROBOVAST_KUBE_CONTEXT for the in-pod driver.
        self.kube_context = kube_context or os.environ.get("ROBOVAST_KUBE_CONTEXT")
        # Which of the three sources won, reported in version(). Without it the
        # implicit case ("whatever kubectl points at") is indistinguishable from a
        # deliberate one, and that is the case that quietly targets another cluster.
        self._kube_context_source = (
            "--context" if kube_context
            else "ROBOVAST_KUBE_CONTEXT" if os.environ.get("ROBOVAST_KUBE_CONTEXT")
            else "active kubeconfig context")
        # Built on first use rather than here: constructing it touches the Kubernetes client,
        # and a service must come up to report that the cluster is unreachable rather than
        # failing to start because it is.
        self._admission = None
        self._admission_lock = threading.Lock()
        self._config_name = cluster_config_name or os.environ.get(
            "ROBOVAST_CLUSTER_CONFIG_NAME")
        self._config_kwargs = cluster_config_kwargs
        if self._config_kwargs is None:
            raw = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
            self._config_kwargs = json.loads(raw) if raw else {}
        # Off-cluster + embedded MinIO: one persistent kubectl port-forward for the
        # service lifetime, giving the in-process driver's storage client a
        # host-reachable S3 endpoint (see _driver_s3_endpoint / _cluster_config).
        self._minio_pf = None
        self._minio_pf_endpoint = None
        self._minio_pf_port: "int | None" = None
        self._pf_lock = threading.Lock()
        # Bumped every time a forward is opened. A storage client that timed out watches
        # this instead of tearing the tunnel down itself: the keep-alive
        # (_pf_monitor_loop) is the single rotator, so "has it been replaced yet?" is the
        # only question a client needs answered. Guarded by _pf_lock.
        self._pf_generation = 0
        self._pf_monitor: "threading.Thread | None" = None
        self._pf_monitor_stop = threading.Event()
        # Per-campaign locks so concurrent readers don't each re-download the same
        # objects into the shared cache dir. Guarded by ``_fetch_locks_guard``.
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._fetch_locks_guard = threading.Lock()
        # What this service's last transfer of each campaign's objects cost, as
        # ``(bytes, seconds)`` — so ``campaign_data_status`` reports a measured number
        # rather than a guess, and a caller that waited can be told why. Process-local: a
        # restart forgets it, and the cache it describes is scratch anyway.
        self._last_fetch: dict[str, tuple[int, float]] = {}
        # Last kubelet Summary reading behind the disk/store meters, as
        # ``(monotonic, fields)``. Its own TTL, longer than the usage cache's -- see
        # ``_DISK_CACHE_TTL``. Read under ``_usage_lock``, so it needs no lock of its own.
        self._disk_cache: "tuple[float, dict] | None" = None
        # Why metrics-server could not be read, as ``(monotonic, reason)`` -- a NEGATIVE memo
        # only. See ``_METRICS_ABSENT_TTL``: a cluster that does not serve metrics.k8s.io
        # must not be asked every usage window forever. Read under ``_usage_lock``.
        self._metrics_absent: "tuple[float, str] | None" = None
        # How far along the blocking work for each campaign currently is — the counts behind
        # ``CampaignDataStatus.progress``. Written by the transfer and the notebook render,
        # dropped when they finish, so a present entry means "busy right now". In memory on
        # purpose: a client polls this once a second while a transfer is saturating the
        # port-forward, and asking must not add a round-trip to the store it is describing.
        self._work_progress: dict[str, "WorkProgress"] = {}
        self._work_progress_guard = threading.Lock()
        # (monotonic, {campaign_id: created_at}) from the object store's campaign index,
        # or None when it must be re-read. TTL-cached because the campaign-list SSE stream
        # re-lists once a second; see _campaign_index.
        #: (when, {id: created_at}, {id: finished_at}) — both maps from ONE cached pass,
        #: because ordering needs a start time for every campaign and a finish time for
        #: every terminal one, and two listings of zero-byte keys cost far less than a
        #: record fetch per campaign.
        self._index_cache: "tuple[float, dict, dict] | None" = None
        self._index_lock = threading.Lock()
        # True while one caller is out doing the listing. Guards the single-flight in
        # _campaign_index: the listing itself must not hold _index_lock (it is network
        # I/O), so this is what stops a poll from launching a second one behind the first.
        self._index_refreshing = False
        if reap_on_start:
            self.reap_orphans()
            self.resume_interrupted_campaigns()

    # -- version ------------------------------------------------------------

    def version(self) -> VersionInfo:
        v = super().version()
        v.backend = "kubernetes"
        v.kube_context = self.kube_context
        v.kube_context_source = self._kube_context_source
        v.namespace = self.namespace
        v.in_pod = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
        v.api_server = self._api_server_url()
        # No filesystem roots on this lane. Campaign results live in the object store;
        # the local ``/tmp/robovast-campaigns`` scratch is ephemeral and holds only
        # already-fetched campaigns, so a caller told to look there would find one
        # campaign present and the next missing. Workspaces *are* on this service's
        # disk, but that disk is the cluster's, not the caller's.
        v.results_root = None
        v.sources_root = None
        # Overrides the local lane's unconditional True: here a build needs somewhere to
        # push to, and this deployment may not have one. Read from the cached cluster
        # config, which is a plain `os.environ` lookup -- deliberately not
        # `_resolve_registry_objects`, which does Secret lookups and would put an API
        # call in the one call a client makes to find out where it is pointed.
        try:
            registry = self._cluster_config().get_registry_config()
            v.can_build_images = registry.enabled()
            v.build_unavailable = registry.why_disabled() or None
        except Exception:  # noqa: BLE001 - no config readable is not a build verdict
            # Leave both None: "I could not tell" is not "you cannot build", and a
            # consumer prints nothing for None. Reporting False here would send an
            # operator to fix a registry over a config-loading problem.
            pass
        return v

    # -- rolling this service onto newer bytes ------------------------------

    def upgrade_info(self) -> UpgradeInfo:
        """What is running here, what is published, and whether this pod can roll itself.

        Builds on the local answer -- which supplies the live campaign list and a refusal --
        and replaces the refusal only once every precondition actually holds. Written that
        way round so a lane that cannot roll always carries a *reason*, and never an empty
        ``supported=False`` a reader has to interpret.
        """
        info = super().upgrade_info()
        if not os.environ.get("KUBERNETES_SERVICE_HOST"):
            # A service driving the cluster from outside it: there is a Deployment, but it
            # is not this process, and rolling it would not update the thing the caller is
            # talking to.
            info.unsupported_reason = (
                "this service drives the cluster from outside it, so it has no Deployment "
                "of its own to roll. Restart it where it runs.")
            return info
        from .service_deploy import deployment_image_ref, running_image_digest
        ref, denied = deployment_image_ref(self.namespace, self.kube_context)
        if denied:
            # A 403 on the plain read is a missed migration, not a broken cluster: the
            # apps/deployments grant arrived with this feature, and a deployment set up
            # before it has a Role without it. Named with the fix, and the fix deliberately
            # does not need a roll -- which is what makes it available mid-campaign.
            info.unsupported_reason = (
                "this deployment's service account may not read its own Deployment. Run "
                "'vast service upgrade --no-restart' once, from somewhere with "
                "cluster access, to grant it -- that reconciles RBAC without rolling the "
                "pod, so it is safe while a campaign is in flight.")
            return info
        info.image_ref = ref
        info.running_digest = running_image_digest(self.namespace, self.kube_context)
        try:
            info.registry_digest = self._images.published_digest(ref) if ref else ""
        except Exception as e:  # noqa: BLE001 - a registry that will not answer is a fact
            logger.debug("could not read the published digest for %s: %s", ref, e)
            info.registry_digest = ""
        # Its own try: the date is an addition to the digest above, so a registry that
        # answers the HEAD but not the config blob must still report the digest it gave.
        try:
            info.registry_built_at = (
                self._images.published_created(ref) if info.registry_digest else "")
        except Exception as e:  # noqa: BLE001 - same fact, one question further in
            logger.debug("could not read the published build date for %s: %s", ref, e)
            info.registry_built_at = ""
        if info.running_digest and info.registry_digest:
            # The two sides spell a digest differently: the registry answers
            # ``repo@sha256:...`` while the kubelet's imageID is already reduced to the
            # bare ``sha256:...`` (see running_image_digest). Compare the part they share.
            # When either is missing this stays None -- "I could not tell" -- because
            # rendering that as "up to date" is the one wrong answer here.
            info.upgrade_available = (
                info.registry_digest.rpartition("@")[2] != info.running_digest)
        info.supported = True
        info.unsupported_reason = None
        return info

    def upgrade_service(self, force: bool = False) -> ActionResult:
        """Roll this service's own Deployment. See :meth:`upgrade_info` for what it is not.

        The live-campaign refusal is a ``RuntimeError`` (409, a conflict the caller can
        resolve) rather than the ``ValueError`` (400) an unsupported lane raises: one is
        "not now", the other is "not here".

        It refuses only for the campaigns that would actually be lost. A live campaign is not
        itself a reason: a replacement pod picks one back up (see
        :meth:`resume_interrupted_campaigns`). What is left is the campaigns that cannot be
        picked back up, and the refusal names
        each one's reason rather than its phase -- because the reason is what the operator
        would have to change.
        """
        info = self.upgrade_info()
        if not info.supported:
            raise ValueError(info.unsupported_reason)
        blocked = self._campaigns_a_restart_would_lose(info.active_campaigns)
        if blocked and not force:
            named = "; ".join(f"{cid}: {why}" for cid, why in blocked.items())
            raise RuntimeError(
                f"refusing to roll while {len(blocked)} live campaign(s) could not be "
                f"picked up again by the replacement — {named}. Stop them, wait for them, "
                f"or force the roll. Every other live campaign survives the roll: its Jobs "
                f"keep running and the new pod re-attaches to them.")
        from .service_deploy import patch_restart_annotation
        stamped = patch_restart_annotation(self.namespace, self.kube_context)
        return ActionResult(ok=True, message=(
            f"rolling robovast-service (restartedAt {stamped}). Kubernetes starts the new "
            f"pod before stopping this one, so the API stays up; watch the running digest "
            f"for the handover. RBAC, the registry route, the env "
            f"Secrets and the build daemon are NOT reconciled -- "
            f"'vast service upgrade' is what does that."))

    def _api_server_url(self) -> "str | None":
        """The API server this lane targets, read from config only — never dialled.

        ``version()`` is the call a client makes to find out *where* it is pointed,
        including when the cluster is unreachable; a probe here would make it hang
        exactly when the answer matters most. ``None`` when the config cannot be
        read at all, which is itself the answer to "which cluster?".
        """
        try:
            from kubernetes import client as k8s_client

            from .kube_client import load_kube_config
            load_kube_config(context=self.kube_context)
            return k8s_client.Configuration.get_default_copy().host
        except Exception:  # noqa: BLE001 - informational field, never fatal
            return None

    def _compute_resource_usage(self) -> ResourceUsage:
        """Cluster CPU/memory capacity + current usage from the Kubernetes API.

        Capacity is the sum of every node's ``allocatable`` (the same measure admission
        sizes against); ``cpu_reserved`` is the sum of resource *requests* of the pods
        **bound to those same nodes** — what the scheduler has actually committed,
        the number ``kubectl describe node`` calls "Allocated resources". Both are
        read behind :meth:`LocalTransport.resource_usage`'s TTL cache, and the pod
        list is filtered server-side to skip finished pods — so a poll costs at most
        one ``list_node`` + one filtered ``list_pod`` + one metrics-server ``nodes`` list
        per cache window.

        ``cpu_measured`` is what is actually being consumed, from metrics-server (see
        :meth:`_measured_cpu_mem`), and is ``None`` on a cluster that cannot answer for it.
        Reserved is the number that decides whether the next campaign *fits*; measured is
        the number that says whether the last one needed what it asked for.

        Summing over one node set keeps ``used <= capacity``, which a cluster-wide
        pod sum does not: a pod still waiting for a node (or left behind by one that
        was removed) requests resources nothing has granted, so a queue of pending
        scenario runs would otherwise report more cores in use than the cluster has —
        "29.7/24" on a 24-core workstation. Pending work is visible as
        ``jobs_pending`` instead, counted from Jobs by :meth:`_scenario_job_tally`.

        Requires the service's ClusterRole (nodes/pods get,list + nodes/proxy get +
        metrics.k8s.io/nodes get,list — see ``service_deploy._service_rbac_manifests``). The
        proxy grant is for the disk meter's kubelet read, which adds one Summary GET per node
        per ``_DISK_CACHE_TTL`` on top of the per-window list calls above; the metrics grant
        is for ``cpu_measured``, and a deployment that lacks it reports the reason rather
        than losing the rest of this reading.
        """
        from .kube_client import pod_workload_containers  # pylint: disable=import-outside-toplevel
        from .service_deploy import SERVICE_NAME  # pylint: disable=import-outside-toplevel
        from .kube_client import parse_resource as _parse_resource  # pylint: disable=import-outside-toplevel
        v1 = self._k8s()

        cpu_capacity = 0.0
        mem_capacity = 0
        node_names = set()
        for node in v1.list_node().items:
            alloc = node.status.allocatable or {}
            cpu_capacity += _parse_resource(alloc.get("cpu"))
            mem_capacity += int(_parse_resource(alloc.get("memory")))
            node_names.add(node.metadata.name)

        cpu_used = 0.0
        mem_used = 0
        service_node = None
        pods = v1.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed")
        for pod in pods.items:
            # Which node carries the service, taken from a list we already have rather than
            # a read of its own: the disk meter reports THAT node's filesystem, because the
            # workspaces volume is a hostPath there on a stock RKE2 and a cluster-wide sum
            # answers a question nobody asks.
            meta = pod.metadata
            if ((meta.labels or {}).get("app") == SERVICE_NAME
                    and meta.namespace == self.namespace):
                service_node = getattr(pod.spec, "node_name", None) or service_node
            if getattr(pod.spec, "node_name", None) in node_names:
                # Native sidecars included: Kubernetes adds their requests to the pod's
                # effective total rather than taking the max as it does for ordinary init
                # containers. Counting only spec.containers would therefore under-report a
                # scenario job by its simulator and its SUT -- the two biggest reservations
                # in a three-container campaign -- and this number is what sizes a sweep.
                for container in pod_workload_containers(pod):
                    requests = (container.resources.requests
                                if container.resources else None) or {}
                    cpu_used += _parse_resource(requests.get("cpu"))
                    mem_used += int(_parse_resource(requests.get("memory")))

        jobs_running, jobs_pending = self._scenario_job_tally()
        measured = self._disk_and_store(node_names, service_node)
        cpu_metric, mem_metric, metrics_reason = self._measured_cpu_mem(node_names)
        return ResourceUsage(
            backend="kubernetes",
            cpu_capacity=cpu_capacity,
            cpu_used=cpu_used,
            memory_capacity_bytes=mem_capacity,
            memory_used_bytes=mem_used,
            # The request sum, said in the field that means it. ``cpu_used`` above carries
            # the same number as the headline every existing consumer reads.
            cpu_reserved=cpu_used,
            memory_reserved_bytes=mem_used,
            cpu_measured=cpu_metric,
            memory_measured_bytes=mem_metric,
            metrics_unavailable=metrics_reason,
            parallel_runs=True,   # runs execute in parallel, bounded only by capacity
            jobs_running=jobs_running,
            jobs_pending=jobs_pending,
            disk=measured.get("disk"),
            disk_node=measured.get("disk_node"),
            store=measured.get("store"),
            store_node=measured.get("store_node"),
            disk_unavailable=measured.get("unavailable"),
        )

    def _measured_cpu_mem(self, node_names) -> tuple:
        """Real cpu/memory consumption from metrics-server: ``(cores, bytes, reason)``.

        One ``metrics.k8s.io/v1beta1/nodes`` list for the whole cluster -- not a per-node
        fan-out like :meth:`_disk_and_store`, which is why this needs no budget: the payload
        is one small item per node, next to a ``list_pod_for_all_namespaces`` in the same
        window that carries every non-terminal pod spec in the cluster.

        Summed over ``node_names`` -- the same node set ``cpu_capacity`` is summed over, so
        capacity, reserved and measured are all statements about one cluster.

        **Either all three numbers or none.** A node in the set with no metrics item, or one
        whose quantity does not parse, yields ``(None, None, reason)`` rather than a partial
        sum: :func:`kube_client.parse_resource` answers 0 for unparseable (load-bearing in
        the fit tests, where an unadvertised resource must read as none available), so
        summing blind would report a cluster at 60% of its cores as being at 10% -- a wrong
        answer that looks right. A freshly joined node is missing from metrics for ~15 s, and
        a gap in the chart is the truth for that window.

        Failures are memoised for ``_METRICS_ABSENT_TTL``; successes are not. Called with
        ``_usage_lock`` held (see :meth:`LocalTransport.resource_usage`), so both the memo
        and the read need no lock of their own.

        Requires ``metrics.k8s.io/nodes`` get+list in the service's usage ClusterRole (see
        ``service_deploy._service_rbac_manifests``). A deployment whose RBAC predates that
        grant keeps working: it gets a 403, and the reason says which command reconciles it.
        """
        from .kube_client import (CONNECT_TIMEOUT_SECONDS,  # pylint: disable=import-outside-toplevel
                                  parse_resource)

        now = time.monotonic()
        remembered = self._metrics_absent
        if remembered is not None and now - remembered[0] < self._METRICS_ABSENT_TTL:
            return None, None, remembered[1]

        def absent(reason):
            self._metrics_absent = (time.monotonic(), reason)
            return None, None, reason

        try:
            listed = self._k8s_custom().list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes",
                # A (connect, read) pair rather than a scalar: a scalar replaces BOTH, and
                # the connect default is the one thing `load_kube_config` installs
                # process-wide (see kube_client). Only the read is capped here.
                _request_timeout=(CONNECT_TIMEOUT_SECONDS, self._METRICS_TIMEOUT))
        except Exception as e:  # noqa: BLE001 - capacity must still be answerable
            status = getattr(e, "status", None)
            if status == 403:
                return absent("the service's ClusterRole does not grant "
                              "metrics.k8s.io/nodes -- run `vast service upgrade` to "
                              "reconcile RBAC")
            if status == 404:
                return absent("metrics.k8s.io is not served -- install metrics-server "
                              "on the cluster to measure real cpu/memory use")
            # Anything else (a timeout, an unavailable aggregated API mid-restart) is
            # transient as far as this can tell, so it is reported without being remembered.
            logger.debug("could not read node metrics: %s", e)
            return None, None, f"node metrics could not be read: {e}"

        by_node = {(item.get("metadata") or {}).get("name"): item
                   for item in listed.get("items") or []}
        cpu = 0.0
        mem = 0
        missing = 0
        for name in node_names:
            usage = (by_node.get(name) or {}).get("usage") or {}
            # Both quantities or neither: half a node's reading is not a reading.
            cores, byts = usage.get("cpu"), usage.get("memory")
            if not cores or not byts:
                missing += 1
                continue
            node_cpu = parse_resource(cores)          # nanocores ("123456789n")
            node_mem = int(parse_resource(byts))      # working set ("1234Ki")
            # A zero working set is how an unparseable quantity arrives here, because
            # ``parse_resource`` answers 0 rather than raising. It cannot be a real reading:
            # a node running a kubelet has a resident set. CPU is not checked the same way
            # -- an idle node's nanocores can legitimately round to nothing.
            if node_mem <= 0:
                missing += 1
                continue
            cpu += node_cpu
            mem += node_mem
        if missing:
            # Not memoised: a node that just joined is reported seconds later, and this is
            # the one reason that resolves itself.
            return None, None, (f"metrics for {missing} of {len(node_names)} nodes were "
                                "not reported")
        return cpu, mem, None

    def _disk_and_store(self, node_names, service_node=None) -> dict:
        """The kubelet-measured ``disk``/``store`` fields, and which node each came from.

        Read over the ``nodes/proxy`` subresource — the same channel
        :func:`robovast.common.execution._check_static_cpu_manager` reads ``configz`` on.
        There is no alternative source: metrics-server publishes cpu and memory only, and
        the pod-request sum behind ``cpu_used`` cannot answer disk because
        ``ephemeral-storage`` is almost never requested — it would report a few hundred MB
        used on a node that is nearly full, a wrong answer that looks right.

        Memoised on its own longer TTL; called with ``_usage_lock`` held (see
        :meth:`LocalTransport.resource_usage`), so the memo needs no lock of its own.
        """
        now = time.monotonic()
        cached = self._disk_cache
        if cached is None or now - cached[0] >= self._DISK_CACHE_TTL:
            cached = (now, self._read_disk_and_store(sorted(node_names), service_node))
            self._disk_cache = cached
        return cached[1]

    def _read_disk_and_store(self, node_names, service_node=None) -> dict:
        """The SERVICE's node filesystem, then let the provider name its results store.

        Node-local, not summed, and that is the whole point at scale. A cluster-wide sum
        answers a question nobody asks: with twenty nodes it reports tens of terabytes while
        the only disk that decides whether a campaign can be written is the one under the
        service's workspaces -- a hostPath on a stock RKE2, so pinned to a single node and
        invisible in the kubelet's per-volume stats. Summing also could not survive the
        scale it claimed to serve: all-or-nothing across the node set, with a
        ``_DISK_BUDGET_SECONDS`` of 5 against a ``_DISK_NODE_TIMEOUT`` of 2, meant twenty
        nodes blew the budget and reported no disk at all.

        ``used / (used + available)``, not ``capacityBytes``: reserved blocks are in the
        capacity and cannot be written, so ``available`` is the only honest denominator --
        the same correction the store's meter carries.

        ``node.fs`` (nodefs) only, **not** summed with ``node.runtime.imageFs``: on a
        single-disk node those are two views of the SAME device, so summing doubles
        capacity and used alike -- the ratio survives but the labelled numbers become
        fiction.

        The store is a different node's business, so the nodes are read in order --
        the service's first, so the disk figure is answerable even if the budget then runs
        out -- and the walk STOPS as soon as the provider recognises its store. On the
        clusters that have one that is one or two reads, whatever the node count.
        """
        from .kube_client import (  # pylint: disable=import-outside-toplevel
            read_node_summary, nodefs_used_available)
        v1 = self._k8s()
        deadline = time.monotonic() + self._DISK_BUDGET_SECONDS
        summaries = {}
        fields = {}
        # The service's node first: it is the one figure that must survive a short budget.
        ordered = ([service_node] if service_node in node_names else []) + [
            n for n in node_names if n != service_node]
        if service_node is not None and service_node not in node_names:
            logger.debug("service node %s is not in the node list", service_node)
        for name in ordered:
            if time.monotonic() > deadline:
                break
            try:
                summary = read_node_summary(v1, name, self._DISK_NODE_TIMEOUT)
                summaries[name] = summary
                if name == service_node and "disk" not in fields:
                    used, available = nodefs_used_available(summary)
                    if used is not None:
                        fields["disk"] = DiskSpace(capacity_bytes=used + available,
                                                   used_bytes=used)
                        fields["disk_node"] = name
            except Exception as e:  # noqa: BLE001 - the other figure must still be answerable
                # The node is named in the log, never in a returned reason: that string
                # crosses the interface to a UI and an MCP client.
                logger.debug("kubelet stats/summary unavailable on node %s: %s", name, e)
                if name == service_node:
                    fields["unavailable"] = self._summary_read_reason(e)
            # Stop as soon as the provider can answer: its store lives on one node, and
            # walking the rest buys nothing but latency against the budget.
            store_used, store_capacity = self._store_usage(summaries)
            if store_used is not None and store_capacity is not None and store_capacity > 0:
                fields["store"] = DiskSpace(capacity_bytes=store_capacity,
                                            used_bytes=store_used)
                # The node whose Summary finally answered. The walk stops here, so this is
                # the one that carries the store -- which is not necessarily the service's.
                fields["store_node"] = name
                break
        if "disk" not in fields and "unavailable" not in fields:
            fields["unavailable"] = (
                "no node filesystem for the service's node"
                if service_node else "the service's node could not be identified")
        return fields

    @staticmethod
    def _summary_read_reason(e) -> str:
        """Why a kubelet Summary read failed, in words that cross to a UI.

        Only 403 is an RBAC verdict. Answering "the service needs `nodes/proxy` get; run
        `vast service upgrade` to reconcile RBAC" for EVERY exception -- a timeout, a TLS
        refusal, a summary missing a key -- leaves the real reason no further than a
        logger.debug, and reconciling RBAC then returns the identical message. That is the
        failure mode that makes a guess worse than no reason at all: a reader cannot tell a
        fix that did not work from a diagnosis that was never right.
        """
        status = getattr(e, "status", None)
        if status == 403:
            return ("the service may not read `nodes/proxy` (403) — run "
                    "`vast service upgrade --no-restart` to reconcile RBAC")
        detail = str(getattr(e, "reason", None) or e).strip().splitlines()
        detail = (detail[0] if detail else e.__class__.__name__)[:120]
        prefix = f"HTTP {status}: " if status else f"{e.__class__.__name__}: "
        return f"the kubelet Summary API did not answer: {prefix}{detail}"

    def _store_usage(self, summaries):
        """The provider's results-store reading, or ``(None, None)``.

        Delegated because the answer is provider-specific: a cluster hosting its own object
        store can measure the volume behind it, while one backed by a cloud bucket has no
        capacity to fill and so no meter to draw. Never fatal — a provider that raises must
        not take the disk meter down with it.
        """
        try:
            return self._cluster_config().get_store_usage(summaries)
        except Exception as e:  # noqa: BLE001 - the disk meter must still be answerable
            logger.debug("could not read the results store usage: %s", e)
            return None, None

    def _scenario_job_tally(self) -> "tuple[int, int]":
        """``(running, pending)`` over every scenario-run Job in this namespace.

        Counted from **Jobs**, not pods, because a Job whose pod is not bound yet has no
        pod at all (see :func:`list_jobs_with_phase`) — and that is the state every cluster
        batch *starts* in. Reading pods therefore reported a freshly launched 25-run
        sweep as ``0/0`` while its whole queue waited for quota, which is exactly the
        "nothing is happening" the sidebar's jobs bar is there to contradict.

        Classification is delegated rather than repeated: ``list_jobs_with_phase`` is
        the single place that turns Jobs + pods into a phase, and the previous
        hand-rolled pod check here was a consumer that had drifted from it.
        ``pending`` folds in ``waiting`` (queued for quota) and ``blocked`` (cannot
        start on its own) — both are accepted work that is not executing; the
        per-campaign :class:`JobCounts` keeps them apart for the campaign view, which
        is where a blocked job needs its own treatment. ``completed``/``failed`` are
        past work and belong in neither.

        Namespace-scoped, unlike the CPU/memory figures above: those must stay
        cluster-wide because the nodes are shared, but the job tally answers "what is
        *this* service running", the same question :meth:`list_jobs` answers per
        campaign. A read failure propagates — a silently zero tally is the bug this
        method exists to fix.
        """
        from .cluster_execution import \
            list_jobs_with_phase  # pylint: disable=import-outside-toplevel
        phases = [phase for _job, phase, _detail in list_jobs_with_phase(
            self._k8s_batch(), self._k8s(), self.namespace, "jobgroup=scenario-runs")]
        return (sum(1 for p in phases if p == "running"),
                sum(1 for p in phases if p in ("pending", "waiting", "blocked")))

    # -- helpers ------------------------------------------------------------

    def _cluster_config(self):
        from .cluster_setup import get_cluster_config
        if not self._config_name:
            raise ValueError(
                "cluster config not configured (ROBOVAST_CLUSTER_CONFIG_NAME); "
                "the service must be deployed by 'vast cluster setup'")
        cfg = get_cluster_config(self._config_name)
        if self._config_kwargs:
            cfg.restore_from_setup_kwargs(self._config_kwargs)
        # Off-cluster the driver's storage client cannot use the cluster-internal
        # endpoint, so install a resolver giving it a host-reachable one. Every
        # off-cluster storage_client_for caller uses a cfg built here (the
        # service's, directly or via backend.cluster_config), so this one line
        # reaches them all — no arg threading. The config owns the per-provider
        # policy (embedded → port-forward, else → direct); the service only owns
        # the port-forward. In-cluster we install nothing: robovast:9000 resolves.
        # Lazy: the port-forward opens only when a storage client is actually built.
        if not os.environ.get("KUBERNETES_SERVICE_HOST"):
            cfg.set_driver_s3_endpoint_resolver(
                lambda force_reconnect=False, current=None: cfg.resolve_driver_s3_endpoint(
                    self._minio_port_forward_endpoint, force_reconnect, current))
        return cfg

    def _minio_port_forward_endpoint(self, force_restart: bool = False,
                                     current: "str | None" = None) -> str:
        """Return ``http://localhost:<port>`` for the shared MinIO port-forward,
        opening (or re-opening) the forward under the lock.

        A ``kubectl port-forward`` frequently goes *stalled-but-alive* under a large
        transfer (e.g. downloading a whole campaign's rosbags): the process keeps
        running while its tunnel stops proxying, so every S3 request then read-times
        out. ``poll()`` cannot see this — it only reports a *dead process*. So the
        driver's storage client, on a network timeout, re-resolves with
        *force_restart=True*, which tears the current forward down and opens a fresh
        one on a new port; the client then rebuilds itself against the new endpoint.

        *current* is the endpoint the caller was using. When many storage clients
        share this one forward, a stall makes them **all** time out and request a
        restart at once; honoring every request would make each teardown kill the
        forward a sibling just opened, and the sibling's next request would then hit
        "connection refused". So a forced restart is coalesced: if *current* no longer
        matches the live endpoint, another caller already rotated the forward since —
        return the fresh endpoint untouched instead of tearing it down again.
        """
        from robovast.common.shutdown import is_shutting_down

        from .bucket_ops import open_minio_port_forward
        with self._pf_lock:
            if force_restart:
                if (current is not None and self._minio_pf is not None
                        and current != self._minio_pf_endpoint):
                    return self._minio_pf_endpoint
                self._close_minio_pf_locked()
            elif self._minio_pf is not None and self._minio_pf.poll() is not None:
                self._minio_pf = None  # forward died; drop it and reopen below
            if self._minio_pf is None:
                # Opening one now would hand the process a kubectl child it is no
                # longer around to reap: shutdown() has closed (or is closing) the
                # forward, and a late caller — an in-flight S3 read on a worker
                # thread — would resurrect it and leak the tunnel past exit.
                if is_shutting_down():
                    raise RuntimeError(
                        "service is shutting down; not opening a MinIO port-forward")
                self._minio_pf, port = open_minio_port_forward(
                    self.namespace, self.kube_context)
                self._minio_pf_endpoint = f"http://localhost:{port}"
                self._minio_pf_port = port
                self._pf_generation += 1
                logger.info("Opened MinIO port-forward for driver S3 at %s (generation %d)",
                            self._minio_pf_endpoint, self._pf_generation)
                self._start_pf_monitor_locked()
            return self._minio_pf_endpoint

    #: How often the keep-alive probes the forward, and how many consecutive failures it
    #: takes to rotate. Two failures rather than one so a single dropped probe — a busy
    #: tunnel mid-transfer, a GC pause — does not throw away a working forward.
    _PF_PROBE_INTERVAL_S = 5.0
    _PF_FAILURES_BEFORE_ROTATE = 2

    def _start_pf_monitor_locked(self) -> None:
        """Start the keep-alive thread, once. Caller must hold ``_pf_lock``."""
        if self._pf_monitor is not None:
            return
        self._pf_monitor = threading.Thread(
            target=self._pf_monitor_loop, name="robovast-minio-pf-keepalive", daemon=True)
        self._pf_monitor.start()

    def _pf_monitor_loop(self) -> None:
        """Probe the shared forward on a timer and rotate it when it stops serving.

        Stall detection belongs here rather than on the request path. Discovering a stalled
        tunnel by *waiting for an S3 request to time out* costs that request its whole
        timeout budget, and every concurrent request pays it too — which is how one stalled
        forward turned into an unresponsive API. A 5 s probe with a 5 s deadline finds the
        same fact for a fixed, tiny cost and off the path serving users.

        This is also the **only** rotator, which is what makes it safe to rotate at all:
        when N storage clients all time out on one stalled forward and each asks for a
        restart, every teardown kills the tunnel a sibling just opened (the thundering-herd
        mutual teardown the ``current``-coalescing in
        :meth:`_minio_port_forward_endpoint` exists to blunt). One prober cannot race
        itself, so clients need not force anything — they wait for
        ``_pf_generation`` to move and re-resolve.
        """
        from robovast.common.shutdown import is_shutting_down

        from .bucket_ops import forward_is_serving
        failures = 0
        while not self._pf_monitor_stop.wait(self._PF_PROBE_INTERVAL_S):
            if is_shutting_down():
                return
            with self._pf_lock:
                port = self._minio_pf_port
                pf = self._minio_pf
            if pf is None or port is None:
                failures = 0
                continue          # nothing open right now; the next caller opens one
            if forward_is_serving(port):
                failures = 0
                continue
            failures += 1
            if failures < self._PF_FAILURES_BEFORE_ROTATE:
                logger.debug("MinIO port-forward on %d missed a probe (%d/%d)",
                             port, failures, self._PF_FAILURES_BEFORE_ROTATE)
                continue
            failures = 0
            logger.warning(
                "MinIO port-forward on port %d stopped serving; rotating it", port)
            try:
                with self._pf_lock:
                    # Re-check under the lock: a caller may have rotated it since the probe.
                    if self._minio_pf_port != port:
                        continue
                    self._close_minio_pf_locked()
                # Reopened outside the lock is wrong (two callers could both open one), so
                # go through the normal path, which holds the lock and bumps the generation.
                self._minio_port_forward_endpoint()
            except Exception as e:  # noqa: BLE001 - a keep-alive must outlive one failure
                logger.warning("Could not rotate the MinIO port-forward: %s", e)

    def _close_minio_pf_locked(self) -> None:
        """Terminate the current MinIO port-forward. Caller must hold ``_pf_lock``."""
        pf, self._minio_pf = self._minio_pf, None
        self._minio_pf_endpoint = None
        self._minio_pf_port = None
        if pf is not None and pf.poll() is None:
            pf.terminate()
            try:
                pf.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pf.kill()


    def _load_kube(self):
        from .kube_client import load_kube_config
        load_kube_config(context=self.kube_context)

    def _k8s(self):
        from kubernetes import client
        self._load_kube()
        return client.CoreV1Api()

    def _k8s_batch(self):
        from kubernetes import client
        self._load_kube()
        return client.BatchV1Api()

    def _k8s_custom(self):
        """For ``metrics.k8s.io``, which has no generated typed client of its own."""
        from kubernetes import client
        self._load_kube()
        return client.CustomObjectsApi()

    # -- launch hooks (see LocalTransport.create_campaign) -------------------

    def _guard_new_campaign(self) -> None:
        """Cluster campaigns run in parallel.

        The local guard exists because Docker is single-flight; here each campaign
        is an I/O-bound driver thread whose compute lives in Kubernetes Jobs, so
        many run at once. Everything they touch is campaign-scoped: the
        container-runner factory is a ContextVar, ``controller.log`` is filtered to
        its worker thread, and each aux pod / result prefix is keyed by campaign id.
        """
        return None

    def _build_backend(self, state):
        from .kubernetes_backend import KubernetesBackend
        return KubernetesBackend(cluster_config=self._cluster_config(),
                                 namespace=self.namespace,
                                 kube_context=self.kube_context,
                                 state=state,
                                 admission=self._admission_controller())

    def _admission_controller(self):
        """The process-wide admission queue, built once.

        On the service because it is the one object with process lifetime that every campaign
        thread reaches, and because the queue only orders correctly if there is exactly one of
        it: a controller per campaign would order by whichever thread asked first, which is
        the behaviour it exists to replace.

        **Its own lock, never ``_usage_lock``.** That one is held across a resource reading
        that talks to every kubelet in turn (see ``_DISK_BUDGET_SECONDS``), and sharing it
        would let one unresponsive node block every campaign's job creation.

        **Raises rather than falling back.** Nothing gates job creation behind this, so a
        ``None`` here would mean creating a campaign's entire plan -- for a one-run-per-job
        sweep, upwards of a thousand Jobs -- in one unthrottled loop against a cluster sized
        for a few dozen. A service that cannot measure the cluster must refuse to submit
        to it.
        """
        with self._admission_lock:
            if self._admission is None:
                from kubernetes import client

                from .cluster_capacity import ClusterBudgetProvider
                from .kube_client import load_kube_config
                from .node_admission import AdmissionController

                def _core():
                    # Per call, not cached: the config load is idempotent and a client
                    # held across a service's lifetime outlives token rotation.
                    load_kube_config(self.kube_context)
                    return client.CoreV1Api()

                # The cluster config comes along so an autoscaling deployment is sized
                # by what it can become rather than by the nodes it currently has --
                # see ClusterBudgetProvider._declared_total.
                # The node pool campaign jobs are confined to. Counted here AND stamped
                # on every job pod: filtering capacity alone would leave kube-scheduler
                # free to place outside the pool, and stamping alone would have admission
                # promise room on nodes the pods may not use.
                from .node_placement import job_node_pool

                self._admission = AdmissionController(ClusterBudgetProvider(
                    _core, node_selector=job_node_pool(),
                    cluster_config=self._cluster_config(),
                    kube_context=self.kube_context))
            return self._admission

    def _run_options(self, request):
        from robovast.execution.backends import RunOptions

        # postprocess travels in the options (not the process env): one process
        # drives many campaigns, and an env var could not tell them apart.
        # gui stays False unconditionally — a show_gui request never reaches here,
        # _admit_show_gui having refused it.
        return RunOptions(gui=False,
                          postprocess=bool(request.postprocess),
                          upload_to_share=bool(getattr(request, "upload_to_share", False)),
                          namespace=self.namespace)

    def _postprocess_in_process(self) -> bool:
        """False: the builder chains postprocessing before its upload.

        ``_chain_postprocessing`` runs inside the builder (rosbag→CSV as a Job, then
        ``data.db`` here) *before* ``finalize_campaign``, so the derived data rides
        the campaign's existing upload instead of needing one of its own.
        """
        return False

    @contextlib.contextmanager
    def _aux_runner_context(self, tag, project, *, hold=False):
        """An aux container for *tag*'s span + the container-runner factory for this thread.

        Entered inside the thread that composes, so the factory (a ContextVar) is scoped to
        exactly the composition that reads it — concurrent campaigns never clobber each
        other's aux target, and it is reset on the way out because a request thread is
        reused while a worker thread is not.

        A project whose variations need no helper image creates nothing and installs no
        factory. Discovery still runs, and still propagates: "we could not tell whether one
        is needed" must not read as "none is".

        The two spans differ only in who owns the container's death — see
        :meth:`LocalTransport._aux_runner_context`. A campaign's pod is deleted here;
        a held one is released to the exec manager's reaper.
        """
        from robovast.common.config_generation import set_container_runner_factory
        from robovast.service.world_query import _reset_factory

        from .container_runner import AuxPodSession, required_container_specs

        specs = required_container_specs(project.config_path)
        if not specs:
            yield
            return
        if hold:
            with self._held_aux_runners(tag, specs) as factory:
                token = set_container_runner_factory(factory)
                try:
                    yield
                finally:
                    _reset_factory(token)
            return
        with AuxPodSession(tag, specs, self.namespace, core_v1=self._k8s(),
                           kube_context=self.kube_context,
                           pull_secret=self._registry_pull_secret(),
                           **self._aux_store_kwargs()) as session:
            token = set_container_runner_factory(session.runner_factory())
            try:
                yield
            finally:
                _reset_factory(token)

    @contextlib.contextmanager
    def _held_aux_runners(self, tag, specs):
        """Hold one aux container per spec through the exec manager; yield their factory.

        The manager already owns every held container's lifetime — idle reap, a hard
        deadline baked into the pod, an LRU cap and a stray sweep after a restart — so this
        adds no second policy. The specs are held as *query* slots because that policy is
        already this one: a warm image and nothing else, since a runner mirrors its
        workspace around each command and leaves nothing behind between them.

        On the way out the slots are released, not stopped: the next preview of the same
        project reuses a warm pod, and two previews running at once cannot destroy each
        other's.
        """
        from robovast.service.container_exec import ExecSpec, container_name

        from .container_runner import AUX_HOLD_LIMIT_S, ClusterContainerRunner
        from .kube_exec_lane import HELD_CONTAINER
        store = self._aux_store_kwargs()
        slots = {}
        try:
            for spec in specs:
                # The image is what makes the pod worth reusing, and the project is what
                # keeps two of them apart; the pod holds nothing else that could differ.
                identity = ("aux", tag, spec.container_name(), spec.image)
                held = ExecSpec(image=spec.image, command="",
                                config_dir=tempfile.mkdtemp(prefix="robovast_aux_hold_"),
                                env=dict(spec.env or {}), config_name=str(tag),
                                image_identity=spec.image, aux_spec=spec)
                slots[spec.container_name()] = self._exec_manager.hold(
                    held, identity, AUX_HOLD_LIMIT_S)

            def factory(spec):
                return ClusterContainerRunner(
                    spec, container_name(slots[spec.container_name()]), self.namespace,
                    self._k8s(), storage=store["storage"], bucket=store["bucket"],
                    owner_id=str(tag), kube_context=self.kube_context,
                    container=HELD_CONTAINER)

            yield factory
        finally:
            for slot in slots.values():
                self._exec_manager.release_hold(slot)

    def _aux_store_kwargs(self) -> dict:
        """Storage wiring for an aux pod's workspace mirror.

        The same bucket an image-build context stages to — an aux workspace belongs to no
        campaign's results either, and is scratch that is deleted when the runner closes.
        The pod is given the *cluster-internal* endpoint, while this process keeps its own
        client (which off-cluster reaches the store through a port-forward).
        """
        from robovast.execution.cluster_execution import in_pod_storage

        from .cluster_image_build import build_context_bucket
        cfg = self._cluster_config()
        access_key, secret_key = cfg.get_s3_credentials()
        return {
            "storage": in_pod_storage.storage_client_for(cfg),
            "bucket": build_context_bucket(cfg),
            "s3": (cfg.get_s3_endpoint(), access_key, secret_key),
        }

    def _record_campaign_failure(self, campaign_id, results_dir, state, exc, backend):
        """Record the terminal outcome *and* publish it to the object store.

        The local base class only writes ``_execution/outcome.json`` on disk; here the
        service pod's disk is scratch, so the reason must reach the durable home —
        otherwise ``get_status`` could not explain a failure after the fact.
        """
        from robovast.execution.controller import _record_controller_failure
        campaign_root = os.path.join(results_dir, campaign_id)
        try:
            _record_controller_failure(campaign_root, campaign_id, state, exc, backend)
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.warning("Could not record failure for %s", campaign_id, exc_info=True)

    def _record_campaign_stopped(self, campaign_id, results_dir, state, backend) -> None:
        """Publish a stopped campaign's outcome to the object store (pod disk is scratch).

        Succeeds for a Stop-button stop (the storage tunnel is up); on Ctrl+C the
        tunnel is already gone, so the upload fails quietly (logged concisely, no
        traceback) — the process is exiting anyway.
        """
        from robovast.execution.controller import _record_controller_outcome
        campaign_root = os.path.join(results_dir, campaign_id)
        try:
            _record_controller_outcome(campaign_root, campaign_id, state, backend)
        except Exception as e:  # noqa: BLE001 - best-effort; never block the stop
            logger.warning("Could not record stopped outcome for %s: %s", campaign_id, e)

    # -- status / listing ---------------------------------------------------

    # ``_status_from_disk`` is inherited, not overridden here: ``_record_dir`` puts the
    # durable ``_execution/outcome.json`` where every reader already looks, so the inherited
    # implementation — which prefers ``outcome.json`` and merges ``postprocessed`` from a
    # present ``data.db`` — is the one precedence for both lanes. An override reading the
    # object store itself cannot keep a per-campaign status in step with the list view,
    # because ``_summary_for`` reconstructs directly and never calls it.

    def _durable_campaign_ids(self) -> set[str]:
        """Campaign ids from the object store's index (see ``in_pod_storage``).

        This is what makes a finished cluster campaign listable at all: its home is the
        object store, and the inherited disk scan sees only what this pod happens to still
        have in scratch.
        """
        return set(self._campaign_index()[0])

    def _started_at_for(self, cid: str) -> "str | None":
        """Inherited precedence, plus the index as the last resort.

        The index is consulted **before** the store's ``campaign.db``, and that ordering is
        the point: ``list_campaigns`` calls this for *every* candidate id to order them
        before it paginates, so a start time read per campaign would mean one object read
        per campaign on every cold listing — with a 100-campaign SSE poll behind it. The
        marker carries the time in its key, so the whole ordering pass costs the one cached
        listing, and ``_record_dir`` is reached only for the page actually rendered.
        """
        with self._lock:
            entry = self._campaigns.get(cid)
        if entry is not None:
            return entry.created_at
        cached = self._started_at_cache.get(cid)
        if cached is not None:
            return cached
        indexed = self._campaign_index()[0].get(cid)
        if indexed:
            self._started_at_cache[cid] = indexed
            return indexed
        return super()._started_at_for(cid)

    def _finished_at_for(self, cid: str) -> "str | None":
        """Inherited precedence, plus the index as the last resort — see
        :meth:`_started_at_for`, whose reasoning this repeats exactly.

        The marker matters more here than the start one does, because a finish time has no
        cheap fallback: without it, ordering the terminal group would mean materialising a
        record per campaign, which is what that override exists to avoid.

        Not cached locally. The inherited cache is keyed to a value written once; this one
        moves whenever a campaign ends again, and the index it reads is already cached for
        :data:`_INDEX_CACHE_TTL`, so a second cache would only add a way to be stale.
        """
        with self._lock:
            entry = self._campaigns.get(cid)
        if entry is None:
            indexed = self._campaign_index()[1].get(cid)
            if indexed:
                return indexed
        return super()._finished_at_for(cid)

    def _on_campaign_finished(self, campaign_id: str, state) -> None:
        """Publish the campaign's finish marker, so a listing can order by it.

        Best-effort for the same reason as :meth:`_on_campaign_started`: a campaign that has
        already run is not worth failing over its index entry, and a campaign whose marker
        never lands simply orders by its start time.
        """
        from datetime import datetime, timezone
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.execution.control_server import is_terminal
        snap = state.snapshot() if state is not None else None
        if snap is None or not is_terminal(snap.phase) or not snap.phase_since:
            return
        finished_at = datetime.fromtimestamp(snap.phase_since, tz=timezone.utc).isoformat()
        try:
            cfg = self._cluster_config()
            # Interactive: one tiny marker PUT on the campaign's last breath, already
            # best-effort, and nothing should wait minutes on a stalled tunnel for it.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
            in_pod_storage.mark_campaign_finished(storage, cfg, campaign_id, finished_at)
        except Exception as e:  # noqa: BLE001 - discoverability, not correctness
            logger.warning("Could not record the finish time of campaign %s: %s",
                           campaign_id, e)
            return
        with self._index_lock:
            # Extended rather than dropped, for the reason `_on_campaign_started` gives:
            # a cold listing at exactly this moment is the one worth avoiding.
            cached = self._index_cache
            if cached is not None:
                self._index_cache = (cached[0], cached[1],
                                     {**cached[2], campaign_id: finished_at})

    #: How long a campaign-index listing is reused. The campaign-list SSE stream re-lists
    #: every second (app.py ``_SSE_LIST_POLL_S``), so without this every one of those polls
    #: would be an object-store round-trip.
    _INDEX_CACHE_TTL = 10.0

    @staticmethod
    def _empty_index() -> "tuple[dict, dict]":
        return {}, {}

    def _campaign_index(self) -> "tuple[dict, dict]":
        """``({campaign_id: created_at}, {campaign_id: finished_at})`` from the object store,
        cached together for :data:`_INDEX_CACHE_TTL`.

        Two maps from one cached pass rather than two caches: they are read by the same
        ordering loop, on the same tick, and a campaign appears in the second only once it
        has ended.

        Best-effort: an unreachable store means "cannot tell what is stored right now", and
        the honest response is to list what we *can* see rather than fail the listing. The
        stale cache is kept in that case, so a brief outage does not make every stored
        campaign blink out of the list and back.

        **Single-flight.** The listing runs outside ``_index_lock`` on purpose — holding the
        lock across network I/O would queue every reader behind it — but that alone let
        *every* concurrent caller past a cold cache issue its own listing. Behind a 1 Hz SSE
        poll and a slow store that compounds: each tick starts another round-trip that the
        previous tick has not finished, so the work in flight grows without bound and each
        piece of it holds a worker thread. So one caller refreshes and the rest take the
        stale value immediately; a slightly-late listing is worth far more than a
        pile-up. ``{}`` is only returned when there is nothing cached at all.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        now = time.monotonic()
        with self._index_lock:
            cached = self._index_cache
            if cached is not None and now - cached[0] < self._INDEX_CACHE_TTL:
                return cached[1:]
            if self._index_refreshing:
                return self._empty_index() if cached is None else cached[1:]
            self._index_refreshing = True
        try:
            cfg = self._cluster_config()
            # Interactive: this listing sits under the campaign-list SSE's 1 Hz poll, and
            # the ``except`` below already has a good degraded answer (the stale cache).
            # With the bulk budget a stalled tunnel made each poll block for minutes.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
            index = dict(in_pod_storage.list_indexed_campaigns(storage, cfg))
            finished = dict(in_pod_storage.list_finished_campaigns(storage, cfg))
        except Exception as e:  # noqa: BLE001 - never fail a listing over discovery
            logger.warning("Could not read the campaign index: %s", e)
            with self._index_lock:
                self._index_refreshing = False
                return self._empty_index() if self._index_cache is None else self._index_cache[1:]
        with self._index_lock:
            self._index_refreshing = False
            # A marker ``_on_campaign_started`` added while this listing was in flight is
            # simply overwritten, and that is fine: ``list_campaigns`` unions the live
            # registry into its id set (``LocalTransport._extra_live_ids``), so a campaign
            # this process just started stays listed without the index, and the next
            # refresh picks the marker up from the store.
            self._index_cache = (now, index, finished)
        return index, finished

    def _on_campaign_started(self, campaign_id: str, created_at: str) -> None:
        """Publish the campaign's index marker, so it is discoverable from here on.

        Called at the top of the driver, before the image build and the run: everything
        that can go wrong afterwards — a failed build, a crash mid-run, a stop, a failed
        finalize upload — leaves a campaign that is still listed, which is the whole reason
        the marker is not written at the end.

        Best-effort: a campaign is not worth failing over its index entry, and a store
        broken enough to refuse this will fail the campaign's own uploads with a real error
        moments later.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        try:
            cfg = self._cluster_config()
            # Interactive: one tiny marker PUT, already best-effort, and it runs at the
            # head of the driver — a start must not sit for minutes on a stalled tunnel.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
            in_pod_storage.mark_campaign_indexed(storage, cfg, campaign_id, created_at)
        except Exception as e:  # noqa: BLE001 - discoverability, not correctness
            logger.warning("Could not index campaign %s for discovery: %s",
                           campaign_id, e)
            return
        with self._index_lock:
            # Add the new marker to the cache rather than dropping it. Dropping it forced a
            # cold listing at the *worst* moment: the campaign whose start just invalidated
            # it is about to saturate the same connection with its own uploads, and the
            # campaign-list poll would meet a cold cache on every tick until one listing
            # completed. Inserting the one fact the listing would have told us keeps the
            # campaign instantly discoverable and the cache warm. Nothing else about the
            # index can have changed as a result of *this* call.
            cached = self._index_cache
            if cached is None:
                self._index_cache = None  # nothing to extend; the next caller lists
            else:
                self._index_cache = (cached[0], {**cached[1], campaign_id: created_at},
                                     cached[2])

    def _unmark_campaign(self, campaign_id: str) -> None:
        """Drop a deleted campaign's index marker, so it stops being listed."""
        from robovast.execution.cluster_execution import in_pod_storage
        try:
            cfg = self._cluster_config()
            # Interactive: one tiny marker delete on a request path, already best-effort.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
            in_pod_storage.unmark_campaign_indexed(storage, cfg, campaign_id)
            in_pod_storage.unmark_campaign_finished(storage, cfg, campaign_id)
        except Exception as e:  # noqa: BLE001 - the data itself is already gone
            logger.warning("Could not remove campaign %s from the index: %s",
                           campaign_id, e)
        with self._index_lock:
            self._index_cache = None

    def _store_phase_bytes(self, campaign_id: str):
        """A ``get_bytes`` over the campaign's durable ``_execution/`` phase files.

        Resolved **lazily, on the first miss**: this sits behind the log SSE stream,
        which re-polls while the user watches, so a campaign whose phase files are all
        on pod scratch must not pay a store round-trip per poll. A store that cannot be
        resolved reads as "no durable copy" (``None`` for every file) rather than an
        error — the log panel showing what it can beats it showing a stack trace.
        """
        from robovast.common.campaign_logs import EXECUTION_DIR
        from robovast.execution.cluster_execution import in_pod_storage

        #: Empty until first use, then holds ``(storage, bucket, prefix)`` or ``None``.
        resolved: list = []

        def _read(filename: str):
            if not resolved:
                try:
                    cfg = self._cluster_config()
                    bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
                    resolved.append((
                        in_pod_storage.storage_client_for(cfg, interactive=True),
                        bucket, prefix))
                except Exception as e:  # noqa: BLE001 - best-effort; empty if unavailable
                    logger.debug("could not resolve object store for %s: %s", campaign_id, e)
                    resolved.append(None)
            if resolved[0] is None:
                return None
            storage, bucket, prefix = resolved[0]
            try:
                raw = storage.read_object(bucket, f"{prefix}{EXECUTION_DIR}/{filename}")
            except Exception as e:  # noqa: BLE001 - a missing phase file is normal
                logger.debug("could not read %s for %s: %s", filename, campaign_id, e)
                return None
            if not raw:
                return None
            return raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")

        return _read

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        """Serve the unified infrastructure log — live pod scratch, then object store.

        Assembles the per-phase files (variation → run → postprocessing) into one
        divider-separated stream (see
        :func:`robovast.common.campaign_logs.assemble_log`). While this process is
        driving the campaign each phase file is a local file in the service pod's
        scratch (the same one the thread-isolated handlers write), read straight
        from *offset*. Once the campaign is no longer tracked here, the durable copy
        of each phase file in the object store is read.

        The two are **layered, not either/or**, because not every phase writes to the
        tracked scratch root. Cluster postprocessing runs against its own fetched
        campaign root and publishes ``postprocessing.log`` to the object store, so a
        scratch-only read served every tracked cluster campaign a log with no
        POSTPROCESSING section at all — pass or fail — while the bytes sat in the store
        the whole time. That is not detectable as a bug from the reader's side: a
        missing phase file is also how "this phase has not run" looks.
        """
        from robovast.common.campaign_logs import (assemble_log, disk_get_bytes,
                                                   layered_get_bytes)
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        store = self._store_phase_bytes(campaign_id)
        if entry is not None:
            # Scratch first: a phase file this process is still appending to is the live
            # copy, and the durable one lags it. See ``layered_get_bytes`` on why the
            # fallback triggers on absence only.
            campaign_dir = Path(entry.results_dir) / campaign_id
            get_bytes = layered_get_bytes(disk_get_bytes(campaign_dir), store)
            eof = self._is_done(entry)
        else:  # past / reaped campaign: the store holds every phase file's durable copy
            get_bytes = store
            eof = True
        text, next_offset, eof = assemble_log(get_bytes, offset, eof=eof)
        return LogChunk(text=text, next_offset=next_offset, eof=eof)

    # -- jobs (live) --------------------------------------------------------

    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        """List the campaign's scenario-run Kubernetes Jobs with live status.

        Selects Jobs by the campaign label the backend stamps on them
        (``jobgroup=scenario-runs,campaign-id=<label-safe>``) and classifies each
        with :func:`list_jobs_with_phase` — the same pod-accurate logic the CLI
        monitor's aggregate counter uses, so the two never drift. ``display_name``
        is the pod template's ``job-name-full`` annotation (``<batch>-job-<index>``)
        for a readable label.

        **Node-calibration probes are listed but not tallied.** A probe carries the same
        campaign labels -- it is real work on a real node, and every selector that counts or
        cleans up has to keep seeing it -- so it appears here, marked
        ``kind="calibration"`` and named for the node it measures. The counts are the
        campaign's own jobs alone, because a reader takes them as facts about *runs*: see
        :attr:`~robovast.service.interface.JobCounts.calibration`.
        """
        from .cluster_execution import _label_safe_campaign, list_jobs_with_phase
        label = (f"jobgroup=scenario-runs,"
                 f"campaign-id={_label_safe_campaign(campaign_id)}")
        # Phase is pod-accurate: a Job whose pod is still Pending (unscheduled or
        # image-pulling) reports pending, not running.
        jobs = [
            JobSummary(job_name=job.metadata.name, status=phase, kind=self._job_kind(job),
                       display_name=self._job_display_name(campaign_id, job),
                       detail=detail)
            for job, phase, detail in list_jobs_with_phase(
                self._k8s_batch(), self._k8s(), self.namespace, label)]
        # Planned jobs are the campaign's own by construction: probes queue under a separate
        # owner (see ``_PROBE_OWNER_SUFFIX``), so ``states(campaign_id)`` never yields one.
        jobs.extend(self._planned_jobs(campaign_id, {j.job_name for j in jobs}))
        runs = [j for j in jobs if j.kind != JobKind.CALIBRATION]
        counts = JobCounts(
            running=sum(1 for j in runs if j.status == "running"),
            pending=sum(1 for j in runs if j.status == "pending"),
            waiting=sum(1 for j in runs if j.status == "waiting"),
            completed=sum(1 for j in runs if j.status == "completed"),
            failed=sum(1 for j in runs if j.status == "failed"),
            blocked=sum(1 for j in runs if j.status == "blocked"),
            calibration=len(jobs) - len(runs),
            total=len(runs))
        return ListJobsResponse(jobs=jobs, counts=counts)

    def _planned_jobs(self, campaign_id, created) -> list:
        """The campaign's admitted-but-not-yet-created jobs, as ``waiting`` summaries.

        These have **no Kubernetes object at all**, so unlike every other status here they
        cannot come from a listing -- the controller is the only thing that knows they
        exist. Reporting them is what keeps ``waiting`` meaning "queued for capacity"
        rather than silently becoming a count that is always zero.

        Never *builds* the controller. This is a read path -- a job listing behind a web
        UI -- and a cluster that cannot be measured must degrade to "nothing planned"
        rather than raise; refusing is the submit path's job. If no campaign has submitted
        yet there is nothing planned either way, so the two answers agree.
        """
        from .node_admission import PLANNED

        with self._admission_lock:
            admission = self._admission
        if admission is None:
            return []
        return [JobSummary(job_name=name, status="waiting", display_name=None,
                           detail="queued for cluster capacity")
                for name, state in sorted(admission.states(campaign_id).items())
                if state == PLANNED and name not in created]

    @staticmethod
    def _job_kind(job) -> str:
        """Which kind of ``scenario-runs`` Job this is, from the label the backend stamps.

        Unlabelled means the campaign's own work: the label is stamped by
        :func:`~.kubernetes_backend.probe_manifest` and by nothing else, and a Job created
        before it existed is a run.
        """
        try:
            labels = job.metadata.labels or {}
        except AttributeError:
            return JobKind.RUN
        return (JobKind.CALIBRATION
                if labels.get(JOB_KIND_LABEL) == CALIBRATION_JOB_KIND else JobKind.RUN)

    @staticmethod
    def _job_display_name(campaign_id, job) -> "str | None":
        """The Job's ``job-name-full`` pod annotation, minus the campaign prefix."""
        try:
            full = job.spec.template.metadata.annotations.get("job-name-full")
        except AttributeError:
            return None
        if full and full.startswith(f"{campaign_id}-"):
            return full[len(campaign_id) + 1:]
        return full

    def _new_job_log_tail(self, campaign_id: str, job_name: str):
        """This lane's tail reads a pod's containers, not a job dir's files."""
        from .cluster_execution import PodLogTail
        return PodLogTail()

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a running Job's live pod log from byte *offset* onward.

        Finds the Job's pod by the auto-added ``job-name`` label and streams *all* of
        its containers' logs merged into one stream (the main ``robovast`` container
        plus any sim/SUT sidecars; see :class:`PodLogTail`). Reads are
        incremental: a cached tail keeps the full assembled text so the byte offset
        still maps onto it, but each poll only pulls the delta from the kube API
        rather than the whole log. Live source only; a missing pod raises (→ 404).

        A ``Pending`` pod is read like any other, and must be: the sim/SUT sidecars are
        native sidecars, so kubelet runs them *during* the init phase, while the pod is
        still Pending. They are already logging -- and a simulator that cannot load its
        world says so there and then keeps the pod Pending forever. Short-circuiting on
        the phase, as this did, threw away exactly the output that explains the hang.
        A container with no log yet is handled a layer down, where ``PodLogTail._fetch``
        swallows the API's 400/404 and contributes nothing.
        """
        from kubernetes import client

        from .cluster_execution import _label_safe_campaign
        core = self._k8s()
        label = (f"jobgroup=scenario-runs,"
                 f"campaign-id={_label_safe_campaign(campaign_id)},job-name={job_name}")
        pods = core.list_namespaced_pod(self.namespace, label_selector=label)
        if not pods.items:
            raise KeyError(f"no pod for job {job_name!r} in campaign {campaign_id!r}")
        pod = pods.items[0]
        tail = self._job_log_tail(campaign_id, job_name)
        try:
            with tail.lock:
                terminal = tail.read(core, pod, self.namespace, time.time())
                text, next_offset = tail.merged.slice_from(offset)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise KeyError(
                    f"pod for job {job_name!r} is gone (campaign {campaign_id!r})") from e
            raise
        return LogChunk(text=text, next_offset=next_offset, eof=terminal)

    # -- image builds (in-cluster BuildKit Job) -----------------------------

    def _image_build_state(self) -> dict:
        state = getattr(self, "_image_builds_by_id", None)
        if state is None:
            state = {}
            self._image_builds_by_id = state
        return state

    def _build_context(self, request):
        """Resolve (specs, project_dir, cfg, registry) for a build request.

        *specs* maps container name → :class:`BuildSpec`: a campaign may build several
        images. Raises ``ValueError`` (→ 400) with an actionable message when nothing
        needs building, a container's package lists are invalid, or the deployment has
        no registry configured (registry details live only in the cluster config).
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.service.image_build import extract_build_specs, validate_build_spec
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        specs = extract_build_specs(campaign_config,
                                    Path(project.config_path).parent)
        if not specs:
            raise ValueError(
                "nothing to build: no container adds system_packages, "
                "python_packages or ros_packages, so every image is used as declared")
        wanted = getattr(request, "container", None)
        if wanted:
            if wanted not in specs:
                raise ValueError(
                    f"container '{wanted}' builds no image; the ones that do are: "
                    + ", ".join(sorted(specs)))
            specs = {wanted: specs[wanted]}
        project_dir = Path(project.config_path).resolve().parent
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise ValueError(f"invalid execution.containers.{name}:\n  - "
                                 + "\n  - ".join(problems))
        cfg = self._cluster_config()
        registry = self._images.registry(require=False)
        if not registry.enabled():
            raise ValueError(f"cannot build an image: {registry.why_disabled()}")
        from .cluster_image_build import build_context_bucket
        bucket = build_context_bucket(cfg)
        return project, campaign_config, specs, project_dir, cfg, registry, bucket

    @property
    def _images(self):
        """This lane's image store: the registry this deployment pushes to.

        Overriding this one factory is what makes every image question on this lane correct,
        including the ones nobody remembered to override before — ``_exec_image`` asked the
        *local* docker daemon from inside a service pod that has none, and reported every
        built image as unbuilt.
        """
        store = getattr(self, "_image_store", None)
        if store is None:
            from .registry_image_store import RegistryImageStore
            store = RegistryImageStore(self.namespace, self._cluster_config, self._k8s)
            self._image_store = store
        return store

    def build_image(self, request):
        from robovast.service.image_build import primary_build_ref
        (_project, _cc, specs, project_dir, cfg, registry, bucket) = \
            self._build_context(request)
        refs = {name: self._start_cluster_build(spec, project_dir, cfg, registry, bucket)
                for name, spec in specs.items()}
        return primary_build_ref(refs)

    def _start_cluster_build(self, spec, project_dir, cfg, registry, bucket):
        """Core (idempotent) launch shared by build_image + the campaign preflight."""
        from robovast.common.execution import BUILD_IMAGE_PREFIX, resolve_build_base_image
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.service.image_build import cache_scope, generate_dockerfile
        from robovast.service.interface import ImageBuildRef, ImageBuildStatus

        from robovast.common.errors import ImageBuildFailed

        from .buildkitd_deploy import BUILDKITD_NAME, buildkitd_address, buildkitd_ready
        from .cluster_image_build import (build_job_manifest, cache_image_ref,
                                          context_prefix, s3_init_env, stage_context_to_s3)

        # One resolution, from the store, so a submitted build and a later "is it there?"
        # cannot disagree about what this image is called. Deriving it separately is how a
        # built image comes to be reported as unbuilt.
        found = self._images.ref_for(spec, project_dir)
        image_ref, image_hash, build_id = found.ref, found.image_hash, found.build_id
        symbolic = f"{BUILD_IMAGE_PREFIX}{spec.tag}"
        state = self._image_build_state()

        # Before anything else, and on every path (a cache hit included, or a project
        # that only ever hits the cache would never sweep): retire the contexts no
        # status poll got to — a build submitted with --no-wait and never polled, or
        # one whose service restarted mid-build.
        self._sweep_build_contexts(cfg, bucket)

        # Idempotent, and the registry is asked first: a pushed manifest for this exact
        # input hash is durable proof the image exists, where the Job that produced it is
        # deleted after ttlSecondsAfterFinished (1 h) and the in-process record dies with
        # the service. Without this the same bit-identical image was rebuilt and re-pushed
        # an hour later.
        if self._registry_has_image(found):
            status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="cached",
                                      done=True, cached=True, image_ref=symbolic,
                                      digest=image_hash)
            state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                               "hash": image_hash, "status": status}
            # Nothing will be built, so this is the only chance to warm. A cache hit is the
            # coldest case there is: the image may have been pushed weeks ago, by a service
            # that has restarted and onto a node that has rebooted since.
            self._warm(image_ref)
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=True)

        # The Job is still consulted, but only for the case the registry cannot answer:
        # a build already in flight (nothing pushed yet) that this caller should join
        # rather than duplicate.
        existing = self._existing_build_job(build_id)
        if existing == "succeeded":
            status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="cached",
                                      done=True, cached=True, image_ref=symbolic,
                                      digest=image_hash)
            state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                               "hash": image_hash, "status": status}
            # Nothing will be built, so this is the only chance to warm. A cache hit is the
            # coldest case there is: the image may have been pushed weeks ago, by a service
            # that has restarted and onto a node that has rebooted since.
            self._warm(image_ref)
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=True)
        if existing == "running":
            return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=False)
        if existing == "failed":
            # A retry after a failed build has the same content hash, hence the same
            # build_id, and the spent Job lingers for ttlSecondsAfterFinished (1 h).
            # Nothing is salvageable from it, and leaving it in place made the retry die
            # on an unhandled 409 AlreadyExists from create_namespaced_job — a 500 with
            # no message, for what is a perfectly reasonable "try again".
            self._delete_build_job(build_id)

        # Before staging anything: a build cannot happen without the daemon, and every step
        # from here on costs something (a full copy of the project tree, an upload, a Job).
        # Refusing loudly here is also the only way this failure gets named -- past this point
        # it surfaces as a gRPC dial error inside the build log, which reads like the project's
        # own build configuration being wrong and sends whoever hit it to edit a `.vast`.
        if not buildkitd_ready(self.namespace):
            raise ImageBuildFailed(
                f"the shared build daemon ({BUILDKITD_NAME}) has no ready pod in namespace "
                f"'{self.namespace}', so there is nothing to build with. Images are built by a "
                f"long-lived BuildKit daemon rather than per build, so this is a cluster fault "
                f"and not a problem with this project. Check it with "
                f"`kubectl -n {self.namespace} get deploy/{BUILDKITD_NAME}`; "
                f"`vast service upgrade` re-applies it if it is missing.")

        # Registered *before* staging so a concurrent build's context sweep can see
        # this build is in flight — its context exists in the object store for the
        # whole upload, while its Job does not exist yet.
        status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="pending",
                                  image_ref=symbolic, digest=image_hash)
        # The spec rides along so a failure can be classified against what was actually
        # asked for: without it every missing distribution looks like a bad entry in
        # build.python_packages, including the ones the base image should have carried.
        state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                           "hash": image_hash, "status": status, "spec": spec}

        # Everything up to a created Job is undone on failure: the in-flight record
        # holds the sweep back, so a submit that dies here (staging error, rejected
        # Job) would otherwise strand its context for as long as the service lives.
        try:
            # Stage the context (project dir + generated Dockerfile) to S3.
            base_ref = (spec.base_image or registry.base_experiment_image
                        or resolve_build_base_image())
            # Record the *resolved* base, not the declared one: spec.base_image is often
            # empty (the cluster default or the framework image supplied it), and an
            # error that cannot name the image it built on is the harder one to act on.
            # Guarded because this only sharpens a future error message -- failing the
            # submit itself over it would trade something that matters for something
            # that does not.
            if dataclasses.is_dataclass(spec):
                state[build_id]["spec"] = dataclasses.replace(spec, base_image=base_ref)
            # The same resolution the hash was taken over, so the Dockerfile installs the
            # commit rather than the branch. Rendering without it left the build installing
            # whatever the ref pointed at when the Job ran -- which the campaign's record then
            # could not name, the failure the resolution exists to prevent.
            dockerfile = generate_dockerfile(spec, project_dir, base_ref,
                                             resolved_vcs=self._images.resolve_vcs(spec))
            build_prefix = context_prefix(build_id)
            storage = in_pod_storage.storage_client_for(cfg)
            context_bytes = stage_context_to_s3(storage, bucket, build_prefix,
                                                project_dir, dockerfile)

            # Scoped to this build's layer-chain shape, not just the container name:
            # otherwise every project's `sut` shares one tag and evicts the others' layers.
            # `base_ref` is the resolution the hash was taken over, so the scope and the
            # build agree on what they are built on.
            cache_ref = cache_image_ref(registry.registry_prefix, spec.tag,
                                        cache_scope(spec, base_ref))
            # The two fixed costs BuildKit's output never names — see the header
            # `_await_build_image` writes into the campaign's build.log.
            status.context_bytes, status.cache_ref = context_bytes or 0, cache_ref

            access_key, secret_key = cfg.get_s3_credentials()
            init_env = s3_init_env(cfg.get_s3_endpoint(), access_key, secret_key,
                                   bucket, build_prefix)
            manifest = build_job_manifest(
                build_id=build_id, image_ref=image_ref, campaign_label=build_id,
                init_env=init_env, push_secret_name=registry.push_secret_name,
                namespace=self.namespace, insecure=registry.insecure,
                ca_configmap_name=registry.ca_configmap_name,
                cache_ref=cache_ref,
                host_aliases=cfg.get_host_aliases(),
                # Already resolved on this object (registry_image_store fills it from the
                # push Secret, which serves both directions), so no second lookup.
                pull_secret_name=registry.pull_secret_name or "",
                # The token a private `python_packages` git spec installs with. Looked up
                # rather than assumed: naming a Secret that does not exist would keep the
                # build pod from starting, which is a worse failure than building without
                # a credential no spec here needs.
                git_secret_name=self._images.git_secret_name(),
                # Which builder this client dials. A Service name, so a daemon replaced
                # between submit and start is still reachable at the same address.
                daemon_addr=buildkitd_address(self.namespace))
            self._k8s_batch().create_namespaced_job(self.namespace, manifest)
        except BaseException:
            status.phase, status.done = "failed", True
            self._discard_build_context(cfg, bucket, build_id)
            raise
        status.phase = "building"
        # The base is most of the built image: every experiment image is FROM a family
        # member, and containerd's content store is digest-addressed, so warming it now
        # means the pull after the build moves only this spec's own apt/pip layers. Free,
        # because a build takes minutes and nothing is waiting on the node yet.
        self._warm(base_ref)
        return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=False)

    def _discard_build_context(self, cfg, bucket: str, build_id: str) -> None:
        """Drop *build_id*'s staged context. Best-effort: a leftover copy of the
        project dir is not worth failing a finished build over, but it is worth a
        warning, since the next sweep is the only thing that will retry it."""
        from robovast.execution.cluster_execution import in_pod_storage

        from .cluster_image_build import discard_context
        try:
            storage = in_pod_storage.storage_client_for(cfg)
            removed = discard_context(storage, bucket, build_id)
        except Exception as e:  # noqa: BLE001 - cleanup must not fail the build
            logger.warning("could not discard the staged build context for %s: %s",
                           build_id, e)
            return
        if removed:
            logger.info("discarded the staged build context for %s (%d objects)",
                        build_id, removed)

    def _sweep_build_contexts(self, cfg, bucket: str) -> None:
        """Discard staged contexts whose build is over.

        A context is stale when no build Job owns it any more (Jobs self-destruct at
        ``ttlSecondsAfterFinished``, so an absent Job means the build ended at least
        that long ago — or died with a previous service instance). Builds this process
        still has in flight are held back explicitly: theirs is staged before their Job
        exists, so "no Job" alone would delete a context out from under a sibling
        request's init container.
        """
        from robovast.execution.cluster_execution import in_pod_storage

        from .cluster_image_build import staged_context_build_ids
        try:
            storage = in_pod_storage.storage_client_for(cfg)
            staged = staged_context_build_ids(storage, bucket)
            jobs = self._k8s_batch().list_namespaced_job(
                self.namespace, label_selector="jobgroup=image-builds").items
        except Exception as e:  # noqa: BLE001 - cleanup must not fail the build
            logger.warning("could not sweep stale build contexts: %s", e)
            return
        live = {(job.metadata.labels or {}).get("build-id") for job in jobs}
        # Snapshot: a concurrent submit inserting into the state dict must not turn
        # this into "dictionary changed size during iteration".
        live |= {bid for bid, rec in list(self._image_build_state().items())
                 if not rec["status"].done}
        for build_id in sorted(staged - live):
            self._discard_build_context(cfg, bucket, build_id)

    def _registry_has_image(self, found) -> bool:
        """Is *found* already pushed? The **build** path's fail-closed view of the store.

        ``ImageBuildStore.present`` raises when the registry could not be asked, because a
        caller deciding whether it can *run* an image must never read that as "not built".
        The caller deciding whether to *build* one wants the opposite trade, and always did:
        uncertainty means rebuild, which costs a redundant push, where a wrong cache hit
        leaves the campaign's pods in ImagePullBackOff with the build long finished.
        """
        from robovast.common.errors import ImageStoreUnavailable
        try:
            return self._images.present(found)
        except ImageStoreUnavailable as e:
            logger.warning("treating %s as not yet pushed: %s", found.identity, e)
            return False

    def _delete_build_job(self, build_id: str, timeout_s: float = 60.0) -> None:
        """Delete a spent build Job (and its pods) and wait until it is really gone.

        The wait matters: ``create_namespaced_job`` right after a delete request still
        races the API server's cleanup and would 409 again.
        """
        from kubernetes import client
        batch = self._k8s_batch()
        try:
            batch.delete_namespaced_job(
                build_id, self.namespace,
                grace_period_seconds=0, propagation_policy="Background")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                batch.read_namespaced_job(build_id, self.namespace)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    logger.info("removed the previous failed build Job %s", build_id)
                    return
                raise
            time.sleep(1.0)
        raise RuntimeError(
            f"the previous failed build Job '{build_id}' did not disappear within "
            f"{timeout_s:.0f}s; delete it manually and retry")

    def _existing_build_job(self, build_id: str) -> "str | None":
        """Return 'succeeded' | 'failed' | 'running' for an existing Job, else None."""
        from kubernetes import client
        batch = self._k8s_batch()
        try:
            job = batch.read_namespaced_job(build_id, self.namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise
        st = job.status
        if st and st.succeeded:
            return "succeeded"
        if st and st.failed:
            return "failed"
        return "running"

    def get_image_build_status(self, build_id: str):
        from robovast.service.interface import ImageBuildStatus
        state = self._image_build_state()
        record = state.get(build_id)
        if record is None:
            # Not tracked in-process (e.g. after a restart): derive from the Job.
            phase = self._existing_build_job(build_id)
            if phase is None:
                raise KeyError(f"unknown build '{build_id}'")
            done = phase in ("succeeded", "failed")
            if done:
                # A build from a previous service instance: no record memoizes the
                # transition, so this repeats per poll — a no-op list once the prefix
                # is gone, and it beats waiting for the next build to sweep it.
                self._retire_build_context(build_id)
                # Deliberately no prewarm here, though this is a "the image exists now"
                # transition like the others. All this branch has is the build_id, and
                # `build_id_for` is not reversible into a ref: it lowercases and folds `_`
                # to `-`, which `concrete_image_ref` does not, so a tag like `my_sut` would
                # yield a ref that no registry serves. The prewarm would then sit in
                # ImagePullBackOff until its deadline, warming nothing and saying nothing,
                # since nothing reads a prewarm back. The next submit for this spec takes
                # the cache-hit path above and warms from a properly resolved ref.
                return ImageBuildStatus(
                    build_id=build_id, phase=phase, done=done,
                    cached=phase == "succeeded")
            # A restarted service must reach the same verdict about a stuck pod as the
            # one that submitted the build; the probe below needs no record to do it.
            blocked, failed = self._build_pod_verdict(build_id)
            if failed is not None:
                self._retire_build_context(build_id)
                return ImageBuildStatus(build_id=build_id, phase="failed", done=True,
                                        error=failed)
            return ImageBuildStatus(
                build_id=build_id, phase="blocked" if blocked else phase, done=False,
                error=blocked)
        status: ImageBuildStatus = record["status"]
        if status.done:
            return status
        phase = self._existing_build_job(build_id)
        if phase == "succeeded":
            status.phase = "succeeded"
            status.done = True
        elif phase == "failed":
            status.phase = "failed"
            status.done = True
            status.error = self._build_error(build_id, record.get("spec"))
        else:
            # Still active as far as the Job is concerned — which it will remain forever if
            # its pod cannot start, since `backoffLimit: 0` and no `activeDeadlineSeconds`
            # leave both counters at zero. That was a wait that never returned.
            blocked, failed = self._build_pod_verdict(build_id)
            if failed is not None:
                status.phase = "failed"
                status.done = True
                status.error = failed
            elif blocked is not None:
                status.phase = "blocked"
                status.error = blocked
            elif status.phase == "blocked":
                # It cleared on its own -- the transient blip the grace window is for.
                status.phase, status.error = "building", None
        if status.done:
            # This transition is the one moment we know the context is dead, for both
            # outcomes. Cheap (a prefix delete) and it runs once, since a done record
            # returns above.
            self._retire_build_context(build_id)
            if status.phase == "succeeded":
                # Same transition, and the fire point that earns the feature: the image
                # exists now, nobody has pulled it yet, and what needs pulling is precisely
                # the layers this build added on top of the base warmed at submit.
                self._warm(record["image_ref"])
        return status

    def _build_pod_verdict(self, build_id: str):
        """``(blocked, failed)`` for a build whose Job is still active, both ``ImageBuildError``.

        ``(None, None)`` — the pod is fine, or there is none yet. ``(blocked, None)`` — it
        cannot start, but not yet for long enough to call it. ``(None, failed)`` — it will
        not recover.

        **The grace window is the pod's own age, not a timer this method keeps.** Holding a
        ``blocked_since`` stamp across calls would make the verdict depend on how often
        someone polls, lose it whenever the service restarts, and require the "a failed probe
        must not clear the timer" discipline the campaign batch loop has to state explicitly.
        Kubernetes already records when the pod appeared, so asking it removes the state and
        the hazard together. ``pod_block_reason`` never fires on ``ContainerCreating`` or
        ``PodInitializing``, so age here does not punish a slow legitimate pull.
        """
        import datetime

        from .cluster_execution import BLOCKED_GRACE_SECONDS, pod_block_reason
        try:
            pod = self._build_pod(build_id)
        except Exception as e:  # noqa: BLE001 - one dropped read is not a verdict
            # Explicitly not "not blocked": saying so would end the build on the next
            # succeeded/failed check as if the pod were healthy. The next poll asks again.
            logger.warning("could not check whether build %s can start: %s", build_id, e)
            return None, None
        if pod is None:
            return None, None
        blocked = pod_block_reason(pod)
        if blocked is None:
            return None, None
        reason, message = blocked
        # ``start_time`` is set once the kubelet accepts the pod; ``creation_timestamp``
        # covers the window before that (an unschedulable pod never gets the former).
        started = (getattr(pod.status, "start_time", None)
                   or getattr(pod.metadata, "creation_timestamp", None))
        age = None
        if started is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
            age = (now - started).total_seconds()
        detail = f"{reason}: {message}" if message else reason
        container = self._blocked_container(pod)
        # No timestamp at all (Kubernetes always sets one, so: a substrate we do not
        # recognise) means the window cannot be measured. Act on the reason rather than
        # granting an unmeasurable grace, which is the indefinite wait this replaces --
        # the block itself was observed either way.
        if age is not None and age < BLOCKED_GRACE_SECONDS:
            # Reported as `blocked`, with its diagnosis, rather than silently waited out --
            # so the reason reaches the caller on its first poll instead of a minute later.
            logger.warning("build %s cannot start yet (%s)", build_id, detail)
            return (self._blocked_build_error(build_id, reason, message, container,
                                              terminal=False),
                    None)
        logger.error("build %s cannot start and will not recover (%s)", build_id, detail)
        return None, self._blocked_build_error(build_id, reason, message, container,
                                               terminal=True)

    @staticmethod
    def _blocked_container(pod) -> str:
        """Which container of *pod* cannot pull, or ``""`` for an unschedulable pod.

        Named so the error can say *which* registry is unreachable: the sidecar and BuildKit
        come from different ones and are fixed in different places.

        This re-walks the statuses ``pod_block_reason`` just matched, because that function
        reports the reason and not where it came from -- a signature every campaign caller
        shares and none of them needs widened. The two agree by construction: same statuses,
        same order, same :data:`POD_BLOCKED_REASONS`. An unschedulable pod matches nothing
        here, which is the empty string, and the caller reads that as "not a container".
        """
        from .cluster_execution import POD_BLOCKED_REASONS
        statuses = list(getattr(pod.status, "init_container_statuses", None) or []) + \
            list(getattr(pod.status, "container_statuses", None) or [])
        for cs in statuses:
            state = getattr(cs, "state", None)
            waiting = getattr(state, "waiting", None) if state else None
            if waiting and getattr(waiting, "reason", None) in POD_BLOCKED_REASONS:
                return getattr(cs, "name", None) or ""
        return ""

    def _retire_build_context(self, build_id: str) -> None:
        """Discard a just-finished build's staged context, resolving the bucket."""
        from .cluster_image_build import build_context_bucket
        try:
            cfg = self._cluster_config()
            bucket = build_context_bucket(cfg)
        except Exception as e:  # noqa: BLE001 - cleanup must not fail a status read
            logger.warning("cannot resolve the build-context bucket for %s: %s",
                           build_id, e)
            return
        self._discard_build_context(cfg, bucket, build_id)

    def _warm(self, image_ref: str) -> None:
        """Pull *image_ref* onto a node now, so the next pod to run it does not wait.

        A built image is in the registry and on no node, so whoever runs it first pays the
        whole pull -- and that is usually ``exec_in_container``, which exists to answer a
        question in seconds. See :mod:`.image_warm`; this method is only the seam that keeps
        the call sites one line each and makes the failure mode uniform.

        **Best-effort by construction, and that is not laziness.** A failed prewarm leaves
        exactly the situation that held before it existed: a slow first pod. Raising here
        would turn a missed optimization into a failed build, so the bare ``except`` is the
        correct trade -- but it warns, because a prewarm that never works is invisible
        otherwise (nothing reads it back, by design).
        """
        from .image_warm import warm_image
        try:
            warm_image(self._k8s_batch(), self.namespace, image_ref,
                       self._registry_pull_secret())
        except Exception as e:  # noqa: BLE001 - a prewarm must never fail its caller
            logger.warning("could not prewarm %s: %s", image_ref, e)

    def _build_error(self, build_id: str, spec=None):
        """Classify a failed build. *spec* is what the build was asked to install.

        Took a ``tag`` it never used; that slot now carries the spec, which the
        classifier does use -- without it a dependency missing from the base image is
        indistinguishable from a bad ``build.python_packages`` entry.
        """
        from robovast.service.image_build import classify_build_error
        log = self._build_log_text(build_id)
        return classify_build_error(log, spec)

    def _build_pod(self, build_id: str):
        """*build_id*'s builder pod, or ``None`` if it has none yet.

        One lookup for the two questions asked of that pod — what did the build print, and
        why can it not start — so they cannot disagree about which pod they mean. **Raises**
        on an API error rather than returning ``None``: a caller deciding whether the pod is
        blocked must not read "could not ask" as "not blocked".
        """
        pods = self._k8s().list_namespaced_pod(
            self.namespace, label_selector=f"build-id={build_id}")
        return pods.items[0] if pods.items else None

    #: Containers of the build pod, and what a failed pull of each one means. The reason
    #: Kubernetes reports is the same either way, but the fix is not, and naming the wrong
    #: one sends the reader to the wrong registry.
    _BUILD_CONTAINER_HINTS = {
        "context-fetch": (
            "the build infrastructure image (robovast-sidecar) could not be pulled. Either "
            "it is not in the registry this deployment points at, or the build Job has no "
            "credential for it -- check the image project/tag the service resolves "
            "(ROBOVAST_PROJECT / ROBOVAST_PROJECT_TAG) and the registry pull Secret. "
            "Nothing about the project's build: section is involved"),
        "buildkit": (
            "the BuildKit builder image could not be pulled, so the cluster has no path to "
            "the public registry it comes from (egress, or a misconfigured pull-through "
            "mirror). Nothing about the project's build: section is involved"),
    }

    def _blocked_build_error(self, build_id: str, reason: str, message: str,
                             container: str, terminal: bool):
        """The structured error for a builder pod that cannot start.

        Carried while the build is still ``blocked`` as well as once it has ``failed``, so the
        reason reaches the caller on its first poll rather than after the grace window: a
        status that says only "blocked" repeats the original complaint, which was an agent
        with no idea what had happened. *terminal* is what separates "not yet" from "not
        going to".

        Deliberately **not** ``classify_build_error``: that reads the builder's output, and a
        pod that never started produced none, so every such failure classified as the generic
        "the image build failed; see the log tail" — pointing an agent at ``build:``, which is
        the one thing that cannot be at fault here. Kubernetes' own message names the image
        and the registry error; the hint names the knob.
        """
        from robovast.service.interface import ImageBuildError

        from .cluster_execution import BLOCKED_GRACE_SECONDS
        hint = self._BUILD_CONTAINER_HINTS.get(container)
        if hint is None:
            # An unschedulable pod has no offending container -- the scheduler never got
            # that far -- and its message is the per-node accounting, which is the diagnosis.
            hint = ("the cluster could not place the build pod; the message above names the "
                    "resource no node can satisfy. This is capacity, not the project's "
                    "build: section")
        detail = f"{reason}: {message}" if message else reason
        if terminal:
            lead = f"the build pod cannot start -- {detail}"
            # The log is worth an extra read only here: the terminal error is what someone
            # reads, and during the grace window this would cost two API calls per poll.
            tail = self._build_log_text(build_id)
        else:
            lead = (f"the build pod cannot start yet -- {detail}. It fails if this has not "
                    f"cleared {BLOCKED_GRACE_SECONDS:.0f}s after the pod appeared")
            tail = ""
        return ImageBuildError(phase="builder-pod", fixable_by="infra",
                               message=f"{lead}. In short: {hint}", log_tail=tail)

    def _build_log_text(self, build_id: str) -> str:
        """The builder's own output, or the best available substitute.

        Falls back from the build container to the init container to the reason the pod
        cannot start, because the empty string is the one answer that is never useful: a
        failed build sends its reader here, and a pod that never ran ``buildctl`` has no
        ``buildkit`` log to give -- which is exactly the case where "read the log" was the
        advice and "" was the log.
        """
        from kubernetes import client

        from .cluster_execution import pod_block_reason
        core = self._k8s()
        try:
            pod = self._build_pod(build_id)
        except Exception as e:  # noqa: BLE001 - a log read must not fail a status poll
            logger.debug("could not find the build pod for %s: %s", build_id, e)
            return ""
        if pod is None:
            return ""
        for container in ("buildkit", "context-fetch"):
            try:
                text = core.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=self.namespace,
                    container=container)
            except client.exceptions.ApiException:
                continue
            if text:
                return text
        blocked = pod_block_reason(pod)
        if blocked:
            reason, message = blocked
            return f"{reason}: {message}\n" if message else f"{reason}\n"
        # Nothing from the client, so the reason is somewhere the client cannot see. Since the
        # solve happens in the shared daemon, a whole class of failure -- GC, a full store, a
        # solve killed for memory -- leaves the client's log empty and its own log holding the
        # only account of it. Without this the reader gets "" for a build that failed for a
        # reason that was written down.
        return self._daemon_log_tail()

    def _daemon_log_tail(self, lines: int = 50) -> str:
        """The build daemon's recent output, labelled as its own.

        Labelled because it is not this build's log and must not read as one: the daemon is
        shared, so what is in here may belong to a concurrent build. It is offered as the last
        resort it is -- a lead, not an account.
        """
        from kubernetes import client

        from .buildkitd_deploy import BUILDKITD_NAME

        try:
            pods = self._k8s().list_namespaced_pod(
                self.namespace, label_selector=f"app={BUILDKITD_NAME}")
            if not pods.items:
                return (f"no output from the build client, and no {BUILDKITD_NAME} pod to ask "
                        f"-- the shared build daemon is not running.\n")
            text = self._k8s().read_namespaced_pod_log(
                name=pods.items[0].metadata.name, namespace=self.namespace,
                container="buildkitd", tail_lines=lines)
        except client.exceptions.ApiException as e:
            logger.debug("could not read the build daemon's log: %s", e)
            return ""
        if not text:
            return ""
        return (f"--- no output from the build client; last {lines} lines from the shared "
                f"build daemon ({BUILDKITD_NAME}), which may include other builds ---\n{text}")

    def get_image_build_log(self, build_id: str, offset: int = 0):
        raw = self._build_log_text(build_id).encode("utf-8", "replace")
        record = self._image_build_state().get(build_id)
        done = bool(record and record["status"].done)
        return LogChunk(text=raw[offset:].decode("utf-8", "replace"),
                        next_offset=len(raw), eof=done)

    def _campaign_build_context(self, project, campaign_config, image_project=None,
                                image_project_tag=None):
        """``(spec, project_dir, cfg, registry)`` for a campaign's ``build:`` image, or
        ``None`` when it has none. Works from the already-resolved project/config, unlike
        :meth:`_build_context`, which resolves a standalone ``build_image`` request.

        Raises :class:`CampaignConfigError` — *not* ``ValueError`` as the request path
        does — because this runs on the campaign's worker thread, where the failure is
        recorded as the campaign's outcome rather than answered as a 400. An
        unconfigured registry and a broken ``build:`` section are both bad input with a
        self-contained message, so the campaign fails with that message alone; a
        ``ValueError`` fell through to the worker's catch-all and printed a stack trace,
        which reads as a RoboVAST bug rather than as something to go and configure.
        """
        from robovast.common.errors import CampaignConfigError
        from robovast.service.image_build import extract_build_specs, validate_build_spec
        project_dir = Path(project.config_path).resolve().parent
        # Same ordering as _build_specs_for: the campaign's own plugins may carry the
        # simulator backend that decides which container builds, so they have to be
        # resolvable before the specs are read, and base_dir has to be passed for a
        # file-ref backend to resolve at all.
        specs = extract_build_specs(campaign_config, base_dir=str(project_dir),
                                    image_project=image_project,
                                    image_project_tag=image_project_tag)
        if not specs:
            return None
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise CampaignConfigError(
                    f"invalid execution.containers.{name}:\n  - " + "\n  - ".join(problems))
        cfg = self._cluster_config()
        registry = self._images.registry(require=False)
        if not registry.enabled():
            # The leading clause is this site's own: a campaign author needs to hear that
            # their *campaign* is what asked for a build. The rest is shared.
            raise CampaignConfigError(
                f"this campaign builds a container image, but {registry.why_disabled()}")
        return specs, project_dir, cfg, registry

    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None) -> list:
        """Submit (or join) an in-cluster BuildKit Job per image this campaign builds.

        Returns as soon as each build has a handle; ``LocalTransport._await_build_image``
        waits on them over the interface, so both lanes share one wait loop.
        """
        resolved = self._campaign_build_context(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag)
        if resolved is None:
            return []
        specs, project_dir, cfg, registry = resolved
        from .cluster_image_build import build_context_bucket
        bucket = build_context_bucket(cfg)
        return [self._start_cluster_build(spec, project_dir, cfg, registry, bucket)
                for spec in specs.values()]

    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None) -> dict:
        """Concrete registry refs to pin, by container name."""
        specs, project_dir, _cfg, registry = self._campaign_build_context(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag)
        del registry            # the store carries the registry the refs are formed against
        return {name: self._images.ref_for(spec, project_dir).ref
                for name, spec in specs.items()}

    # ``list_campaigns`` is inherited from LocalTransport. Its id set is "on disk ∪ durable
    # ∪ being driven", and this lane contributes the middle one via
    # ``_durable_campaign_ids``: off-cluster the driver runs in this process and writes each
    # campaign under the local results dir, so the disk scan already covers those, but in-pod
    # the disk is scratch and the object store's index is the only record that a campaign
    # from a previous service life exists. Each id is then resolved by the same precedence
    # ``get_status`` uses — live snapshot if tracked, else its records via ``_record_dir``.

    def stop(self, campaign_id: str) -> ActionResult:
        """Stop a campaign this process is driving.

        The driver is in this process, so the cooperative flag is a direct state
        write. That flag alone only ends a *search* between generations, though — a
        batch campaign's wait loop blocks until its Jobs finish on their own, so the
        flag would appear to do nothing. We therefore also tear down the campaign's
        cluster workloads (the same cleanup ``vast cluster
        jobs-cleanup`` performs): the running pods terminate now, the batch wait loop
        unblocks (``get_remaining_jobs`` treats a gone Job as finished), and the
        driver winds the campaign down.

        A campaign still in ``building`` is stopped by the flag alone: that teardown is
        label-scoped to ``jobgroup=scenario-runs`` and cannot reach the
        ``jobgroup=image-builds`` Job. That is deliberate and must stay true — an image
        build is content-addressed and therefore shared, so cancelling it could strand a
        sibling campaign waiting on the same image, and the image is a cache entry rather
        than this campaign's property. ``_await_build_image`` detaches instead.
        """
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(
                ok=False, message=f"campaign {campaign_id} is not running here")
        entry.state.request_stop()
        self._teardown_campaign_jobs(campaign_id)
        return ActionResult(ok=True, message="stop requested; in-flight jobs terminated")

    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        """The inherited reads, pointed at this job's pod instead of a local container.

        Everything that decides *what* is asked -- the simulator's own command, the scenario's own
        tree reader, which container each belongs in, the JSON passed through unreshaped, what
        "unavailable" means, and the TTL that makes one check serve every watcher -- is
        :class:`LocalTransport`'s and shared. Only the target differs, which is the whole reason
        ``exec_in`` takes one.

        Two lane facts shape it. ``job_name`` here is the **Kubernetes Job** name rather than a run
        key, so it cannot be turned into ``/out/<config>/<run>``; and a Job may pack several runs,
        so there is no single run dir to name even in principle. But ``/out`` is *this pod's own*
        emptyDir, holding only this job's runs -- so naming it is exact rather than vague, and every
        reader finds the run still being written underneath it, whichever container it is asked in.

        Raises ``KeyError`` for a job between scheduling and running, or already gone; the callers
        turn that into a stated reason rather than an empty answer.
        """
        return self._job_pod_target(campaign_id, job_name, role), "/out"

    #: The run dirs inside a Job, newest first. Only **real** run dirs: a campaign root holds
    #: ``_jobs/`` beside them (and the other names in
    #: :data:`~robovast.common.campaign_data.RESERVED_CAMPAIGN_DIRS`), and an earlier version of
    #: this took the newest file anywhere under the root -- which was reliably a job artifact, so
    #: it named the run ``_jobs/batch-0`` and pointed every reader at a subtree with no run in it.
    #:
    #: The shape is the filter: ``<config>/<run-number>``, the run number being digits. Matched on
    #: the layout rather than on a list of names to exclude, because the layout is what the readers
    #: below depend on and a new reserved name would silently pass an exclusion list.
    _LIVE_RUN_FIND = ("find {root} -mindepth 2 -maxdepth 2 -type d "
                      "-regex '.*/[^/]+/[0-9]+' -printf '%T@ %P\\n' "
                      "| sort -rn | head -1 | cut -d' ' -f2-")

    def _job_live_run(self, campaign_id: str, job_name: str, target, run_dir: str) -> tuple:
        """``(run_dir, run_key)`` for the run this Job is on. Always resolved, never delegated.

        **Which run a job is on is RoboVAST's question, and it gets answered here.** Answering it
        only for a Job that packs several runs hands an unpacked one ``/out`` and leaves the readers
        to find the run underneath it. Both of them can -- ``tree_state`` and ``roqsim health`` each
        search a couple of levels down -- and that is exactly the problem:

        * it is two other components modelling *this* layout, and a layout guessed in two places is
          free to disagree with the one place that owns it;
        * "the newest one below here" is a heuristic answering a question they cannot see the answer
          to, while the service can;
        * and it **masks** a wrong directory instead of failing on it. Pointed at ``_jobs/batch-0``,
          a reader searches around it and then reports that the scenario may have run without
          ``--bt-log`` -- a confident wrong cause for a path bug.

        So the exact run dir goes out, every time, and ``run`` names it in the reply. One ``find``
        over the pod's own emptyDir per read, which is nothing beside the reads it precedes -- one
        resolution in place of two heuristics.

        A discovery that finds nothing leaves ``/out`` in place: a job between starting and its
        first record is normal, and the readers' own "nothing here yet" is a better answer than a
        failure from the step that was only trying to be more precise.
        """
        del campaign_id
        from robovast.service.local_transport import _JOB_STATE_LIMIT_S
        command = self._LIVE_RUN_FIND.format(root=shlex.quote(run_dir))
        _code, stdout, _stderr, timed_out = self._exec_lane().exec_in(
            target, ["/bin/bash", "-c", command], _JOB_STATE_LIMIT_S)
        if timed_out:
            return run_dir, None
        run_key = (stdout or "").strip()
        if not run_key or run_key.count("/") != 1:
            return run_dir, None
        return f"{run_dir.rstrip('/')}/{run_key}", run_key

    def _job_output_dir(self, campaign_id: str, job_name: str, run_dir: str) -> str:
        """This Job's own ``OUTPUT_DIR``, which is where its resource samples and logs are.

        Read off the Job rather than resolved through the campaign manifest, and rather than
        derived from the layout: the backend **stamps it on the pod** as
        ``OUTPUT_DIR=/out/_jobs/<batch>/job-<idx>``, so reading it back is exact for a running job
        and needs no exec at all. :meth:`_job_artifact_dir` already does that read for the
        intervention ledger; this is the same fact, wanted for the same job, so it is the same
        read.
        """
        del run_dir
        rel = self._job_artifact_dir(job_name)
        return f"/out/{rel.strip('/')}" if rel else "/out"

    def _job_pod_target(self, campaign_id: str, job_name: str, role: str = SCENARIO_CONTAINER):
        """``(pod, container)`` for one running job's *role*, or raise saying why not.

        The scenario is the pod's first workload container and the sidecars follow it in
        declaration order, so it resolves to a position rather than to a name assembled here. Read
        from the live pod for the same reason the log tail reads it: the manifest owns those names.

        **Through :func:`~.kube_client.pod_workload_containers`, which is not optional.** The
        simulator and the system under test are *native sidecars* -- ``initContainers`` with
        ``restartPolicy: Always`` -- so ``pod.spec.containers`` holds the scenario container and
        nothing else. Asking it directly made this the fourth place to get that wrong in the same
        way (see that function's docstring for the other three): every role but ``scenario`` is
        refused as "this job runs no such container" on a pod that is visibly running three, and the
        refusal quotes a one-name list as its evidence.

        **The pod decides, and the plan is only consulted for a role the pod does not name.** That
        order is the whole point. Asking the plan first resolves ``simulation`` to the scenario
        container for a campaign whose archived config cannot be read -- or whose block simply names
        no simulator -- and the read then enters a container with no simulator in it. Confidently,
        and with no way for the caller to tell. A pod that *has* a container called ``simulation``
        is not a thing the config can outvote.

        The plan still answers the case the pod cannot: a simulator stepped in-process **is** the
        scenario container, so there is no container of that name to find and refusing the role
        would deny a read the campaign can answer.
        """
        from robovast.common.config import CONTAINER_ROLES

        from .cluster_execution import _label_safe_campaign
        from .kube_client import pod_workload_containers
        if role not in CONTAINER_ROLES:
            raise ValueError(f"unknown container role {role!r}; expected one of "
                             f"{', '.join(CONTAINER_ROLES)}")
        label = (f"jobgroup=scenario-runs,"
                 f"campaign-id={_label_safe_campaign(campaign_id)},job-name={job_name}")
        pods = self._k8s().list_namespaced_pod(self.namespace, label_selector=label)
        if not pods.items:
            raise KeyError(f"no pod for job {job_name!r} in campaign {campaign_id!r}: it is "
                           f"between scheduling and running, or already gone")
        pod = pods.items[0]
        names = [c.name for c in pod_workload_containers(pod)]
        if not names:
            raise KeyError(f"pod for job {job_name!r} declares no workload containers")
        # The scenario is the pod's first workload container, by position: the manifest owns what
        # it is called, so there is no name to match on.
        if role == SCENARIO_CONTAINER:
            return pod.metadata.name, names[0]
        if role in names:
            return pod.metadata.name, role
        if self._plan_role(campaign_id, role) == SCENARIO_CONTAINER:
            return pod.metadata.name, names[0]
        raise KeyError(f"this job runs no {role!r} container; it has: {', '.join(names)}")

    def exec_in_job(self, campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> "ExecResult":
        """The inherited probe, pointed at this job's pod.

        Same recording, same ordering, same refusal for a job that is not running, same environment
        -- only the target differs, which is what ``exec_in`` exists for. The container is resolved
        from the pod rather than from a name built here: a role maps to a *position* in the pod spec
        (the scenario runs first), and the concrete names are the manifest's business.
        """
        from robovast.common.campaign_data import KIND_PROBED, record_intervention
        from robovast.common.execution import in_run_env
        from robovast.service.interface import ExecResult

        if not (command or "").strip():
            raise ValueError("exec_in_job needs a command: there is no scenario to start here, "
                             "only a live job to look at.")
        self._require_running_job(campaign_id, job_name)
        campaign_root = self._campaigns_root() / campaign_id
        record_intervention(campaign_root, kind=KIND_PROBED,
                            job_dir=self._job_artifact_dir(job_name), job_name=job_name,
                            source=source, detail=command)
        # Mirrored at once, for the reason the kill is: postprocessing runs as its own in-cluster
        # Job before the campaign root is uploaded, so a probe recorded only on pod disk would be
        # lost exactly when the results are assembled.
        self._publish_interventions(campaign_id, campaign_root)
        from robovast.service.local_transport import _PROBE_LIMIT_S
        pod, pod_container = self._job_pod_target(campaign_id, job_name, container)
        exit_code, stdout, stderr, timed_out = self._exec_lane().exec_in(
            (pod, pod_container), in_run_env(command), _PROBE_LIMIT_S)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                          timed_out=timed_out, limit_s=_PROBE_LIMIT_S, limit_source="command")

    def stop_job(self, campaign_id: str, job_name: str,
                 reason: "str | None" = None, source: str = "api") -> ActionResult:
        """Delete one running scenario Job; its siblings and the batch keep going.

        Deliberately **not** ``request_stop()`` and **not** ``_teardown_campaign_jobs``:
        the flag ends the campaign and the teardown is label-scoped to *every* Job of it.
        A single ``delete_namespaced_job`` is enough because the batch wait loop treats a
        gone Job as finished (``get_remaining_jobs``), so the remaining Jobs run to
        completion and the batch still projects its results.

        ``Background`` propagation so the pod is collected with the Job, through its
        owner reference. Whatever this job's runs had already
        uploaded to the object store survives: each job uploads its own results.
        """
        from kubernetes import client

        from robovast.common.campaign_data import KIND_KILLED, record_intervention
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            raise KeyError(f"campaign {campaign_id!r} is not running here")
        self._require_running_job(campaign_id, job_name)
        job_dir = self._job_artifact_dir(job_name)
        # Recorded before the delete: the pod dies asynchronously, and a failure in
        # between must not leave a cut-short run with no record of why.
        campaign_root = self._campaigns_root() / campaign_id
        record_intervention(campaign_root, kind=KIND_KILLED, job_dir=job_dir, job_name=job_name,
                            source=source, detail=reason)
        self._publish_interventions(campaign_id, campaign_root)
        try:
            self._k8s_batch().delete_namespaced_job(
                job_name, self.namespace,
                grace_period_seconds=0, propagation_policy="Background")
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            # It finished between the precondition and here. The record stands: its runs
            # either delivered results (and keep their real verdict) or did not.
            return ActionResult(ok=True, message=f"job {job_name} was already gone")
        return ActionResult(
            ok=True,
            message=(f"deleted job {job_name}; the campaign continues with its remaining "
                     f"jobs and this job's unfinished runs are recorded as 'killed'"))

    def _publish_interventions(self, campaign_id: str, campaign_root) -> None:
        """Push the kill ledger to the object store **now**, not at finalize.

        Postprocessing runs as its own in-cluster Job reading the campaign from the object
        store, and it starts *before* ``_finalize`` uploads the campaign root — so a ledger
        that waited for finalize would reach the store only after the step that needs it
        had already failed on the killed job's unfinalized rosbag.

        Best-effort, like every other record this lane publishes: a kill that could not be
        mirrored still took effect and is still on local disk, and the run is still
        recorded as ``killed`` by the controller, which reads that disk.
        """
        from robovast.common.campaign_data import _INTERVENTIONS_FILENAME
        from robovast.execution.cluster_execution import in_pod_storage
        path = campaign_root / "_execution" / _INTERVENTIONS_FILENAME
        try:
            cfg = self._cluster_config()
            storage = in_pod_storage.storage_client_for(cfg)
            bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
            storage.upload_file(str(path), bucket,
                                f"{prefix}_execution/{_INTERVENTIONS_FILENAME}")
        except Exception as e:  # noqa: BLE001 - never block the stop on the mirror
            logger.warning("Could not publish the kill ledger for %s: %s", campaign_id, e)

    def _job_artifact_dir(self, job_name: str) -> str:
        """The Job's campaign-relative artifact dir, read off the Job itself.

        The pod already carries it as ``OUTPUT_DIR=/out/_jobs/<batch>/job-<idx>`` (see
        ``kubernetes_backend.create_job_manifest``), so this reads back what the backend
        actually stamped rather than re-deriving the layout from the Job's *name* — a
        parse of ``<batch>-job-<idx>`` would be a second definition of that layout, free
        to drift from :func:`~robovast.common.execution.job_artifact_rel`.

        ``""`` when the Job carries no such variable, which keeps the kill recordable:
        the ledger's other resolution path (the job-link manifest) is what needs this, and
        a record without it is still a truthful record that a human stopped the job.
        """
        from kubernetes import client
        try:
            job = self._k8s_batch().read_namespaced_job(job_name, self.namespace)
        except client.exceptions.ApiException:
            return ""
        containers = (getattr(job.spec.template.spec, "containers", None) or []
                      if job.spec and job.spec.template and job.spec.template.spec else [])
        for container in containers:
            for var in getattr(container, "env", None) or []:
                if var.name == "OUTPUT_DIR" and var.value:
                    return var.value.removeprefix("/out/").lstrip("/")
        return ""

    def _teardown_campaign_jobs(self, campaign_id: str) -> None:
        """Delete one campaign's in-flight cluster workloads (label-scoped).

        Reuses ``cleanup_cluster_campaign`` — the same teardown ``vast cluster
        jobs-cleanup`` performs — so the running pods terminate now and the driver's
        batch wait loop unblocks. Label-scoped to this campaign's Jobs and pods, and
        nothing cluster-wide is paused for the duration, so a concurrent campaign keeps
        being admitted while this one is torn down.
        """
        from .cluster_execution import cleanup_cluster_campaign
        cleanup_cluster_campaign(namespace=self.namespace, campaign=campaign_id,
                                 context=self.kube_context)

    def _adopts_on_restart(self) -> bool:
        """True: this lane's campaigns outlive the process, and the next one adopts them.

        A cluster campaign's compute is its scenario Jobs. They are not children of this
        process, they upload their own results to the object store, and
        :mod:`~robovast.execution.cluster_execution.campaign_resume` re-attaches to them at
        startup -- so exiting is not a reason to destroy them, and a pod replacement
        (``vast service upgrade``, an eviction, a drain, an OOM) stops being a data-loss
        event.

        This is why there is no ``_terminate_running_campaigns`` override here any more.
        Stopping a campaign is :meth:`stop`, which tears its Jobs down through
        :meth:`_teardown_campaign_jobs` and records the stop; exiting the service is not,
        and never was a good way to say it -- the cooperative stop persists a terminal
        ``outcome.json``, and a campaign that has recorded an ending is one no successor
        will pick up again.

        Unconditional, and deliberately not a ``KUBERNETES_SERVICE_HOST`` test: an
        off-cluster service driving a cluster adopts on its next start exactly like an
        in-pod one, so keying on where the process runs would answer a different question.
        """
        return True

    # -- container exec -----------------------------------------------------

    def _exec_lane(self):
        """The in-cluster exec lane: one aux pod, driven through ``pods/exec``.

        Staging goes through the object store, exactly as an image build's context does
        (see ``_start_cluster_build``) — the same bucket resolution, the same
        service-side client, and the pod given the *cluster-internal* endpoint rather
        than whatever this process is using to reach the store.
        """
        from robovast.execution.cluster_execution import in_pod_storage

        from .cluster_image_build import build_context_bucket
        from .container_runner import service_pod_owner_reference
        from .kube_exec_lane import KubeExecLane
        owner = None
        try:
            owner = service_pod_owner_reference(self._k8s(), self.namespace)
        except Exception as e:  # noqa: BLE001 - off-cluster there is no service pod
            logger.debug("no service-pod owner reference for the exec pod: %s", e)
        cfg = self._cluster_config()
        # A diagnostic exec belongs to no campaign, so it has no campaign bucket — the
        # same position an image build is in, and the same answer.
        bucket = build_context_bucket(cfg)
        access_key, secret_key = cfg.get_s3_credentials()
        return KubeExecLane(self.namespace, owner_ref=owner,
                            kube_context=self.kube_context,
                            # The exec pod runs the experiment image, which on this lane is
                            # in our own registry and may be private. Without this the pull
                            # succeeds only on a node that already cached it.
                            pull_secret=self._registry_pull_secret(),
                            # Deferred: off-cluster, building this opens a port-forward,
                            # and the stray-reap builds a lane it never stages into.
                            storage_factory=lambda: in_pod_storage.storage_client_for(cfg),
                            bucket=bucket,
                            s3_endpoint=cfg.get_s3_endpoint(),
                            s3_access_key=access_key, s3_secret_key=secret_key)

    def _reap_stray_exec_container(self) -> None:
        """Delete every exec pod and staged tree left by a previous service process.

        A sweep rather than ``stop_held()`` on the one fixed name: a query pod's name
        carries a hash of the identity it was started for, and nothing persists those
        across a restart, so the label is the only handle left on it.
        """
        try:
            deleted = self._exec_lane().sweep_held()
            if deleted:
                logger.info("removed %d stray exec pod(s) from a previous run: %s",
                            len(deleted), ", ".join(deleted))
        except Exception as e:  # noqa: BLE001 - a missing cluster must not break startup
            logger.debug("could not check for stray exec pods: %s", e)

    # -- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Stop running campaigns, then tear down the shared MinIO port-forward."""
        # Stop the keep-alive first, and outside the lock: it must not observe the
        # teardown below as a stall and helpfully reopen the tunnel this is closing.
        self._pf_monitor_stop.set()
        monitor, self._pf_monitor = self._pf_monitor, None
        if monitor is not None:
            monitor.join(timeout=self._PF_PROBE_INTERVAL_S + 1)
        try:
            super().shutdown()
        finally:
            with self._pf_lock:
                self._close_minio_pf_locked()

    # -- orphan reaping -----------------------------------------------------

    def reap_orphans(self) -> int:
        """Delete campaign workloads left behind by a previous service instance.

        Aux pods only, and that is the whole scope: a restart leaves the campaigns'
        **scenario Jobs** running on purpose (see :meth:`_adopts_on_restart`), so this
        must never widen to them. Aux pods are owned by the service pod and thus
        garbage-collected by Kubernetes when it is replaced; this is the backstop for
        the cases GC misses (e.g. the ownerReference could not be resolved), and the
        successor to the old launcher-side ``reap_orphaned_runs``. Best-effort.
        """
        try:
            core = self._k8s()
            pods = core.list_namespaced_pod(
                self.namespace, label_selector=AUX_LABEL).items
        except Exception as e:  # noqa: BLE001 - never block startup
            logger.debug("Could not list aux pods to reap: %s", e)
            return 0
        reaped = 0
        for pod in pods:
            # Nothing in this fresh process is driving any campaign yet, so every
            # aux pod present at startup is by definition an orphan.
            try:
                core.delete_namespaced_pod(pod.metadata.name, self.namespace)
                reaped += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("Could not delete orphaned aux pod %s: %s",
                             pod.metadata.name, e)
        if reaped:
            logger.info("Reaped %d orphaned aux pod(s) from a previous service instance",
                        reaped)
        return reaped

    def _campaigns_a_restart_would_lose(self, active) -> dict:
        """``{campaign_id: why}`` for the live campaigns a replacement could not pick up.

        Asked of :mod:`.campaign_resume` -- the same decision the successor will make -- so
        the warning before a roll and the behaviour after it cannot drift apart. A campaign
        this cannot answer for is treated as one that would be lost: the whole point of the
        refusal is to be wrong in the safe direction.
        """
        from . import campaign_resume
        blocked = {}
        for summary in active:
            cid = summary.campaign_id
            try:
                _, _, refusal = campaign_resume.plan_for(
                    self, cid, Path(self._campaign_dir(cid)))
            except Exception as e:  # noqa: BLE001 - "cannot tell" is not "will survive"
                refusal = f"could not be checked ({e})"
            if refusal is not None:
                blocked[cid] = refusal
        return blocked

    def resume_interrupted_campaigns(self) -> dict:
        """Pick up the campaigns a previous service process was driving.

        Synchronously, and before this service answers anything. Not a background thread:
        ``_launch_campaign`` returns as soon as a campaign is named, so the blocking part is
        one index listing plus a restore per campaign — and registering the campaigns *here*
        is what stops a fresh launch arriving over the API and racing a campaign that is
        about to be adopted.

        Returns ``{campaign_id: None | refusal}``; see :mod:`.campaign_resume` for what is
        picked up and what is deliberately left alone. Never raises.
        """
        from . import campaign_resume
        outcomes = campaign_resume.resume_all(self)
        resumed = [cid for cid, refusal in outcomes.items() if refusal is None]
        if resumed:
            logger.info("Resumed %d campaign(s) interrupted by a service restart: %s",
                        len(resumed), ", ".join(resumed))
        return outcomes

    def cleanup_campaign_data(self, request) -> ActionResult:
        """Delete campaign result bucket(s) from the object store.

        Runs here (not on the client) because this process holds the cluster config
        (object-store credentials) and the authoritative live-campaign set. A bulk
        delete (``campaign_id`` None) always skips campaigns this service is still
        driving; a targeted delete honours ``force`` to remove a named one anyway.
        """
        from robovast.execution.cluster_execution import bucket_ops
        from robovast.service.interface import ListCampaignsRequest

        running: set = set()
        if request.campaign_id is None or not request.force:
            for c in self.list_campaigns(ListCampaignsRequest(limit=1000)).campaigns:
                if is_running(c.phase):
                    # Match both the raw id and its sanitized bucket name, since
                    # ``cleanup_campaigns`` compares against object-store names.
                    running.add(c.campaign_id)
                    running.add(bucket_ops.bucket_name(c.campaign_id))
        removed = bucket_ops.cleanup_campaigns(
            self._cluster_config(), namespace=self.namespace,
            context=self.kube_context, campaign_id=request.campaign_id,
            running_campaigns=running)
        # Retire the marker of everything actually removed, or those campaigns keep being
        # listed with nothing behind them. Driven by what the sweep *did*, not by what it
        # was asked to do: a campaign whose delete failed keeps its marker, because its
        # data is still there and a listing that omits stored data is the very defect the
        # index exists to fix. Matching is forward-only (id → bucket name); the reverse is
        # the lossy transform this index replaced.
        self._unmark_removed(removed)
        return ActionResult(
            ok=True, message=f"Removed {len(removed)} bucket(s) from the object store.")

    def _unmark_removed(self, removed) -> None:
        """Retire index markers for the storage names *removed* names.

        Skips the object store entirely when nothing was removed — the common no-op, and
        it keeps a cleanup that swept nothing from paying for a listing.
        """
        if not removed:
            return
        from robovast.execution.cluster_execution import bucket_ops
        gone = set(removed)
        for cid in self._campaign_index()[0]:
            if cid in gone or bucket_ops.bucket_name(cid) in gone:
                self._unmark_campaign(cid)

    def delete_campaign(self, campaign_id: str) -> ActionResult:
        """Delete one cluster campaign wholesale: object-store data, leftover Jobs,
        and the service's local caches (see :meth:`RobovastInterface.delete_campaign`).

        The object store is the durable home here, so it is the primary target; the
        Job reap catches anything a crashed/orphaned campaign left behind, and the
        cache wipe mirrors the local transport. The external share copy is untouched.
        """
        import shutil

        from botocore.exceptions import ClientError

        from robovast.execution.cluster_execution import bucket_ops

        from .cluster_execution import cleanup_cluster_campaign

        self._ensure_deletable(campaign_id)  # refuse while this service still drives it
        cfg = self._cluster_config()
        # 1. Durable home: object-store bucket / shared prefix. Tolerate an
        #    already-absent bucket so a repeated delete is idempotent.
        try:
            bucket_ops.delete_campaign(campaign_id, cfg, namespace=self.namespace,
                                       context=self.kube_context)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                raise
        # 2. Reap any leftover Jobs/pods (best-effort — the data is already gone).
        try:
            cleanup_cluster_campaign(namespace=self.namespace, campaign=campaign_id,
                                     context=self.kube_context)
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("Leftover-Job cleanup for %s failed", campaign_id,
                           exc_info=True)
        # 3. The index marker, or the campaign keeps being listed with no data behind it.
        self._unmark_campaign(campaign_id)
        # 4. The central index. Inherited rather than repeated: this lane deletes more
        # places than the local one, but the index is the same index, and a campaign whose
        # object-store copy is gone must not keep answering queries from rows describing it.
        self._forget_in_index(campaign_id)
        # 5. Service-local caches: the fetch scratch and any in-pod driver dir.
        shutil.rmtree(self._cache_dir(campaign_id), ignore_errors=True)
        shutil.rmtree(self._campaign_dir(campaign_id), ignore_errors=True)
        with self._lock:
            self._campaigns.pop(campaign_id, None)
        return ActionResult(
            ok=True,
            message=f"Deleted campaign {campaign_id!r} (object store, jobs, index, "
                    f"cache).")

    # -- data / results -----------------------------------------------------

    def _cache_dir(self, campaign_id: str) -> Path:
        """Local scratch mirroring a campaign's objects. Ephemeral by design — the object
        store is the durable home — and shared by the whole-campaign fetch and every
        named-object fetch, so the two can never hold divergent copies of one file."""
        return Path("/tmp") / "robovast-campaigns" / campaign_id  # noqa: S108 - pod scratch

    def _data_dir(self, campaign_id: str):
        """Refused on this lane: there is no cheap "the campaign's directory" here.

        Answering it with ``fetch_campaign`` — the whole object-store prefix, rosbags
        included — makes every *inherited* method touching it a whole-campaign download,
        silently and at the worst possible moment: ``list_campaign_plots`` pulls the entire
        campaign to read one small ``.vast``, once per campaign, every time the Results page
        loads.

        The failure mode is what makes this a refusal rather than a comment. Nothing errors,
        no test fails, the page merely takes minutes and the pod moves gigabytes. So a caller
        says what it needs and pays only that:

        * :meth:`_query_dir` — the campaign a SQL query names (it reads the index).
        * :meth:`_config_dir` — the frozen ``_config`` snapshot.
        * :meth:`_whole_campaign_dir` — everything, when that is genuinely the need.
        """
        raise NotImplementedError(
            f"_data_dir is not available on the cluster lane (campaign {campaign_id!r}): "
            "it would fetch the whole campaign from the object store. Ask for what you "
            "need instead — _query_dir (a query, which reads the index), _config_dir "
            "(the frozen config snapshot), or _whole_campaign_dir (everything, "
            "deliberately).")

    def _whole_campaign_dir(self, campaign_id: str):
        """Everything, deliberately: the campaign prefix into the local cache.

        Expensive by nature — the callers entitled to it cannot know which files they
        will read (notebook rendering against run outputs, the ``/results`` address
        space, endpoint plugins via ``resolve_data_dir``).
        """
        return self.fetch_campaign(campaign_id)

    def _config_dir(self, campaign_id: str):
        """Materialise only the frozen ``_config`` snapshot, then answer from the cache.

        A handful of small objects against ``fetch_campaign``'s whole prefix — the same
        discipline as :meth:`_scene_source_dir`, and the reason the declared-plots and
        panel-asset readers are cheap again.
        """
        storage, bucket, prefix = self._campaign_object_location(
            campaign_id, interactive=True)
        objects, _ = storage.list_entries(bucket, f"{prefix}_config")
        rels = [key[len(prefix):] for key, _size in objects]
        if rels:
            self._materialize(campaign_id, rels, "campaign config", interactive=True)
        return Path(self._cache_dir(campaign_id)) / "_config"

    @contextlib.contextmanager
    def _render_progress(self, campaign_id: str, workload: str):
        """Publish notebook-execution progress, continuing the bar the transfer started.

        The two halves of an Explorer click are a fetch and a render, and only reporting the
        first would replace a silent wait with a bar that fills, vanishes, and leaves the
        caller staring at nothing for the remaining minutes.
        """
        with self._reporting_progress(campaign_id) as publish:
            def on_cell(done, total):
                publish(phase="executing", unit="cells", done=done, total=total,
                        detail=workload)
            yield on_cell

    @contextlib.contextmanager
    def _reporting_progress(self, campaign_id: str):
        """Publish this campaign's live progress for the duration of the block.

        Yields a ``publish(**fields)`` that builds a :class:`WorkProgress`, and drops the
        entry on the way out however the block ends — so a failed transfer stops advertising
        itself as in flight rather than leaving a bar frozen at 37%% forever.

        One record per campaign, not per request: the expensive phase is already serialised
        by ``_fetch_locks``, so the only overlap this loses is two notebooks of the same
        campaign executing at once, where last-writer-wins costs a caller nothing but a
        slightly wrong cell number.
        """
        from robovast.service.interface import WorkProgress

        def publish(**fields) -> None:
            with self._work_progress_guard:
                self._work_progress[campaign_id] = WorkProgress(**fields)

        try:
            yield publish
        finally:
            with self._work_progress_guard:
                self._work_progress.pop(campaign_id, None)

    def _materialize(self, campaign_id: str, rel_paths, subject: str, *,
                     interactive: bool = False) -> Path:
        """Copy named objects of a campaign into its cache dir; return the dir.

        A **single-object** fetch per path — the same discipline as the ``/results`` file
        overrides below, and the reason both callers exist: pulling the campaign prefix to
        read a 2 KB ``outcome.json`` drags every rosbag the campaign produced, in the
        deployment where campaigns are largest. Writes into the
        *same* cache dir, so a later full ``fetch_campaign`` finds these files already at
        the right size and skips them.

        A cached copy is validated by **size, not existence**: ``outcome.json`` is
        rewritten in place by re-postprocessing, and an existence check would pin the first
        version this service ever saw and serve it forever.

        A missing object is skipped, not an error: whether "not published yet" is a problem
        is the caller's question, and each answers it differently (a campaign with no
        ``outcome.json`` reconstructs to ``unknown``).
        """
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        from robovast.common.progress import fmt_size
        dest = self._cache_dir(campaign_id)
        storage, bucket, prefix = self._campaign_object_location(
            campaign_id, interactive=interactive)
        with self._fetch_locks_guard:
            lock = self._fetch_locks.setdefault(campaign_id, threading.Lock())
        # The same lock ``fetch_campaign`` takes: concurrent first-load reads (the results
        # explorer fires one query per sub-view; the campaign list re-summarizes every
        # second) must not race to write the same file, nor race a whole-campaign fetch
        # writing it too.
        rel_paths = list(rel_paths)
        with lock, self._reporting_progress(campaign_id) as publish:
            fetched = total = done = 0
            started = time.perf_counter()
            try:
                for rel in rel_paths:
                    dst = dest / Path(rel)
                    size = storage.stat_object(bucket, f"{prefix}{rel}")
                    if size is None:
                        continue
                    total += size
                    if dst.exists() and dst.stat().st_size == size:
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    # Published before the transfer, not after: a single object can be
                    # hundreds of MB, so the interesting part of this wait is the file in
                    # flight.
                    # Nothing is published when every path is already cached — an instant
                    # call should not flash a progress bar.
                    publish(phase="downloading", unit="files", done=done,
                            total=len(rel_paths), bytes_done=fetched, detail=rel)
                    storage.download_object(bucket, f"{prefix}{rel}", str(dst))
                    fetched += size
                    done += 1
            # An unreachable store (dropped port-forward, connection reset) is translated
            # by ``_S3StorageClient._resilient`` into ObjectStoreUnreachableError — a
            # RuntimeError the service maps to 503, naming the endpoint and the object.
            except ClientError as exc:
                # No bucket: never published (still running / never finalized) or cleaned
                # up. A clean 404 rather than an ASGI 500.
                if exc.response.get("Error", {}).get("Code") == "NoSuchBucket":
                    raise KeyError(
                        f"No stored data for campaign {campaign_id!r}: its object "
                        f"store bucket does not exist (not yet published or removed)"
                    ) from exc
                raise
            elapsed = time.perf_counter() - started
        if fetched:
            self._last_fetch[campaign_id] = (fetched, elapsed)
            logger.info("Fetched %s for campaign %s (%s of %s) from %s/%s in %.1fs",
                        subject, campaign_id, fmt_size(fetched), fmt_size(total),
                        bucket, prefix, elapsed)
        return dest

    # -- on-demand 3D geometry ---------------------------------------------

    def _resolve_image_digest(self, ref: str):  # pylint: disable=useless-return
        """No tag→digest resolution on this lane. Refusing beats answering with the wrong bytes.

        Inherited, this would be ``docker inspect`` **on the service host**, which is either absent
        (in-pod) or -- worse, running off-cluster with ``-x`` -- present and answering with a bare
        local image id. No cluster node can pull such an id (``pullable_digest`` rejects it), so the
        aux pod would fail to start on an identity we had just declared trustworthy. A campaign here
        that recorded no per-role digest is therefore refused with the resolver's message.
        """
        del ref
        # Explicit, not incidental: None *is* the answer on this lane, and the
        # docstring above is about that. Falling off the end would read as an
        # unfinished function.
        return None

    def _scene_source_dir(self, campaign_id: str) -> str:
        """Materialise only what resolving geometry reads, then answer from that.

        One small object -- ``execution.yaml`` -- against ``fetch_campaign``'s whole prefix, which for a
        25-run campaign is rosbags. The run's capture manifest is fetched by ``_scene_capture`` below,
        because its path depends on the run.
        """
        self._materialize(campaign_id, ("_execution/execution.yaml",), "execution metadata",
                          interactive=True)
        return str(self._cache_dir(campaign_id))

    def _retrigger_source_dir(self, campaign_id: str) -> str:
        """Materialise what a retrigger reads, then answer from the cache like the base class.

        A named-object fetch, not ``fetch_campaign``: a retrigger reads the frozen config and
        three small records, where the campaign prefix is rosbags. Same discipline as
        ``_scene_source_dir`` above.

        ``_config/`` is listed rather than assumed, because its contents are whatever the
        campaign's ``run_files`` matched. A missing prefix raises ``KeyError`` from
        ``list_files``, which is exactly the "this campaign froze no config" refusal — so it is
        left to propagate.
        """
        address = file_address.format_address(
            file_address.RESULTS, campaign_id, "_config/")
        # limit=0 is "no window" (see file_view.paginate), so this is the whole listing in one
        # call -- a partial page would stage a partial config, which the run_files coverage
        # check would then report as a corrupt source rather than a truncated read.
        listing = self.list_files(address, recursive=True, limit=0)
        # Undetailed entries are strings relative to the address, directories keeping a
        # trailing "/" (see FileListing).
        rel_paths = [f"_config/{name}" for name in listing.entries
                     if not name.endswith("/")]
        # The records a retrigger replays from. Absent ones are skipped by _materialize, and
        # each reader decides what its absence means (a campaign that failed before its first
        # batch has no execution.yaml; one predating launch.yaml has no launch record).
        rel_paths += ["_execution/execution.yaml", "_execution/launch.yaml",
                      "_transient/configurations.yaml"]
        self._materialize(campaign_id, tuple(rel_paths), "campaign config",
                          interactive=True)
        return str(self._cache_dir(campaign_id))

    def _role_image_source_dir(self, campaign_id: str) -> str:
        """Materialise what reading a campaign's per-role images needs, then answer from the cache.

        The same two objects a retrigger reads — the frozen ``_config/`` and
        ``_execution/execution.yaml`` — so this reuses that fetch rather than issuing a second
        listing for the same prefix. Inheriting the base class's answer would read a directory
        that does not exist on this lane, which is precisely the failure mode the seam exists
        for.
        """
        return self._retrigger_source_dir(campaign_id)

    def _scene_capture(self, campaign_id: str, config_name: str, run_id: str) -> dict:
        """Fetch this run's capture manifest, then read it the way the base class does.

        Without this the cluster lane would look for a file on a disk that has none -- the gap that makes
        ``exec_in_container(campaign_id=…)`` fail for a campaign this service did not drive.
        """
        rel = f"{config_name}/{run_id}/capture/capture.json"
        self._materialize(campaign_id, (rel,), "run capture manifest", interactive=True)
        return super()._scene_capture(campaign_id, config_name, run_id)

    def _run_state_path(self, campaign_id: str, config_name: str, run_id: str,
                        filename: str):
        """Fetch this run's recording, then point at it where the base class expects.

        Same shape as :meth:`_scene_capture` above and for the same reason: the render runs a
        container over a *path*, and on this lane nothing is on local disk until asked for.
        A single object — the recording is the one input the frame is drawn from.
        """
        rel = f"{config_name}/{run_id}/{filename}"
        self._materialize(campaign_id, (rel,), "run state recording", interactive=True)
        return super()._run_state_path(campaign_id, config_name, run_id, filename)

    def _scene_identity(self, campaign_id, config_name, run_id):
        """Materialise a campaign-owned world's whole ``_config/`` before resolving identity.

        A world declared as a path in the ``.vast`` is archived under ``_config/``, and on
        this lane nothing is on local disk until it is asked for. Which world it is, is only
        known once the capture has been read, so this cannot join ``_scene_source_dir``'s
        fetch.

        The **whole** prefix, not the world object: the world names its meshes and colliders
        by the ``/config/...`` path the job mounted them at, and the rebuild reproduces that
        mount from this tree (see ``scene_cache._campaign_world``). It is also what the cache
        key is computed over, so a partial fetch would key geometry on a partial tree.
        Listed rather than assumed, for the reason ``_retrigger_source_dir`` gives.
        """
        from robovast.service import scene_cache  # pylint: disable=import-outside-toplevel

        manifest = self._scene_capture(campaign_id, config_name, run_id)
        rel = scene_cache.campaign_world_rel(
            str((manifest or {}).get("world") or ""), config_name)
        address = file_address.format_address(
            file_address.RESULTS, campaign_id, "_config/")
        # limit=0 is "no window" (see file_view.paginate): a partial page would stage a partial
        # tree, which hashes to a key that is neither this campaign's nor anyone else's.
        listing = self.list_files(address, recursive=True, limit=0)
        names = [name for name in listing.entries if not name.endswith("/")]
        # The `.vast` always, because identity needs it to know WHICH simulator ran and so who
        # to ask how to rebuild the geometry. The rest only for a campaign-owned world -- a
        # packaged one is in the image, and staging a campaign's meshes to compile it would be
        # transfer for nothing.
        wanted = names if rel else [n for n in names if n.endswith(".vast")]
        self._materialize(campaign_id, tuple(f"_config/{n}" for n in wanted),
                          "campaign config", interactive=True)
        # A world a variation GENERATED lives under the configuration, not the campaign, and
        # names its meshes by that prefix -- so its own tree has to come down too, or the
        # build compiles a world whose every reference is missing.
        if rel and rel.startswith(f"{config_name}/"):
            per_config = file_address.format_address(
                file_address.RESULTS, campaign_id, f"{config_name}/_config/")
            entries = self.list_files(per_config, recursive=True, limit=0).entries
            self._materialize(
                campaign_id,
                tuple(f"{config_name}/_config/{n}" for n in entries if not n.endswith("/")),
                "configuration config", interactive=True)
        return super()._scene_identity(campaign_id, config_name, run_id)

    def _scene_runner_context(self, campaign_id: str, identity: dict):
        """A context manager yielding an aux-pod runner factory on the campaign's own image.

        Deliberately not ``AuxPodSession``'s campaign-scoped use: this build is not part of a campaign's
        lifecycle. It is a cache fill that may happen long after the campaign finished, and its result
        serves *every* campaign that used that world -- so the pod's lifetime is the build's, and the
        context manager is what guarantees it is torn down rather than left to
        ``activeDeadlineSeconds``.
        """
        import hashlib

        from robovast.common.variation.container_runner import ContainerSpec

        from .container_runner import AuxPodSession

        del campaign_id
        image = identity["image"]
        # A pod name has to be label-safe and stable for this world, and an image digest is neither
        # short nor label-safe. `aux_pod_name` sanitises what it is given, so give it a digest of the
        # digest: same world -> same name, which also makes a duplicate create a 409 the session reuses.
        tag = f"scene-{hashlib.sha256(image.encode()).hexdigest()[:12]}"
        spec = ContainerSpec(image=image)
        pull_secret = self._registry_pull_secret()

        @contextlib.contextmanager
        def context():
            with AuxPodSession(tag, [spec], self.namespace, core_v1=self._k8s(),
                               pull_secret=pull_secret,
                               kube_context=self.kube_context,
                               **self._aux_store_kwargs()) as session:
                yield session.runner_factory()

        return context

    def _registry_pull_secret(self) -> str:
        """The registry pull secret, so a pod of ours can pull a *private* built image.

        Aux images were public when that path was written, so it never needed one, and a node that has
        already cached the campaign image hides the omission (``imagePullPolicy: IfNotPresent``) -- which
        means it first fails on a fresh node, the worst place to discover it.

        Three callers -- the scene aux pod, a campaign's aux pod, and the diagnostic exec pod, which
        runs the same private images -- hence the name is not about scenes. The store answers it, because which Secret pulls
        from this registry is the registry's business. It answers directly rather than through an
        import guarded by a bare ``except``, which turns a wrong module path into a silent no-op.
        """
        return self._images.pull_secret_name()

    def _query_dir(self, campaign_id: str):
        """Where the campaign *would* be — a path, not a transfer.

        A query reads the central index, so nothing about this campaign has to be on this
        pod's disk for one to answer. All the shared query surface still wants from a path
        is the campaign it names, so this returns the cache dir unfetched: the id is in its
        name and no object is touched. It stays an override because the inherited
        ``_query_dir`` goes through the refused :meth:`_data_dir`.
        """
        return Path(self._cache_dir(campaign_id))

    #: The campaign's **recorded facts**: its store row (start time, description, per-run
    #: tallies) and its durable terminal outcome. Both small and both enough to summarize a
    #: campaign without fetching any of its results.
    _RECORD_OBJECTS = ("campaign.db", "_execution/outcome.json")

    def _rest_dir(self, cid: str):
        """Where *cid*'s records already are on this pod's disk, without fetching any.

        The inherited version looks only under the results root, which is right for a campaign
        this process drives. Every *other* campaign here keeps its durable copy in the object
        store and its local copy in the cache dir, so without this override the summary cache
        would never find a key for them -- and they are the many, which is the whole point.

        Whether a fetch is needed is deliberately not asked: this must stay free, because it is
        called on the 1 Hz listing path precisely to avoid ``_record_dir``, whose ``_materialize``
        re-validates its objects against the store by size on every call.
        """
        local = super()._rest_dir(cid)
        if local is not None:
            return local
        cached = self._cache_dir(cid)
        return cached if (cached / "campaign.db").is_file() else None

    def _is_record_cache(self, campaign_dir) -> bool:
        """Whether *campaign_dir* is a fetched mirror of the object store (:meth:`_cache_dir`)
        rather than a campaign tree this pod drives.

        Asked of the directory, not of the campaign id, because that is what the readers
        below are handed -- and resolved through ``_cache_dir`` so the answer follows
        wherever that puts the scratch.
        """
        path = Path(campaign_dir)
        return path == Path(self._cache_dir(path.name))

    def _run_counts(self, campaign_dir: Path, *, live: bool) -> dict:
        """The store's own tallies for a fetched record cache: no backfill, no disk walk.

        For a campaign this pod does not drive, :meth:`_record_dir` hands back the
        two-object mirror of the object store, not the campaign's tree. The inherited
        backfill and its ``get_vast_configuration_info`` fallback both read per-run
        directories that are not there and never will be, so both can only answer zero --
        after opening ``campaign.db`` **read-write**.

        That open is what makes this more than wasted work. ``CampaignStore`` migrates on
        open, so a campaign recorded under an older schema gets rewritten in the cache; the
        copy then no longer matches the object it mirrors in size, and size is exactly how
        :meth:`_materialize` decides a cached object is stale. The next listing pass
        re-fetches it, the read migrates it again, and the pod re-downloads that campaign's
        records once per pass for as long as a browser tab is open, one log line each time.
        Only a campaign with **no runs recorded** reaches the backfill at all, so this shows
        as a few rows cycling in the log rather than as the whole listing.
        """
        if not self._is_record_cache(campaign_dir):
            return super()._run_counts(campaign_dir, live=live)
        from robovast.common.store import read_run_counts
        counts = read_run_counts(campaign_dir)
        # ``None`` is a store too old to have the ``run`` table. The walk is the inherited
        # answer for that, and here it is also the honest one -- it finds nothing, because
        # the runs are not on this disk -- while carrying the keys the caller expects.
        return counts if counts is not None else self._walk_counts(campaign_dir)

    def _record_dir(self, cid: str) -> Path:
        """Where *cid*'s recorded facts are, fetching the two small objects if needed.

        The object store is this lane's durable home, so a campaign this process is not
        driving may have no local copy at all — in-pod that is every campaign from a
        previous service life, and the inherited readers would answer ``unknown`` and zero
        for all of them. Materializing exactly two objects makes the whole inherited
        summary/status path correct without a single new reader.

        Three campaigns are left alone:

        * one this process is **driving** — its driver owns ``campaign.db`` and is writing
          it right now, and its dir is already the live truth;
        * one whose driver dir already holds ``campaign.db`` — off-cluster the driver runs
          here and writes locally, so there is nothing to fetch;
        * one the index does not list — there is nothing of it in the store to fetch, and
          the check is free (that listing is already cached for the id set). Without it a
          listing would attempt a fetch per row, so an *unreachable* store cost one
          connect timeout per campaign on the page instead of one for the page.
        """
        local = self._campaign_dir(cid)
        with self._lock:
            tracked = cid in self._campaigns
        if tracked or (local / "campaign.db").is_file():
            return local
        if cid not in self._campaign_index()[0]:
            return local
        try:
            # Interactive: two small objects, and ``list_campaigns`` reaches here per row
            # on a 1 Hz poll. The ``except`` below already degrades to "unknown".
            return self._materialize(cid, self._RECORD_OBJECTS, "campaign records",
                                     interactive=True)
        except (RuntimeError, KeyError) as e:
            # Unreachable store, or no bucket for this campaign. Absent records are not a
            # failed listing: the inherited readers report ``unknown`` / no start time,
            # which is the honest answer, and the next poll retries.
            logger.debug("could not materialize records for %s: %s", cid, e)
            return local

    def campaign_data_status(self, campaign_id: str):
        """Cluster: a query transfers nothing, because it reads the central index.

        Kept as an override even though the answer now matches the local lane's shape: the
        *reason* differs, and the note is what a client shows. The databases are not on this
        pod and never have to be — the campaign's rows are in the index, so there is no
        fetch to warn about and no probe of the object store to run.
        """
        from robovast.service.interface import CampaignDataStatus
        return CampaignDataStatus(
            campaign_id=campaign_id, source="object-store", fetch_required=False,
            cached=True, transfer="none",
            note="the campaign's results are in the central index; a query reads them "
                 "there and transfers nothing from the object store")

    # -- files: the /results namespace, served straight from the object store --------
    #
    # These override the inherited filesystem implementations for ``/results`` only —
    # ``/sources`` is a workspace on this service's own disk and stays inherited.
    #
    # The point of the override is what it does *not* do: ``_data_dir`` above fetches
    # the whole campaign prefix, so reading a 2 KB ``outcome.json`` through it would
    # drag every rosbag the campaign produced, in the deployment where campaigns are
    # largest. A single-object read is a single object.
    #
    # Rendering (binary refusal, line windows, listing assembly) is **not** reimplemented
    # here — it comes from ``file_view``, so the same file read through either lane
    # gives the same answer. It did not, once: an inlined ``splitlines()`` counted a
    # different number of lines than an iterated file.

    def _results_parts(self, address: str):
        """``(owner, rel)`` for a ``/results`` address, or ``None`` for another
        namespace — the one place this class decides an override applies."""
        namespace, owner, rel = file_address.parse_address(address)
        return (owner, rel) if namespace == file_address.RESULTS else None

    def _results_key(self, campaign_id: str, rel_path: str) -> tuple:
        """``(storage, bucket, key)`` for one object, with the escapes refused.

        ``safe_join`` cannot serve here — there is no filesystem to resolve against, so
        no symlink to follow and no ``resolve()`` to verify with. ``check_relative`` is
        the half of that check which is about the path's *shape*, and it is the half
        that applies to a key.
        """
        from robovast.client.safe_path import check_relative
        if rel_path:
            check_relative(rel_path)
        storage, bucket, prefix = self._campaign_object_location(campaign_id)
        return storage, bucket, f"{prefix}{rel_path}"

    def _campaign_object_location(self, campaign_id: str, *, interactive: bool = False):
        """``(storage, bucket, prefix)`` for a campaign's objects.

        *interactive* selects the fail-fast timeout budget, for the callers whose objects
        are a couple of KB on a polled request path rather than a campaign's worth of
        rosbags; see :func:`in_pod_storage.storage_client_for`.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        return (in_pod_storage.storage_client_for(cfg, interactive=interactive),
                bucket, prefix)

    def read_file_bytes(self, address: str) -> bytes:
        parts = self._results_parts(address)
        if parts is None:
            return super().read_file_bytes(address)
        owner, rel = parts
        if not rel:
            raise ValueError(f"{address!r} is a campaign, not a file — list it instead")
        storage, bucket, key = self._results_key(owner, rel)
        data = storage.read_object(bucket, key)
        if data is None:
            raise KeyError(f"no file at {address!r}")
        return data

    def local_file(self, address: str) -> Path:
        """The one object behind *address*, fetched into the campaign's cache dir.

        Overridden for the same reason as its three neighbours, and it is the override whose
        absence was *invisible*: this class inherits ``LocalTransport.local_file``, so the
        HTTP layer's "does this lane have a path?" test could never be False, and the
        inherited implementation resolved a ``/results`` address through ``_data_dir`` --
        which on this lane is :meth:`fetch_campaign`, i.e. it pulled the **whole campaign**,
        every rosbag included, to serve one file. A ``<video>`` tag on a 5 MB recording paid
        for gigabytes on first play, and nothing about the request said so.

        :meth:`_materialize` is the fix and already the discipline of the reads below: one
        object, validated by size, written into the same cache dir a later full fetch reuses.
        The caller gets a real path, so the response still streams with ``Range``.
        """
        parts = self._results_parts(address)
        if parts is None:
            return super().local_file(address)
        owner, rel = parts
        if not rel:
            raise ValueError(f"{address!r} is a campaign, not a file — list it instead")
        # interactive: this is a browser waiting on a media request, not a batch transfer.
        cache = self._materialize(owner, (rel,), f"file {rel}", interactive=True)
        target = cache / rel
        if not target.is_file():
            raise KeyError(f"no file at {address!r}")
        return target

    def read_file(self, address: str, lines: int = 200, offset: int = 0):
        parts = self._results_parts(address)
        if parts is None:
            return super().read_file(address, lines, offset)
        owner, rel = parts
        data = self.read_file_bytes(address)
        if file_view.is_binary_bytes(data):
            raise file_view.binary_refused(rel.rsplit("/", 1)[-1])
        return FileText(
            address=file_address.format_address(file_address.RESULTS, owner, rel),
            **file_view.text_page(data.decode("utf-8", errors="replace"), lines, offset))

    def list_files(self, address: str, recursive: bool = False, detail: bool = False,
                   offset: int = 0, limit: int = 100):
        parts = self._results_parts(address)
        if parts is None:
            return super().list_files(address, recursive, detail, offset, limit)
        owner, rel = parts
        storage, bucket, key = self._results_key(owner, rel)
        base = f"{key.rstrip('/')}/" if key.rstrip("/") else key
        objects, sub_prefixes = storage.list_entries(bucket, key,
                                                     delimited=not recursive)
        entries = [(k[len(base):], size) for k, size in objects]
        entries += [(p[len(base):], None) for p in sub_prefixes]
        if not entries:
            # An object store has no empty directories: nothing under the prefix means
            # the directory does not exist, which is a 404 rather than an empty listing.
            raise KeyError(f"no directory at {address!r}")
        entries.sort(key=lambda e: e[0])
        return file_view.build_listing(
            FileListing,
            file_address.format_address(file_address.RESULTS, owner,
                                        f"{rel.rstrip('/')}/" if rel else ""),
            entries, recursive=recursive, detail=detail, offset=offset, limit=limit,
            detail_fn=_object_entry)

    def _publish_config_edit(self, campaign_id: str) -> None:
        """Publish an in-place ``_config/<name>.vast`` edit to the object store.

        The object store is the durable home, and a re-run fetches from it with
        ``force=True`` — which would re-download the *old* config over the just-edited
        local copy. Uploading the edited ``.vast`` here makes the edit durable so the
        re-run reads it. (Overrides the local no-op.)
        """
        from robovast.common.results_utils import campaign_vast
        from robovast.execution.cluster_execution import in_pod_storage
        vast = campaign_vast(self._campaign_dir(campaign_id))
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        storage = in_pod_storage.storage_client_for(cfg)
        storage.upload_file(str(vast), bucket, f"{prefix}_config/{vast.name}")
        logger.info("Published edited config %s for %s to the object store",
                    vast.name, campaign_id)

    def _publish_campaign_records(self, campaign_id: str, campaign_root) -> None:
        """Publish the launch record now, not at ``finalize_campaign``.

        This lane's driver disk is scratch (see :ref:`campaign-discovery`), so a record
        written there and uploaded only when the campaign finishes is absent from every
        campaign that did *not* finish — which is the set someone comes looking at, and the
        set a restart has to re-launch from. It rides on :meth:`_publish_execution` rather
        than a put of its own: ``_execution/`` holds only this record at launch time, and
        one uploader for that directory is one fewer thing to keep in step.

        Best-effort, and quiet about it: the campaign's own uploads follow within seconds
        and will fail loudly if the store is genuinely unreachable.
        """
        try:
            self._publish_execution(campaign_id, campaign_root)
        except Exception as e:  # noqa: BLE001 - a record is not worth failing a campaign
            logger.warning("Could not publish the launch record for %s: %s",
                           campaign_id, e)

    def _publish_execution(self, campaign_id: str, campaign_root) -> None:
        """Upload a campaign's ``_execution/`` (outcome + logs) to the store."""
        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        storage = in_pod_storage.storage_client_for(cfg)
        storage.upload_dir(str(Path(campaign_root) / "_execution"),
                           bucket, f"{prefix}_execution")

    def run_postprocessing(self, request) -> ActionResult:
        """(Re)run analysis postprocessing for a cluster campaign, as a monitored
        background operation (returns immediately; watch it in the campaign view).

        The rosbag→CSV step runs as a Job in the campaign's own execution image and the
        host step — metrics, provenance, the index ingest — runs here (pure Python), the
        same two stages the campaign loop chains. ``postprocess_campaign`` streams into the
        scratch ``postprocessing.log``, which is published to the object store so the
        Monitor and a later restart see it.
        """
        from robovast.execution.notify import Notifier
        from robovast.execution.status_recovery import record_step_outcome

        from .postprocess_job import postprocess_campaign

        def work(state):
            # A real whole-campaign need, unlike a query: the host stage reads the run
            # tree it postprocesses -- the frozen ``_config``, ``campaign.db``, every
            # ``test.xml`` and the converted CSVs. Not ``force=True`` any more: that was
            # there for the one mutable object in the prefix, the per-campaign ``data.db``
            # this stage used to rewrite. It is gone (the rows go to the index), and the
            # conversion's own outputs are refreshed by ``sync_outputs``, which carries
            # ``force`` itself -- so forcing here only re-downloaded every rosbag.
            campaign_root = self.fetch_campaign(request.campaign_id)
            cfg = self._cluster_config()
            ok, message = postprocess_campaign(
                cfg, request.campaign_id, str(campaign_root), self.namespace,
                force=request.force, skip=list(request.skip or []),
                kube_context=self.kube_context, state=state)
            status = record_step_outcome(campaign_root, postprocessing=(ok, message))
            # Publish _execution (outcome + the conversion's postprocessing.log, even on
            # failure) so the result survives a restart and the Monitor can read it.
            self._publish_execution(request.campaign_id, campaign_root)
            state.update(postprocessed=status.postprocessed,
                         postprocessing_error=status.postprocessing_error)
            state.set_phase(Phase.FINISHED)
            # Same one-shot notifier as the local lane, and it matters more here: this is
            # the detached lane the push notifications exist for.
            notifier = Notifier.from_env(request.campaign_id)
            if ok:
                notifier.postprocessed()
            else:
                notifier.postprocessing_failed(message)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.POSTPROCESSING, work=work)

    #: Campaign-relative prefixes kept out of an exported archive: ``_postproc`` is
    #: postprocessing's internal staging and ``.cache`` its rebuildable hash cache.
    #: The first is what the download stream already drops; the second is
    #: ``campaign_archive.DEFAULT_EXCLUDE``, so an exported archive and one the
    #: campaign uploaded itself hold the same thing.
    _SHARE_EXCLUDE_PREFIXES = frozenset({"_postproc", ".cache"})

    #: What `record_step_outcome` needs to reconstruct the campaign's Status, and all it
    #: needs: the durable outcome, the run table, and the postprocessing marker. Pulling
    #: these three beats pulling the campaign (see `run_share`).
    _SHARE_STATUS_OBJECTS = ("_execution/outcome.json", "_execution/data.db", "campaign.db")

    def run_share(self, request) -> ActionResult:
        """(Re)trigger upload-to-share for a cluster campaign, as a monitored background
        operation. The outcome (clear/set ``share_error``) is recorded and published;
        adjusting the share env and re-triggering re-uploads to the new provider.

        **Nothing is staged on the way.** A ``fetch_campaign(force=True)`` first would
        materialise the entire campaign in this pod's scratch before a byte reached the
        share, which on a campaign of any size is a second full copy the pod has no room
        for and a long wait reported nowhere the campaign view looks. The
        objects are tarred straight out of the store into the request body instead, the
        same no-scratch path ``campaign_tar_stream`` already serves the download from.
        Only the three small objects that carry the campaign's *status* are pulled down,
        because the outcome has to be edited and published back.
        """
        from robovast.client.status import failure_detail
        from robovast.execution.status_recovery import record_step_outcome

        def work(state):
            from robovast.client.logging_config import (add_campaign_log_handler,
                                                        remove_campaign_log_handler)
            campaign_root = self._materialize(
                request.campaign_id, self._SHARE_STATUS_OBJECTS, "the campaign's outcome")
            # A SHARE phase file, same as the local lane, and written *before*
            # `_publish_execution` below so the account of the upload rides up to the object
            # store with the rest of `_execution` rather than staying in this service's scratch.
            handler = None
            try:
                (Path(campaign_root) / "_execution").mkdir(parents=True, exist_ok=True)
                handler = add_campaign_log_handler(
                    str(Path(campaign_root) / "_execution" / "share.log"))
            except Exception:  # pylint: disable=broad-except
                logger.warning("Could not open share.log for %s", request.campaign_id,
                               exc_info=True)
            try:
                logger.info("upload-to-share: %s", request.campaign_id)
                self._stream_campaign_to_share(request.campaign_id, campaign_root, state)
                ok, message = True, "upload-to-share complete"
                logger.info("✓ %s", message)
            except Exception as e:  # noqa: BLE001 - surfaced via status + share_error
                ok, message = False, failure_detail(e)
                logger.error("✗ upload-to-share failed: %s", message)
            finally:
                remove_campaign_log_handler(handler)
            status = record_step_outcome(campaign_root, share=(ok, message))
            self._publish_execution(request.campaign_id, campaign_root)
            state.update(share_error=status.share_error)
            state.set_phase(Phase.FINISHED)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.SHARING, work=work)

    def _stream_campaign_to_share(self, campaign_id: str, campaign_root, state) -> None:
        """Tar the campaign's stored objects straight into the share. No scratch.

        *campaign_root* supplies only the variant (``_execution/data.db`` present or
        not), so the name an export writes and the name the campaign-end upload writes
        are decided by the same rule.
        """
        from robovast.common.errors import CampaignConfigError
        from robovast.execution import campaign_archive
        from robovast.execution.controller import make_upload_progress_cb
        from robovast.execution.share_providers.naming import archive_name, campaign_variant

        from . import in_pod_upload

        # Before anything is created on the share. `_materialize` skips an object that is
        # not there, and `add_campaign_members` tars an empty prefix without complaint, so
        # without this an unknown id uploads a valid, empty archive under that name and
        # reports success -- the worst possible answer. The predicate is the one
        # `list_campaigns` answers with, so this and the listing cannot disagree.
        if not self._campaign_is_here(campaign_id):
            raise KeyError(f"no campaign {campaign_id!r} on this service")

        provider = in_pod_upload.load_provider_from_env()
        if provider is None:
            raise CampaignConfigError(
                "Cannot export to the share: no share provider is configured "
                "(ROBOVAST_SHARE_TYPE is unset in this service's environment).\n"
                "Set it and its credentials in the environment / .env that 'vast serve' "
                "runs with, then re-run the export.")
        # Before the share is touched at all, and after the cheap env read: a campaign that
        # can never be imported is not worth a round trip to a provider to find out.
        self._refuse_unimportable(campaign_id)
        in_pod_upload.verify_share_access(provider)

        variant = campaign_variant(campaign_root)
        object_name = archive_name(campaign_id, variant)
        cfg = self._cluster_config()
        excludes = set(self._SHARE_EXCLUDE_PREFIXES)

        progress = make_upload_progress_cb(state)
        on_member = getattr(progress, "on_member", None)
        if on_member is not None:
            # A listing pass, not a transfer: the sizes come back with the keys. It is
            # the only denominator available, because the archive is gzipped on the fly
            # and its compressed length is unknown until the last byte.
            progress.set_source_total(
                cfg.campaign_object_bytes(campaign_id, exclude_prefixes=excludes))

        logger.info("Streaming %s campaign %s from the object store to the %s share as %s...",
                    variant, campaign_id, provider.SHARE_TYPE, object_name)
        with campaign_archive.tar_stream(
                lambda tar: cfg.add_campaign_members(
                    tar, campaign_id, exclude_prefixes=excludes,
                    on_member=on_member)) as stream:
            provider.upload_archive_stream(stream, object_name, progress_callback=progress)
        if on_member is not None:
            progress.finish()
        logger.info("Uploaded %s to the %s share.", object_name, provider.SHARE_TYPE)

    def _refuse_unimportable(self, campaign_id: str) -> None:
        """Refuse to upload an archive no deployment could ever take back in.

        The guard above answers "is this id a campaign here"; this answers "is what is
        stored under it importable". They are different questions, and only the second
        catches a campaign that died before its ``_config/`` was published: it is indexed,
        it lists, it has an ``_execution/`` full of logs, and every byte of it tars and
        uploads happily into an archive whose only possible future is an ingest refusal on
        somebody else's service.

        Only the ``_config/`` prefix is listed. Nothing else decides the answer, and a
        campaign's full key listing is the one thing an export of a large campaign should
        not pay for twice.
        """
        from robovast.common.errors import CampaignConfigError
        from robovast.service.ingest import missing_for_import
        storage, bucket, prefix = self._campaign_object_location(campaign_id)
        objects, _sub_prefixes = storage.list_entries(bucket, f"{prefix}_config")
        missing = missing_for_import(key[len(prefix):] for key, _size in objects)
        if missing:
            raise CampaignConfigError(
                f"Cannot export {campaign_id}: it has no " + " ".join(missing) +
                "\nAn archive written from it could not be imported by any deployment, "
                "including this one, so it is refused here rather than at the far end of "
                "a transfer.")

    # -- taking a campaign in ------------------------------------------------
    #
    # The import sequence itself is the inherited one; only the four questions it asks
    # about durability differ here, because a pod's scratch is not where a campaign lives.

    def _campaign_is_here(self, campaign_id: str) -> bool:
        """The object store's index is the answer, not this pod's disk.

        Asking the filesystem would report "no" for every campaign this pod has not
        happened to fetch, so an import would sail past the collision check and then
        overwrite a campaign in the durable home that nobody was warned about.
        """
        return campaign_id in self._durable_campaign_ids()

    def _release_durable_campaign(self, campaign_id: str) -> None:
        """Under ``force``: delete the object-store copy being replaced, and its marker.

        Deliberately not :meth:`delete_campaign`, whose ``_ensure_deletable`` refuses a
        campaign this service is driving — by this point the import is registered under
        that very id, so the campaign would be refused on account of the operation asking.
        The guard that matters has already run: ``_dispatch_background`` would not have
        started a second operation on a busy campaign.
        """
        from botocore.exceptions import ClientError

        from robovast.execution.cluster_execution import bucket_ops
        try:
            bucket_ops.delete_campaign(campaign_id, self._cluster_config(),
                                       namespace=self.namespace, context=self.kube_context)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                raise
        self._unmark_campaign(campaign_id)

    def _publish_imported_campaign(self, campaign_id: str, target) -> None:
        """Upload the imported tree to the object store and index it; drop the scratch.

        Without this an import would land on a pod's ephemeral disk and be gone with the
        pod — and invisible even before that, since ``_durable_campaign_ids`` answers from
        the index rather than from what happens to be on disk.

        ``upload_dir`` walks without following symlinks, so the ``<config>/<run>/job``
        links are not uploaded — correct, because the object store has no symlinks and the
        download side rebuilds them from ``_transient/job_links.yaml``, which is a real
        file and does travel.
        """
        import shutil  # pylint: disable=import-outside-toplevel
        from datetime import datetime, timezone  # pylint: disable=import-outside-toplevel

        from robovast.common.store import \
            read_campaign_created_at  # pylint: disable=import-outside-toplevel
        from robovast.execution.cluster_execution import \
            in_pod_storage  # pylint: disable=import-outside-toplevel

        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        storage = in_pod_storage.storage_client_for(cfg)
        count = storage.upload_dir(str(target), bucket, prefix.rstrip("/"))
        logger.info("Published %d objects of imported campaign %s to the object store",
                    count, campaign_id)
        # The marker is what makes it listable at all; its created_at rides in the key so
        # a cold listing can order campaigns without an object read each.
        # The campaign's own recorded start time, so an imported campaign sorts where it
        # belongs in the listing rather than jumping to the top as if it had just run.
        created_at = (read_campaign_created_at(target)
                      or datetime.now(timezone.utc).isoformat())
        in_pod_storage.mark_campaign_indexed(storage, cfg, campaign_id, created_at)
        with self._index_lock:
            self._index_cache = None
        # The pod's copy has served its purpose; the durable home is the store, and a
        # multi-gigabyte campaign left on scratch is how a service pod fills its disk.
        shutil.rmtree(target, ignore_errors=True)

    def _publish_failed_import(self, campaign_id: str, target) -> None:
        """Publish a failed import's ``_execution/`` — the account, not the campaign.

        A failed import never reaches :meth:`_publish_imported_campaign`, so on this lane
        its ``import.log`` and ``import.json`` would stay on the pod's scratch while
        ``list_files``/``read_file`` read the object store: the campaign card showing the
        refusal, and ``/results/<id>`` answering *no directory* for the one campaign whose
        files anybody wanted to open -- the reason written down, and unreachable.

        Only ``_execution/`` goes up. The rest of the tree is whatever the archive held,
        which by definition this deployment could not make sense of, and a failed import of
        a large campaign must not spend a full upload explaining itself. The scratch copy is
        dropped either way — the pod's disk is not a place to leave an unusable tree.

        The campaign is indexed, so it survives a restart and ``vast campaign delete`` finds
        it. That also makes :meth:`_campaign_is_here` true for the id, so a retry needs
        ``force`` — which is what the failure already tells the reader to use, and
        :meth:`_release_durable_campaign` already clears.
        """
        import shutil  # pylint: disable=import-outside-toplevel
        from datetime import datetime, timezone  # pylint: disable=import-outside-toplevel

        from robovast.common.store import \
            read_campaign_created_at  # pylint: disable=import-outside-toplevel
        from robovast.execution.cluster_execution import \
            in_pod_storage  # pylint: disable=import-outside-toplevel

        execution = Path(target) / "_execution"
        try:
            cfg = self._cluster_config()
            bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
            storage = in_pod_storage.storage_client_for(cfg)
            if execution.is_dir():
                count = storage.upload_dir(str(execution),
                                           bucket, f"{prefix.rstrip('/')}/_execution")
                logger.info("Published %d objects accounting for the failed import of %s",
                            count, campaign_id)
            # The archive's own start time when it brought a readable store, so a failed
            # import sorts where the campaign belongs rather than at the top of the list;
            # now() when it did not, which is most of them -- that is often what failed.
            created_at = (read_campaign_created_at(target)
                          or datetime.now(timezone.utc).isoformat())
            in_pod_storage.mark_campaign_indexed(storage, cfg, campaign_id, created_at)
            with self._index_lock:
                self._index_cache = None
        except Exception:  # pylint: disable=broad-except
            # Best-effort, like `_record_failed_import`: the import already failed, and
            # failing to publish the account of it must not replace that reason with a
            # second, less useful one. The service log still holds the whole sequence.
            logger.warning("Could not publish the failed import of %s to the object store",
                           campaign_id, exc_info=True)
        shutil.rmtree(target, ignore_errors=True)

    def campaign_tar_stream(self, campaign_id: str):
        """Yield a ``tar.gz`` of the postprocessed campaign, streamed from the object store.

        Backs ``GET /campaigns/{id}/archive``. Objects are fetched and tarred on the
        fly (:func:`campaign_archive.iter_tar`), so **no scratch is used on the service
        during or after the download** — decisive for ~1TB campaigns. ``_postproc/``
        internal staging is excluded so the archive is the clean campaign layout.
        """
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel

        # Eagerly, before a generator is handed to the response: once streaming has begun
        # the status line is already 200, so a campaign that does not exist arrives as a
        # truncated body -- "Response ended prematurely" on the client, which names neither
        # the campaign nor the problem. The predicate is the one `list_campaigns` answers
        # with, so the archive route and the listing cannot disagree about what is here.
        if not self._campaign_is_here(campaign_id):
            raise KeyError(f"no campaign {campaign_id!r} on this service")

        cfg = self._cluster_config()
        return campaign_archive.iter_tar(
            lambda tar: cfg.add_campaign_members(
                tar, campaign_id, exclude_prefixes={"_postproc"}))

    def fetch_campaign(self, campaign_id: str, force: bool = False, dest=None, include=None):
        """Pull a campaign from the object store to a local dir; return it.

        The object store is the durable home (the campaign loop published the full
        campaign there via ``finalize_campaign``). The stateless service pulls it
        into ephemeral scratch on demand — to serve a download or re-postprocess.

        Objects are immutable, so files already present locally with a matching
        size are left untouched (see ``download_prefix``): repeat pulls — e.g. a
        notebook re-render — become near-noops. Pass ``force=True`` to overwrite
        the local cache unconditionally.

        ``dest`` overrides where it lands, and there is exactly one caller that needs to:
        :mod:`.campaign_resume`, restoring a campaign that is **not finished** into the
        driver's own campaign root, so its controller re-enters a directory holding what
        the earlier life produced. Everything else wants the shared scratch cache and the
        deduplication that comes with it, so the default stands.

        ``include`` narrows the fetch to the keys it selects (see
        ``StorageClient.download_prefix``); the same caller uses it to take a campaign's
        control plane without its run artifacts. A narrowed fetch leaves an **incomplete**
        directory behind, so whoever asks for one owns getting the rest before anything reads
        it -- ``ExecutionBackend.ensure_campaign_root_complete`` is that call.
        """
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        from robovast.common.progress import fmt_size
        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        dest = Path(dest) if dest is not None else self._cache_dir(campaign_id)
        dest.mkdir(parents=True, exist_ok=True)
        storage = in_pod_storage.storage_client_for(cfg)
        with self._fetch_locks_guard:
            lock = self._fetch_locks.setdefault(campaign_id, threading.Lock())
        # Serialize fetches of the same campaign: the first request populates the
        # cache while the rest wait, then find it complete and skip re-downloading
        # (immutable objects, matching size). Different campaigns still fetch in
        # parallel.
        started = time.perf_counter()
        fetched_bytes = 0
        with lock, self._reporting_progress(campaign_id) as publish:
            # ``listing`` is the pre-pass ``download_prefix`` runs to learn the denominator.
            # Named rather than left blank: on a campaign with 100k objects it is itself a
            # visible wait, and "listing" beats a bar that sits at 0/0.
            publish(phase="listing", unit="files", detail=campaign_id)

            def on_change(done, total, done_bytes, total_bytes):
                nonlocal fetched_bytes
                fetched_bytes = done_bytes
                publish(phase="downloading", unit="files", done=done, total=total,
                        bytes_done=done_bytes, bytes_total=total_bytes,
                        detail=campaign_id)

            try:
                # A whole campaign is GBs over a port-forward; without a running count the
                # transfer is indistinguishable from a hang for as long as it takes. The log
                # line serves whoever reads the pod log; ``on_progress`` serves the UI, which
                # additionally needs the denominator to draw a bar.
                n = storage.download_prefix(
                    bucket, prefix, str(dest), force=force, include=include,
                    on_file=in_pod_storage.download_progress_logger(
                        f"Campaign {campaign_id}"),
                    on_progress=in_pod_storage.download_progress_reporter(on_change))
            # An unreachable store is translated by ``_resilient`` (see _materialize).
            except ClientError as exc:
                # No bucket for this campaign in the object store: it was never
                # published (e.g. still running / never finalized) or has been
                # cleaned up. Surface a clean 404 instead of an ASGI 500.
                if exc.response.get("Error", {}).get("Code") == "NoSuchBucket":
                    raise KeyError(
                        f"No stored data for campaign {campaign_id!r}: its object "
                        f"store bucket does not exist (not yet published or removed)"
                    ) from exc
                raise
        # Elapsed as well as the count: "1832 files" alone does not distinguish a transfer
        # that took two seconds from one that took four minutes, which is the only question
        # a caller staring at a slow first call actually has. The byte figure comes from the
        # progress reporter's running sum — free now that it is tracked, where summing the
        # cache dir would stat every file of a campaign that can hold 100k of them.
        elapsed = time.perf_counter() - started
        logger.info("Fetched campaign %s (%d file(s), %s) from %s/%s to %s in %.1fs",
                    campaign_id, n, fmt_size(fetched_bytes), bucket, prefix, dest, elapsed)
        if fetched_bytes:
            # Same slot ``_materialize`` writes: both describe "what this service's last
            # transfer of this campaign cost", and a caller asking why the first click was
            # slow does not care which of the two paid for it.
            self._last_fetch[campaign_id] = (fetched_bytes, elapsed)
        return dest
