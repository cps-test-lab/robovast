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

"""``ClusterService`` — the in-cluster service core (mode 3).

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
import json
import time
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

from robovast.execution.control_server import Phase, is_running
from robovast.common import file_address, file_view
from robovast.service.client import LocalTransport
from robovast.service.interface import (ActionResult, FileListing, FileText,
                                        JobCounts, JobSummary,
                                        ListJobsResponse, LogChunk,
                                        ResourceUsage, VersionInfo)

logger = logging.getLogger(__name__)

CONTROLLER_LABEL = "app=robovast-controller"
AUX_LABEL = "app=robovast-aux"
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"


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

    def __init__(self, namespace=None, cluster_config_name=None,
                 cluster_config_kwargs=None, store=None,
                 reap_on_start=True, kube_context=None):
        super().__init__(store=store)
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
        # Per-campaign locks so concurrent data queries don't each re-download the
        # same campaign into the shared cache dir (the results explorer fires one
        # query per sub-view on first load). Guarded by ``_fetch_locks_guard``.
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._fetch_locks_guard = threading.Lock()
        # What this service's last transfer of each campaign's query databases cost, as
        # ``(bytes, seconds)`` — so ``campaign_data_status`` reports a measured number
        # rather than a guess, and a caller that waited can be told why. Process-local: a
        # restart forgets it, and the cache it describes is scratch anyway.
        self._last_fetch: dict[str, tuple[int, float]] = {}
        # Incremental, cache-backed job-log tails so a polling log panel fetches only
        # the delta each 1.5s instead of re-reading the whole pod log (see get_job_log
        # / PodLogTail). LRU-bounded so long-lived services don't accumulate buffers.
        self._job_log_tails: "OrderedDict[tuple, object]" = OrderedDict()
        self._job_log_guard = threading.Lock()
        # (monotonic, {campaign_id: created_at}) from the object store's campaign index,
        # or None when it must be re-read. TTL-cached because the campaign-list SSE stream
        # re-lists once a second; see _campaign_index.
        self._index_cache: "tuple[float, dict] | None" = None
        self._index_lock = threading.Lock()
        # True while one caller is out doing the listing. Guards the single-flight in
        # _campaign_index: the listing itself must not hold _index_lock (it is network
        # I/O), so this is what stops a poll from launching a second one behind the first.
        self._index_refreshing = False
        if reap_on_start:
            self.reap_orphans()

    # -- version ------------------------------------------------------------

    def version(self) -> VersionInfo:
        v = super().version()
        v.backend = "kubernetes"
        v.backends = ["cluster"]
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
        return v

    def _api_server_url(self) -> "str | None":
        """The API server this lane targets, read from config only — never dialled.

        ``version()`` is the call a client makes to find out *where* it is pointed,
        including when the cluster is unreachable; a probe here would make it hang
        exactly when the answer matters most. ``None`` when the config cannot be
        read at all, which is itself the answer to "which cluster?".
        """
        try:
            from kubernetes import client as k8s_client

            from robovast.common.kube import load_kube_config
            load_kube_config(context=self.kube_context)
            return k8s_client.Configuration.get_default_copy().host
        except Exception:  # noqa: BLE001 - informational field, never fatal
            return None

    def _compute_resource_usage(self) -> ResourceUsage:
        """Cluster CPU/memory capacity + current usage from the Kubernetes API.

        Capacity is the sum of every node's ``allocatable`` (the same measure Kueue
        quota is derived from); usage is the sum of resource *requests* of the pods
        **bound to those same nodes** — what the scheduler has actually committed,
        the number ``kubectl describe node`` calls "Allocated resources". Both are
        read behind :meth:`LocalTransport.resource_usage`'s TTL cache, and the pod
        list is filtered server-side to skip finished pods — so a poll costs at most
        one ``list_node`` + one filtered ``list_pod`` per cache window.

        Summing over one node set keeps ``used <= capacity``, which a cluster-wide
        pod sum does not: a pod still waiting for a node (or left behind by one that
        was removed) requests resources nothing has granted, so a queue of pending
        scenario runs used to report more cores in use than the cluster has —
        "29.7/24" on a 24-core workstation. Pending work is visible as
        ``jobs_pending`` instead, counted from Jobs by :meth:`_scenario_job_tally`.

        Requires the service's ClusterRole (nodes/pods get,list — see
        ``service_deploy._service_rbac_manifests``).
        """
        from robovast.execution.cluster_execution.kubernetes_kueue import \
            _parse_resource  # pylint: disable=import-outside-toplevel
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
        pods = v1.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed")
        for pod in pods.items:
            if getattr(pod.spec, "node_name", None) in node_names:
                # Native sidecars (init containers with restartPolicy Always) run for the
                # pod's whole life, and Kubernetes adds their requests to the pod's
                # effective total rather than taking the max as it does for ordinary init
                # containers. Counting only spec.containers would therefore under-report a
                # scenario job by its simulator and its SUT -- the two biggest reservations
                # in a three-container campaign -- and this number is what sizes a sweep.
                sidecars = [c for c in (getattr(pod.spec, "init_containers", None) or [])
                            if getattr(c, "restart_policy", None) == "Always"]
                for container in list(pod.spec.containers or []) + sidecars:
                    requests = (container.resources.requests
                                if container.resources else None) or {}
                    cpu_used += _parse_resource(requests.get("cpu"))
                    mem_used += int(_parse_resource(requests.get("memory")))

        jobs_running, jobs_pending = self._scenario_job_tally()
        return ResourceUsage(
            backend="kubernetes",
            cpu_capacity=cpu_capacity,
            cpu_used=cpu_used,
            memory_capacity_bytes=mem_capacity,
            memory_used_bytes=mem_used,
            parallel_runs=True,   # runs execute in parallel, bounded only by capacity
            jobs_running=jobs_running,
            jobs_pending=jobs_pending,
        )

    def _scenario_job_tally(self) -> "tuple[int, int]":
        """``(running, pending)`` over every scenario-run Job in this namespace.

        Counted from **Jobs**, not pods, because a Kueue-suspended Job has no pod at
        all (see :func:`list_jobs_with_phase`) — and that is the state every cluster
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
        from robovast.execution.cluster_execution.cluster_execution import \
            list_jobs_with_phase  # pylint: disable=import-outside-toplevel
        phases = [phase for _job, phase, _detail in list_jobs_with_phase(
            self._k8s_batch(), self._k8s(), self.namespace, "jobgroup=scenario-runs")]
        return (sum(1 for p in phases if p == "running"),
                sum(1 for p in phases if p in ("pending", "waiting", "blocked")))

    # -- helpers ------------------------------------------------------------

    def _cluster_config(self):
        from robovast.execution.cluster_execution.cluster_setup import \
            get_cluster_config
        if not self._config_name:
            raise ValueError(
                "cluster config not configured (ROBOVAST_CLUSTER_CONFIG_NAME); "
                "the service must be deployed by 'vast exec cluster setup'")
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
        from robovast.execution.cluster_execution.bucket_ops import \
            open_minio_port_forward
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
        itself, so clients no longer need to force anything — they wait for
        ``_pf_generation`` to move and re-resolve.
        """
        from robovast.common.shutdown import is_shutting_down
        from robovast.execution.cluster_execution.bucket_ops import forward_is_serving
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
        from robovast.common.kube import load_kube_config
        load_kube_config(context=self.kube_context)

    def _k8s(self):
        from kubernetes import client
        self._load_kube()
        return client.CoreV1Api()

    def _k8s_batch(self):
        from kubernetes import client
        self._load_kube()
        return client.BatchV1Api()

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
        from robovast.execution.cluster_execution.kubernetes_backend import \
            KubernetesBackend
        return KubernetesBackend(cluster_config=self._cluster_config(),
                                 namespace=self.namespace,
                                 kube_context=self.kube_context,
                                 state=state)

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
    def _campaign_context(self, campaign_id, project):
        """Per-campaign aux pod + the container-runner factory for this worker.

        Entered inside the worker thread, so the factory (a ContextVar) is scoped to
        exactly the composition that reads it — concurrent campaigns never clobber
        each other's aux target. A campaign whose variations need no helper image
        creates no pod and installs no factory (the local ``docker run`` fallback is
        never reached in-cluster because nothing asks for a runner).
        """
        from robovast.common.config_generation import set_container_runner_factory
        from robovast.execution.cluster_execution.container_runner import (
            AuxPodSession, required_container_specs)

        specs = required_container_specs(project.config_path)
        with AuxPodSession(campaign_id, specs, self.namespace,
                           core_v1=self._k8s() if specs else None,
                           kube_context=self.kube_context,
                           **(self._aux_store_kwargs() if specs else {})) as session:
            if specs:
                set_container_runner_factory(session.runner_factory())
            yield

    def _aux_store_kwargs(self) -> dict:
        """Storage wiring for an aux pod's workspace mirror.

        The same bucket an image-build context stages to — an aux workspace belongs to no
        campaign's results either, and is scratch that is deleted when the runner closes.
        The pod is given the *cluster-internal* endpoint, while this process keeps its own
        client (which off-cluster reaches the store through a port-forward).
        """
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.execution.cluster_execution.cluster_image_build import \
            build_context_bucket
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

    # ``_status_from_disk`` is inherited. It used to be overridden here to read the durable
    # ``_execution/outcome.json`` out of the object store first, because the local dir it
    # reconstructs from does not exist in-pod. ``_record_dir`` now puts that object where
    # every reader already looks, so the inherited implementation — which prefers
    # ``outcome.json`` and merges ``postprocessed`` from a present ``data.db`` — is the one
    # precedence for both lanes. That override's own promise, that a per-campaign status can
    # never disagree with the list view, only became true with it gone: ``_summary_for``
    # reconstructs directly and never called it.

    def _durable_campaign_ids(self) -> set[str]:
        """Campaign ids from the object store's index (see ``in_pod_storage``).

        This is what makes a finished cluster campaign listable at all: its home is the
        object store, and the inherited disk scan sees only what this pod happens to still
        have in scratch.
        """
        return set(self._campaign_index())

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
        indexed = self._campaign_index().get(cid)
        if indexed:
            self._started_at_cache[cid] = indexed
            return indexed
        return super()._started_at_for(cid)

    #: How long a campaign-index listing is reused. The campaign-list SSE stream re-lists
    #: every second (app.py ``_SSE_LIST_POLL_S``), so without this every one of those polls
    #: would be an object-store round-trip.
    _INDEX_CACHE_TTL = 10.0

    def _campaign_index(self) -> dict:
        """``{campaign_id: created_at}`` from the object store, cached for
        :data:`_INDEX_CACHE_TTL`.

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
                return cached[1]
            if self._index_refreshing:
                return {} if cached is None else cached[1]
            self._index_refreshing = True
        try:
            cfg = self._cluster_config()
            # Interactive: this listing sits under the campaign-list SSE's 1 Hz poll, and
            # the ``except`` below already has a good degraded answer (the stale cache).
            # With the bulk budget a stalled tunnel made each poll block for minutes.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
            index = dict(in_pod_storage.list_indexed_campaigns(storage, cfg))
        except Exception as e:  # noqa: BLE001 - never fail a listing over discovery
            logger.warning("Could not read the campaign index: %s", e)
            with self._index_lock:
                self._index_refreshing = False
                return {} if self._index_cache is None else self._index_cache[1]
        with self._index_lock:
            self._index_refreshing = False
            # A marker ``_on_campaign_started`` added while this listing was in flight is
            # simply overwritten, and that is fine: ``list_campaigns`` unions the live
            # registry into its id set (``LocalTransport._extra_live_ids``), so a campaign
            # this process just started stays listed without the index, and the next
            # refresh picks the marker up from the store.
            self._index_cache = (now, index)
        return index

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
                self._index_cache = (cached[0], {**cached[1], campaign_id: created_at})

    def _unmark_campaign(self, campaign_id: str) -> None:
        """Drop a deleted campaign's index marker, so it stops being listed."""
        from robovast.execution.cluster_execution import in_pod_storage
        try:
            cfg = self._cluster_config()
            # Interactive: one tiny marker delete on a request path, already best-effort.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
            in_pod_storage.unmark_campaign_indexed(storage, cfg, campaign_id)
        except Exception as e:  # noqa: BLE001 - the data itself is already gone
            logger.warning("Could not remove campaign %s from the index: %s",
                           campaign_id, e)
        with self._index_lock:
            self._index_cache = None

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        """Serve the unified infrastructure log — live pod scratch, then object store.

        Assembles the per-phase files (variation → run → postprocessing) into one
        divider-separated stream (see
        :func:`robovast.common.campaign_logs.assemble_log`). While this process is
        driving the campaign each phase file is a local file in the service pod's
        scratch (the same one the thread-isolated handlers write), read straight
        from *offset*. Once the campaign is no longer tracked here, the durable copy
        of each phase file in the object store is read.
        """
        from robovast.common.campaign_logs import (EXECUTION_DIR,
                                                    assemble_log,
                                                    assemble_log_from_dir)
        from robovast.service.interface import LogChunk
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is not None:
            campaign_dir = Path(entry.results_dir) / campaign_id
            text, next_offset, eof = assemble_log_from_dir(
                campaign_dir, offset, eof=self._is_done(entry))
            return LogChunk(text=text, next_offset=next_offset, eof=eof)
        # Past / reaped campaign: each phase file's durable copy is in the object store.
        from robovast.execution.cluster_execution import in_pod_storage
        try:
            cfg = self._cluster_config()
            bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
            # Interactive: this tail sits behind the log SSE stream, which re-polls while
            # the user watches. Returning an empty chunk beats blocking the stream.
            storage = in_pod_storage.storage_client_for(cfg, interactive=True)
        except Exception as e:  # noqa: BLE001 - best-effort; empty if unavailable
            logger.debug("could not resolve object store for %s: %s", campaign_id, e)
            return LogChunk(text="", next_offset=offset, eof=True)

        def _object_bytes(filename: str):
            try:
                raw = storage.read_object(bucket, f"{prefix}{EXECUTION_DIR}/{filename}")
            except Exception as e:  # noqa: BLE001 - a missing phase file is normal
                logger.debug("could not read %s for %s: %s", filename, campaign_id, e)
                return None
            if not raw:
                return None
            return raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")

        text, next_offset, eof = assemble_log(_object_bytes, offset, eof=True)
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
        """
        from robovast.execution.cluster_execution.cluster_execution import (
            _label_safe_campaign, list_jobs_with_phase)
        label = (f"jobgroup=scenario-runs,"
                 f"campaign-id={_label_safe_campaign(campaign_id)}")
        # Phase is pod-accurate: a Job whose pod is still Pending (unscheduled /
        # image-pulling / freshly Kueue-admitted) reports pending, not running.
        jobs = [
            JobSummary(job_name=job.metadata.name, status=phase,
                       display_name=self._job_display_name(campaign_id, job),
                       detail=detail)
            for job, phase, detail in list_jobs_with_phase(
                self._k8s_batch(), self._k8s(), self.namespace, label)]
        counts = JobCounts(
            running=sum(1 for j in jobs if j.status == "running"),
            pending=sum(1 for j in jobs if j.status == "pending"),
            waiting=sum(1 for j in jobs if j.status == "waiting"),
            completed=sum(1 for j in jobs if j.status == "completed"),
            failed=sum(1 for j in jobs if j.status == "failed"),
            blocked=sum(1 for j in jobs if j.status == "blocked"),
            total=len(jobs))
        return ListJobsResponse(jobs=jobs, counts=counts)

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

    #: Cap on cached job-log tails; oldest (LRU) are dropped past this.
    _JOB_LOG_CACHE_MAX = 128

    def _job_log_tail(self, campaign_id: str, job_name: str):
        """The cached :class:`PodLogTail` for a job, created on first use (LRU-bounded)."""
        from robovast.execution.cluster_execution.cluster_execution import PodLogTail
        key = (campaign_id, job_name)
        with self._job_log_guard:
            tail = self._job_log_tails.get(key)
            if tail is None:
                tail = PodLogTail()
                self._job_log_tails[key] = tail
                while len(self._job_log_tails) > self._JOB_LOG_CACHE_MAX:
                    self._job_log_tails.popitem(last=False)
            else:
                self._job_log_tails.move_to_end(key)
            return tail

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a running Job's live pod log from byte *offset* onward.

        Finds the Job's pod by the auto-added ``job-name`` label and streams *all* of
        its containers' logs merged into one stream (the main ``robovast`` container
        plus any secondary sim/SUT servers; see :class:`PodLogTail`). Reads are
        incremental: a cached tail keeps the full assembled text so the byte offset
        still maps onto it, but each poll only pulls the delta from the kube API
        rather than the whole log. Live source only: a pod still ``Pending`` has no
        log yet (empty, non-terminal chunk); a missing pod raises (→ 404).
        """
        from kubernetes import client
        from robovast.execution.cluster_execution.cluster_execution import \
            _label_safe_campaign
        core = self._k8s()
        label = (f"jobgroup=scenario-runs,"
                 f"campaign-id={_label_safe_campaign(campaign_id)},job-name={job_name}")
        pods = core.list_namespaced_pod(self.namespace, label_selector=label)
        if not pods.items:
            raise KeyError(f"no pod for job {job_name!r} in campaign {campaign_id!r}")
        pod = pods.items[0]
        if pod.status and pod.status.phase == "Pending":
            return LogChunk(text="", next_offset=offset, eof=False)
        tail = self._job_log_tail(campaign_id, job_name)
        try:
            with tail.lock:
                terminal = tail.read(core, pod, self.namespace, time.time())
                raw = bytes(tail.buf)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise KeyError(
                    f"pod for job {job_name!r} is gone (campaign {campaign_id!r})") from e
            raise
        return LogChunk(text=raw[offset:].decode("utf-8", "replace"),
                        next_offset=len(raw), eof=terminal)

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
        from robovast.service.image_build import (extract_build_specs,
                                                   validate_build_spec)
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        specs = extract_build_specs(campaign_config,
                                    Path(project.config_path).parent)
        if not specs:
            raise ValueError(
                "nothing to build: no container adds system_packages or "
                "python_packages, so every image is used as declared")
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
        registry = self._resolve_registry_objects(cfg.get_registry_config())
        if not registry.enabled():
            raise ValueError(
                "no container registry is configured for this cluster; in-cluster "
                "image builds are unavailable. Configure one at 'vast exec cluster "
                "setup' (or set ROBOVAST_REGISTRY_PREFIX / _PUSH_SECRET / "
                "_PULL_SECRET on the service).")
        from robovast.execution.cluster_execution.cluster_image_build import \
            build_context_bucket
        bucket = build_context_bucket(cfg)
        return project, campaign_config, specs, project_dir, cfg, registry, bucket

    def _resolve_build_ref(self, spec, project_dir, registry) -> "tuple[str, str]":
        """Return (concrete_registry_ref, image_hash) for a project's build image."""
        from robovast.common.execution import resolve_build_base_image
        from robovast.execution.cluster_execution.cluster_image_build import \
            concrete_image_ref
        from robovast.service.image_build import build_hash
        base_ref = (spec.base_image or registry.base_experiment_image
                    or resolve_build_base_image())
        image_hash = build_hash(spec, project_dir, base_ref)
        ref = concrete_image_ref(registry.registry_prefix, spec.tag, image_hash)
        return ref, image_hash

    def build_image(self, request):
        from robovast.common.config import SCENARIO_CONTAINER
        (_project, _cc, specs, project_dir, cfg, registry, bucket) = \
            self._build_context(request)
        refs = {name: self._start_cluster_build(spec, project_dir, cfg, registry, bucket)
                for name, spec in specs.items()}
        # Every build is started; the handle names one. Prefer the container the
        # scenario runs in, and carry the rest so the others can still be polled.
        primary = refs.get(SCENARIO_CONTAINER) or next(iter(refs.values()))
        primary.builds = {name: ref.build_id for name, ref in refs.items()}
        return primary

    def _start_cluster_build(self, spec, project_dir, cfg, registry, bucket):
        """Core (idempotent) launch shared by build_image + the campaign preflight."""
        from robovast.execution.cluster_execution.cluster_image_build import (
            build_id_for, build_job_manifest, cache_image_ref, context_prefix,
            s3_init_env, stage_context_to_s3)
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.service.image_build import generate_dockerfile
        from robovast.service.interface import ImageBuildRef, ImageBuildStatus
        from robovast.common.execution import BUILD_IMAGE_PREFIX
        from robovast.common.execution import resolve_build_base_image

        image_ref, image_hash = self._resolve_build_ref(spec, project_dir, registry)
        build_id = build_id_for(spec.tag, image_hash)
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
        if self._registry_has_image(image_ref, registry):
            status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="cached",
                                      done=True, cached=True, image_ref=symbolic,
                                      digest=image_hash)
            state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                               "hash": image_hash, "status": status}
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

        # Registered *before* staging so a concurrent build's context sweep can see
        # this build is in flight — its context exists in the object store for the
        # whole upload, while its Job does not exist yet.
        status = ImageBuildStatus(build_id=build_id, tag=spec.tag, phase="pending",
                                  image_ref=symbolic, digest=image_hash)
        state[build_id] = {"tag": spec.tag, "image_ref": image_ref,
                           "hash": image_hash, "status": status}

        # Everything up to a created Job is undone on failure: the in-flight record
        # holds the sweep back, so a submit that dies here (staging error, rejected
        # Job) would otherwise strand its context for as long as the service lives.
        try:
            # Stage the context (project dir + generated Dockerfile) to S3.
            base_ref = (spec.base_image or registry.base_experiment_image
                        or resolve_build_base_image())
            dockerfile = generate_dockerfile(spec, project_dir, base_ref)
            build_prefix = context_prefix(build_id)
            storage = in_pod_storage.storage_client_for(cfg)
            stage_context_to_s3(storage, bucket, build_prefix, project_dir, dockerfile)

            access_key, secret_key = cfg.get_s3_credentials()
            init_env = s3_init_env(cfg.get_s3_endpoint(), access_key, secret_key,
                                   bucket, build_prefix)
            manifest = build_job_manifest(
                build_id=build_id, image_ref=image_ref, campaign_label=build_id,
                init_env=init_env, push_secret_name=registry.push_secret_name,
                namespace=self.namespace, insecure=registry.insecure,
                ca_configmap_name=registry.ca_configmap_name,
                cache_ref=cache_image_ref(registry.registry_prefix, spec.tag),
                host_aliases=cfg.get_host_aliases())
            self._k8s_batch().create_namespaced_job(self.namespace, manifest)
        except BaseException:
            status.phase, status.done = "failed", True
            self._discard_build_context(cfg, bucket, build_id)
            raise
        status.phase = "building"
        return ImageBuildRef(build_id=build_id, tag=spec.tag, cached=False)

    def _discard_build_context(self, cfg, bucket: str, build_id: str) -> None:
        """Drop *build_id*'s staged context. Best-effort: a leftover copy of the
        project dir is not worth failing a finished build over, but it is worth a
        warning, since the next sweep is the only thing that will retry it."""
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.execution.cluster_execution.cluster_image_build import \
            discard_context
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
        from robovast.execution.cluster_execution.cluster_image_build import \
            staged_context_build_ids
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

    def _registry_has_image(self, image_ref: str, registry) -> bool:
        """Is *image_ref* already pushed? Fails closed (see ``registry_client``)."""
        from robovast.execution.cluster_execution.registry_client import \
            manifest_exists
        dockerconfig = self._push_dockerconfig(registry.push_secret_name)
        ca_path = self._registry_ca_path(registry.ca_configmap_name)
        return manifest_exists(image_ref, dockerconfigjson=dockerconfig,
                               insecure=registry.insecure, ca_path=ca_path)

    def _push_dockerconfig(self, push_secret_name: str) -> str:
        """The push Secret's ``.dockerconfigjson``, or ``""`` when unavailable.

        Same credential the build Job mounts; read here only to authenticate a read-only
        manifest probe. Never returned to a client.
        """
        if not push_secret_name:
            return ""
        from kubernetes import client
        try:
            secret = self._k8s().read_namespaced_secret(push_secret_name, self.namespace)
        except client.exceptions.ApiException as e:
            logger.warning("registry check: cannot read push secret %s: %s",
                           push_secret_name, e)
            return ""
        data = (secret.data or {}).get(".dockerconfigjson")
        if not data:
            return ""
        import base64
        try:
            return base64.b64decode(data).decode()
        except (ValueError, UnicodeDecodeError):
            logger.warning("registry check: push secret %s is not decodable",
                           push_secret_name)
            return ""

    def _resolve_registry_objects(self, registry):
        """Fill in the push/pull Secret and CA ConfigMap by *looking for them*.

        Their names are fixed constants written by ``vast exec cluster setup``, so the
        ``ROBOVAST_REGISTRY_{PUSH,PULL}_SECRET`` / ``_CA_CONFIGMAP`` variables were never
        carrying a name — only the fact that setup had created the object, since
        referencing a Secret that does not exist keeps the pod from starting. Setup writes
        them into the *deployed service pod's* env, so an **off-cluster** ``vast serve``
        never learned them and silently pushed anonymously to an untrusted registry.

        Checking existence covers both deployments identically. An explicitly set variable
        still wins, for a deployment that named its objects differently.
        """
        from kubernetes import client
        from robovast.execution.cluster_execution.service_deploy import (
            REGISTRY_CA_CONFIGMAP_NAME, REGISTRY_PUSH_SECRET_NAME)

        def exists(read, name):
            try:
                read(name, self.namespace)
                return True
            except client.exceptions.ApiException as e:
                if e.status not in (403, 404):
                    raise
                if e.status == 403:
                    # In-pod without RBAC for this read: say so rather than treating it as
                    # absent, which would look like "no credentials configured".
                    logger.warning(
                        "not permitted to read %r in %s; cannot tell whether the registry "
                        "object exists", name, self.namespace)
                return False

        core = self._k8s()
        if not registry.push_secret_name and exists(
                core.read_namespaced_secret, REGISTRY_PUSH_SECRET_NAME):
            registry.push_secret_name = REGISTRY_PUSH_SECRET_NAME
            logger.info("using registry push Secret %r", REGISTRY_PUSH_SECRET_NAME)
        if not registry.pull_secret_name and registry.push_secret_name:
            # One dockerconfigjson serves both directions (setup wires it that way).
            registry.pull_secret_name = registry.push_secret_name
        if not registry.ca_configmap_name and exists(
                core.read_namespaced_config_map, REGISTRY_CA_CONFIGMAP_NAME):
            registry.ca_configmap_name = REGISTRY_CA_CONFIGMAP_NAME
            logger.info("using registry CA ConfigMap %r", REGISTRY_CA_CONFIGMAP_NAME)
        return registry

    def _registry_ca_path(self, ca_configmap_name: str) -> str:
        """Materialize the registry CA to a file for ``requests``' ``verify=``.

        Cached per ConfigMap name: this runs on every build submit, and a fresh temp file
        each time would leak one per call for the service's lifetime.
        """
        if not ca_configmap_name:
            return ""
        cache = getattr(self, "_registry_ca_paths", None)
        if cache is None:
            cache = {}
            self._registry_ca_paths = cache
        if ca_configmap_name in cache:
            return cache[ca_configmap_name]
        from kubernetes import client
        try:
            cm = self._k8s().read_namespaced_config_map(ca_configmap_name, self.namespace)
            pem = (cm.data or {}).get("ca.pem", "")
        except client.exceptions.ApiException as e:
            logger.warning("registry check: cannot read CA configmap %s: %s",
                           ca_configmap_name, e)
            pem = ""
        path = ""
        if pem:
            import tempfile
            fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 - lives for the process
                mode="w", suffix=".pem", prefix="robovast-registry-ca-", delete=False)
            fd.write(pem)
            fd.close()
            path = fd.name
        cache[ca_configmap_name] = path
        return path

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
            return ImageBuildStatus(
                build_id=build_id, phase=phase, done=done,
                cached=phase == "succeeded")
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
            status.error = self._build_error(build_id, record["tag"])
        if status.done:
            # This transition is the one moment we know the context is dead, for both
            # outcomes. Cheap (a prefix delete) and it runs once, since a done record
            # returns above.
            self._retire_build_context(build_id)
        return status

    def _retire_build_context(self, build_id: str) -> None:
        """Discard a just-finished build's staged context, resolving the bucket."""
        from robovast.execution.cluster_execution.cluster_image_build import \
            build_context_bucket
        try:
            cfg = self._cluster_config()
            bucket = build_context_bucket(cfg)
        except Exception as e:  # noqa: BLE001 - cleanup must not fail a status read
            logger.warning("cannot resolve the build-context bucket for %s: %s",
                           build_id, e)
            return
        self._discard_build_context(cfg, bucket, build_id)

    def _build_error(self, build_id: str, tag: str):
        from robovast.service.image_build import classify_build_error
        log = self._build_log_text(build_id)
        return classify_build_error(log)

    def _build_log_text(self, build_id: str) -> str:
        from kubernetes import client
        core = self._k8s()
        pods = core.list_namespaced_pod(
            self.namespace, label_selector=f"build-id={build_id}")
        if not pods.items:
            return ""
        pod = pods.items[0]
        try:
            return core.read_namespaced_pod_log(
                name=pod.metadata.name, namespace=self.namespace,
                container="buildkit")
        except client.exceptions.ApiException:
            return ""

    def get_image_build_log(self, build_id: str, offset: int = 0):
        from robovast.service.interface import LogChunk
        raw = self._build_log_text(build_id).encode("utf-8", "replace")
        record = self._image_build_state().get(build_id)
        done = bool(record and record["status"].done)
        return LogChunk(text=raw[offset:].decode("utf-8", "replace"),
                        next_offset=len(raw), eof=done)

    def _campaign_build_context(self, project, campaign_config):
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
        from robovast.service.image_build import (extract_build_specs,
                                                  validate_build_spec)
        specs = extract_build_specs(campaign_config)
        if not specs:
            return None
        project_dir = Path(project.config_path).resolve().parent
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise CampaignConfigError(
                    f"invalid execution.containers.{name}:\n  - " + "\n  - ".join(problems))
        cfg = self._cluster_config()
        registry = self._resolve_registry_objects(cfg.get_registry_config())
        if not registry.enabled():
            raise CampaignConfigError(
                "this campaign builds a container image, but no container registry is "
                "configured for this cluster (see 'vast exec cluster setup').")
        return specs, project_dir, cfg, registry

    def _start_build_images(self, project, campaign_config) -> list:
        """Submit (or join) an in-cluster BuildKit Job per image this campaign builds.

        Returns as soon as each build has a handle; ``LocalTransport._await_build_image``
        waits on them over the interface, so both lanes share one wait loop.
        """
        resolved = self._campaign_build_context(project, campaign_config)
        if resolved is None:
            return []
        specs, project_dir, cfg, registry = resolved
        from robovast.execution.cluster_execution.cluster_image_build import \
            build_context_bucket
        bucket = build_context_bucket(cfg)
        return [self._start_cluster_build(spec, project_dir, cfg, registry, bucket)
                for spec in specs.values()]

    def _resolve_built_images(self, project, campaign_config) -> dict:
        """Concrete registry refs to pin, by container name."""
        specs, project_dir, _cfg, registry = self._campaign_build_context(
            project, campaign_config)
        return {name: self._resolve_build_ref(spec, project_dir, registry)[0]
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
        cluster workloads (the same Kueue-aware cleanup ``vast exec cluster
        run-cleanup`` performs): the running pods terminate now, the batch wait loop
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

    def _teardown_campaign_jobs(self, campaign_id: str) -> None:
        """Delete one campaign's in-flight cluster workloads (Kueue-aware, scoped).

        Reuses ``cleanup_cluster_campaign`` — the same teardown ``vast exec cluster
        run-cleanup`` performs — so the running pods terminate now and the driver's
        batch wait loop unblocks. Label-scoped to this campaign's Jobs/Workloads/pods,
        and it leaves the shared ClusterQueue alone, so a concurrent campaign keeps
        being admitted while this one is torn down.
        """
        from robovast.execution.cluster_execution.cluster_execution import \
            cleanup_cluster_campaign
        cleanup_cluster_campaign(namespace=self.namespace, campaign=campaign_id,
                                 context=self.kube_context)

    def _terminate_running_campaigns(self, running) -> None:
        """On ``vast serve`` shutdown (Ctrl+C), tear down every running campaign's Jobs.

        Overrides the local single-container kill: a cluster campaign's compute is its
        scenario Jobs, so a bare service exit would orphan them (they keep consuming
        cluster resources). Each teardown is best-effort so one failure never blocks
        the others or the process exit.
        """
        for entry in running:
            try:
                self._teardown_campaign_jobs(entry.campaign_id)
            except Exception:  # noqa: BLE001 - shutdown must not mask the exit
                logger.warning("Could not tear down jobs for %s during shutdown",
                               entry.campaign_id, exc_info=True)

    # -- container exec -----------------------------------------------------

    def _exec_lane(self):
        """The in-cluster exec lane: one aux pod, driven through ``pods/exec``.

        Staging goes through the object store, exactly as an image build's context does
        (see ``_start_cluster_build``) — the same bucket resolution, the same
        service-side client, and the pod given the *cluster-internal* endpoint rather
        than whatever this process is using to reach the store.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.execution.cluster_execution.cluster_image_build import \
            build_context_bucket
        from robovast.execution.cluster_execution.container_runner import \
            service_pod_owner_reference
        from robovast.service.kube_exec_lane import KubeExecLane
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
                            # Deferred: off-cluster, building this opens a port-forward,
                            # and the stray-reap builds a lane it never stages into.
                            storage_factory=lambda: in_pod_storage.storage_client_for(cfg),
                            bucket=bucket,
                            s3_endpoint=cfg.get_s3_endpoint(),
                            s3_access_key=access_key, s3_secret_key=secret_key)

    def _reap_stray_exec_container(self) -> None:
        """Delete an exec pod and its staged tree, left by a previous service process."""
        try:
            self._exec_lane().stop_held()
        except Exception as e:  # noqa: BLE001 - a missing cluster must not break startup
            logger.debug("could not check for a stray exec pod: %s", e)

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

        A service restart abandons its in-flight campaigns (the accepted trade), so
        their aux pods would linger. Aux pods are owned by the service pod and thus
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
                    # Match both the raw id and its sanitised bucket name, since
                    # ``cleanup_campaigns`` compares against object-store names.
                    running.add(c.campaign_id)
                    running.add(bucket_ops._bucket_name(c.campaign_id))
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
        for cid in self._campaign_index():
            if cid in gone or bucket_ops._bucket_name(cid) in gone:
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
        from robovast.execution.cluster_execution.cluster_execution import \
            cleanup_cluster_campaign

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
        # 4. Service-local caches: the fetch scratch and any in-pod driver dir.
        shutil.rmtree(self._cache_dir(campaign_id), ignore_errors=True)
        shutil.rmtree(self._campaign_dir(campaign_id), ignore_errors=True)
        with self._lock:
            self._campaigns.pop(campaign_id, None)
        return ActionResult(
            ok=True,
            message=f"Deleted campaign {campaign_id!r} (object store, jobs, cache).")

    # -- data / results -----------------------------------------------------

    def _cache_dir(self, campaign_id: str) -> Path:
        """Local scratch mirroring a campaign's objects. Ephemeral by design — the object
        store is the durable home — and shared by the whole-campaign fetch and the
        query-database fetch, so the two can never hold divergent copies of one file."""
        return Path("/tmp") / "robovast-campaigns" / campaign_id  # noqa: S108 - pod scratch

    def _data_dir(self, campaign_id: str):
        """Whole-campaign dir: pulled from the object store (the durable home) into the
        local cache, for callers that need arbitrary campaign files — notebook render,
        panel assets, endpoint plugins via ``resolve_data_dir``. A **query** must not come
        through here; see :meth:`_query_dir`."""
        return self.fetch_campaign(campaign_id)

    #: The only two objects a SQL query reads, relative to the campaign prefix (see
    #: ``data_query._open_db``). Neither is required to exist: ``campaign.db`` alone is
    #: queryable before postprocessing has built ``data.db``.
    _QUERY_DBS = ("_execution/data.db", "campaign.db")

    def _materialize(self, campaign_id: str, rel_paths, subject: str, *,
                     interactive: bool = False) -> Path:
        """Copy named objects of a campaign into its cache dir; return the dir.

        A **single-object** fetch per path — the same discipline as the ``/results`` file
        overrides below, and the reason both callers exist: pulling the campaign prefix to
        read a 40 MB ``data.db`` (or a 2 KB ``outcome.json``) drags every rosbag the
        campaign produced, in the deployment where campaigns are largest. Writes into the
        *same* cache dir, so a later full ``fetch_campaign`` finds these files already at
        the right size and skips them.

        A cached copy is validated by **size, not existence**: ``data.db`` and
        ``outcome.json`` are both rewritten in place by re-postprocessing, and an existence
        check would pin the first version this service ever saw and serve it forever.

        A missing object is skipped, not an error: whether "not published yet" is a problem
        is the caller's question, and each answers it differently (``_open_db`` raises its
        own clear message; a campaign with no ``outcome.json`` reconstructs to ``unknown``).
        """
        from botocore.exceptions import (  # pylint: disable=import-outside-toplevel
            ClientError, EndpointConnectionError)

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
        with lock:
            fetched = total = 0
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
                    storage.download_object(bucket, f"{prefix}{rel}", str(dst))
                    fetched += size
            except EndpointConnectionError as exc:
                # Object store unreachable (e.g. a dropped port-forward): a clean 4xx
                # instead of botocore bubbling up as an ASGI 500 — as in fetch_campaign.
                raise RuntimeError(f"Object store is unreachable: {exc}") from exc
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

    def _scene_source_dir(self, campaign_id: str) -> str:
        """Materialise only what resolving geometry reads, then answer from that.

        One small object -- ``execution.yaml`` -- against ``fetch_campaign``'s whole prefix, which for a
        25-run campaign is rosbags. The run's capture manifest is fetched by ``_scene_capture`` below,
        because its path depends on the run.
        """
        self._materialize(campaign_id, ("_execution/execution.yaml",), "execution metadata",
                          interactive=True)
        return str(self._cache_dir(campaign_id))

    def _scene_capture(self, campaign_id: str, config_name: str, run_id: str) -> dict:
        """Fetch this run's capture manifest, then read it the way the base class does.

        Without this the cluster lane would look for a file on a disk that has none -- the gap that makes
        ``exec_in_container(campaign_id=…)`` fail for a campaign this service did not drive.
        """
        rel = f"{config_name}/{run_id}/capture/capture.json"
        self._materialize(campaign_id, (rel,), "run capture manifest", interactive=True)
        return super()._scene_capture(campaign_id, config_name, run_id)

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
        from robovast.execution.cluster_execution.container_runner import AuxPodSession

        del campaign_id
        image = identity["image"]
        # A pod name has to be label-safe and stable for this world, and an image digest is neither
        # short nor label-safe. `aux_pod_name` sanitises what it is given, so give it a digest of the
        # digest: same world -> same name, which also makes a duplicate create a 409 the session reuses.
        tag = f"scene-{hashlib.sha256(image.encode()).hexdigest()[:12]}"
        spec = ContainerSpec(image=image)
        pull_secret = self._scene_pull_secret()

        @contextlib.contextmanager
        def context():
            with AuxPodSession(tag, [spec], self.namespace, core_v1=self._k8s(),
                               pull_secret=pull_secret,
                               kube_context=self.kube_context,
                               **self._aux_store_kwargs()) as session:
                yield session.runner_factory()

        return context

    def _scene_pull_secret(self) -> str:
        """The registry pull secret, so an aux pod can pull the campaign's *private* image.

        Aux images were public when that path was written, so it never needed one, and a node that has
        already cached the campaign image hides the omission (``imagePullPolicy: IfNotPresent``) -- which
        means it first fails on a fresh node, the worst place to discover it.
        """
        try:
            from robovast.execution.cluster_execution.cluster_execution import \
                REGISTRY_PUSH_SECRET_NAME
            self._k8s().read_namespaced_secret(REGISTRY_PUSH_SECRET_NAME, self.namespace)
            return REGISTRY_PUSH_SECRET_NAME
        except Exception:  # noqa: BLE001 - an optional secret; a public image needs none
            return ""

    def _query_dir(self, campaign_id: str):
        """Materialize just the query databases into the campaign's cache dir; return it."""
        return self._materialize(campaign_id, self._QUERY_DBS, "query databases")

    #: The campaign's **recorded facts**: its store row (start time, description, per-run
    #: tallies) and its durable terminal outcome. Both small and both enough to summarize a
    #: campaign without fetching any of its results.
    _RECORD_OBJECTS = ("campaign.db", "_execution/outcome.json")

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
        if cid not in self._campaign_index():
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
        """Cluster: a query reads the object store, so report what that will cost.

        Two ``stat_object`` calls — deliberately not a listing of the campaign prefix,
        which is the cost this whole seam exists to avoid.
        """
        from robovast.service.interface import CampaignDataStatus
        in_pod = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
        dest = self._cache_dir(campaign_id)
        # Interactive: two ``stat_object`` calls whose whole purpose is to be cheap enough
        # to ask before a query — a minutes-long block here defeats the seam.
        storage, bucket, prefix = self._campaign_object_location(
            campaign_id, interactive=True)
        db_bytes, cached = 0, True
        for rel in self._QUERY_DBS:
            size = storage.stat_object(bucket, f"{prefix}{rel}")
            if size is None:
                continue
            db_bytes += size
            dst = dest / Path(rel)
            if not (dst.exists() and dst.stat().st_size == size):
                cached = False
        with self._fetch_locks_guard:
            lock = self._fetch_locks.get(campaign_id)
        last = self._last_fetch.get(campaign_id)
        if cached:
            note = ("the campaign's query databases are already in the service cache; "
                    "this query reads them locally")
        else:
            where = ("the in-cluster object store" if in_pod else
                     "the object store through a kubectl port-forward")
            note = (f"the query databases are not in the service cache yet; they are "
                    f"fetched from {where} first")
        return CampaignDataStatus(
            campaign_id=campaign_id, source="object-store", fetch_required=True,
            cached=cached, transfer="cluster-network" if in_pod else "port-forward",
            db_bytes=db_bytes,
            fetch_in_progress=bool(lock is not None and lock.locked()),
            last_fetch_bytes=None if last is None else last[0],
            last_fetch_seconds=None if last is None else last[1],
            note=note)

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
        from robovast.common.safe_path import check_relative
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

    def _publish_execution(self, campaign_id: str, campaign_root) -> None:
        """Upload a campaign's ``_execution/`` (outcome + logs + data.db) to the store."""
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
        ``data.db`` step runs here (pure Python) — the same two stages the campaign loop
        chains. ``postprocess_campaign`` streams into the scratch ``postprocessing.log``,
        which is published to the object store so the Monitor and a later restart see it.
        """
        from robovast.execution.status_recovery import record_step_outcome
        from robovast.execution.cluster_execution.postprocess_job import \
            postprocess_campaign

        def work(state):
            campaign_root = self.fetch_campaign(request.campaign_id, force=True)
            cfg = self._cluster_config()
            ok, message = postprocess_campaign(
                cfg, request.campaign_id, str(campaign_root), self.namespace,
                force=request.force, skip=list(request.skip or []),
                kube_context=self.kube_context)
            status = record_step_outcome(campaign_root, postprocessing=(ok, message))
            # Publish _execution (outcome + the conversion's postprocessing.log, even on
            # failure) so the result survives a restart and the Monitor can read it.
            self._publish_execution(request.campaign_id, campaign_root)
            state.update(postprocessed=status.postprocessed,
                         postprocessing_error=status.postprocessing_error)
            state.set_phase(Phase.FINISHED)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.POSTPROCESSING, work=work)

    def run_share(self, request) -> ActionResult:
        """(Re)trigger upload-to-share for a cluster campaign, as a monitored background
        operation. Fetches the campaign and streams it to the env-configured provider
        (``preflight_upload_to_share`` fails loudly if ``ROBOVAST_SHARE_TYPE`` is unset);
        the outcome (clear/set ``share_error``) is recorded and published. Adjusting the
        share env and re-triggering re-uploads to the new provider.
        """
        from robovast.common.status import failure_detail
        from robovast.execution.backends import RunOptions
        from robovast.execution.control_server import ControllerState
        from robovast.execution.status_recovery import record_step_outcome

        def work(state):
            campaign_root = self.fetch_campaign(request.campaign_id, force=True)
            backend = self._build_backend(ControllerState())
            options = RunOptions(gui=False, upload_to_share=True, namespace=self.namespace)
            try:
                backend.preflight_upload_to_share()
                backend.share_campaign(str(campaign_root), options)
                ok, message = True, "upload-to-share complete"
            except Exception as e:  # noqa: BLE001 - surfaced via status + share_error
                ok, message = False, failure_detail(e)
            status = record_step_outcome(campaign_root, share=(ok, message))
            self._publish_execution(request.campaign_id, campaign_root)
            state.update(share_error=status.share_error)
            state.set_phase(Phase.FINISHED)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.SHARING, work=work)

    def campaign_tar_stream(self, campaign_id: str):
        """Yield a ``tar.gz`` of the postprocessed campaign, streamed from the object store.

        Backs ``GET /campaigns/{id}/archive``. Objects are fetched and tarred on the
        fly (:func:`campaign_archive.iter_tar`), so **no scratch is used on the service
        during or after the download** — decisive for ~1TB campaigns. ``_postproc/``
        internal staging is excluded so the archive is the clean campaign layout.
        """
        from robovast.execution import \
            campaign_archive  # pylint: disable=import-outside-toplevel
        cfg = self._cluster_config()
        return campaign_archive.iter_tar(
            lambda tar: cfg.add_campaign_members(
                tar, campaign_id, exclude_prefixes={"_postproc"}))

    def fetch_campaign(self, campaign_id: str, force: bool = False):
        """Pull a finished campaign from the object store to a local dir; return it.

        The object store is the durable home (the campaign loop published the full
        campaign there via ``finalize_campaign``). The stateless service pulls it
        into ephemeral scratch on demand — to serve a download or re-postprocess.

        Objects are immutable, so files already present locally with a matching
        size are left untouched (see ``download_prefix``): repeat pulls — e.g. a
        notebook re-render — become near-noops. Pass ``force=True`` to overwrite
        the local cache unconditionally.
        """
        from botocore.exceptions import (  # pylint: disable=import-outside-toplevel
            ClientError, EndpointConnectionError)

        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        dest = self._cache_dir(campaign_id)
        dest.mkdir(parents=True, exist_ok=True)
        storage = in_pod_storage.storage_client_for(cfg)
        with self._fetch_locks_guard:
            lock = self._fetch_locks.setdefault(campaign_id, threading.Lock())
        # Serialize fetches of the same campaign: the first request populates the
        # cache while the rest wait, then find it complete and skip re-downloading
        # (immutable objects, matching size). Different campaigns still fetch in
        # parallel.
        started = time.perf_counter()
        with lock:
            try:
                # A whole campaign is GBs over a port-forward; without a running count the
                # transfer is indistinguishable from a hang for as long as it takes.
                n = storage.download_prefix(
                    bucket, prefix, str(dest), force=force,
                    on_file=in_pod_storage.download_progress_logger(
                        f"Campaign {campaign_id}"))
            except EndpointConnectionError as exc:
                # Object store unreachable (e.g. a dropped port-forward). Surface a
                # clean 4xx instead of letting botocore bubble up as an ASGI 500.
                raise RuntimeError(
                    f"Object store is unreachable: {exc}") from exc
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
        # a caller staring at a slow first call actually has. Bytes are left out on
        # purpose — ``download_prefix`` does not report them, and summing the cache dir
        # would stat every file of a campaign that can hold 100k of them.
        logger.info("Fetched campaign %s (%d file(s)) from %s/%s to %s in %.1fs",
                    campaign_id, n, bucket, prefix, dest,
                    time.perf_counter() - started)
        return dest
