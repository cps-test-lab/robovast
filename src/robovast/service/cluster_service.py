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

from robovast.execution.control_server import Phase, Status, is_running
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
        self._pf_lock = threading.Lock()
        # Per-campaign locks so concurrent data queries don't each re-download the
        # same campaign into the shared cache dir (the results explorer fires one
        # query per sub-view on first load). Guarded by ``_fetch_locks_guard``.
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._fetch_locks_guard = threading.Lock()
        # Incremental, cache-backed job-log tails so a polling log panel fetches only
        # the delta each 1.5s instead of re-reading the whole pod log (see get_job_log
        # / PodLogTail). LRU-bounded so long-lived services don't accumulate buffers.
        self._job_log_tails: "OrderedDict[tuple, object]" = OrderedDict()
        self._job_log_guard = threading.Lock()
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
        quota is derived from); usage is the sum of resource *requests* of all
        non-terminal pods cluster-wide (what the scheduler has committed). Both are
        read behind :meth:`LocalTransport.resource_usage`'s TTL cache, and the pod
        list is filtered server-side to skip finished pods — so a poll costs at most
        one ``list_node`` + one filtered ``list_pod`` per cache window.

        Requires the service's ClusterRole (nodes/pods get,list — see
        ``service_deploy._service_rbac_manifests``).
        """
        from robovast.execution.cluster_execution.kubernetes_kueue import \
            _parse_resource  # pylint: disable=import-outside-toplevel
        v1 = self._k8s()

        cpu_capacity = 0.0
        mem_capacity = 0
        for node in v1.list_node().items:
            alloc = node.status.allocatable or {}
            cpu_capacity += _parse_resource(alloc.get("cpu"))
            mem_capacity += int(_parse_resource(alloc.get("memory")))

        cpu_used = 0.0
        mem_used = 0
        jobs_running = 0
        jobs_pending = 0
        pods = v1.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed")
        for pod in pods.items:
            for container in (pod.spec.containers or []):
                requests = (container.resources.requests
                            if container.resources else None) or {}
                cpu_used += _parse_resource(requests.get("cpu"))
                mem_used += int(_parse_resource(requests.get("memory")))
            # Backend-wide scenario-run tally, pod-accurate (Running vs still-waiting)
            # so the sidebar's jobs bar matches k9s. Free — same pod list as above.
            if (pod.metadata.labels or {}).get("jobgroup") == "scenario-runs":
                if pod.status and pod.status.phase == "Running":
                    jobs_running += 1
                else:
                    jobs_pending += 1

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
                self._minio_pf, port = open_minio_port_forward(
                    self.namespace, self.kube_context)
                self._minio_pf_endpoint = f"http://localhost:{port}"
                logger.info("Opened MinIO port-forward for driver S3 at %s",
                            self._minio_pf_endpoint)
            return self._minio_pf_endpoint

    def _close_minio_pf_locked(self) -> None:
        """Terminate the current MinIO port-forward. Caller must hold ``_pf_lock``."""
        pf, self._minio_pf = self._minio_pf, None
        self._minio_pf_endpoint = None
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
                           core_v1=self._k8s() if specs else None) as session:
            if specs:
                set_container_runner_factory(session.runner_factory())
            yield

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

    def _status_from_disk(self, campaign_id: str) -> Status:
        """Fallback for a campaign this process is not driving.

        Precedence, matching what ``_summary_for``/``list_campaigns`` already do so the
        per-campaign status can never disagree with the list view:

        1. the durable ``_execution/outcome.json`` published to the object store (the
           canonical terminal record — ``finished`` / ``failed`` / ``stopped``, plus any
           ``postprocessing_error`` / ``share_error``);
        2. otherwise reconstruct from the campaign's on-disk artifacts. Off-cluster the
           driver downloads every campaign under the local results root, so a campaign
           whose runs finished but that never got a durable outcome (e.g. an older run,
           or one lost to a restart) reconstructs as ``finished`` from its ``test.xml``
           results instead of a bare ``unknown``. In-pod there is no local scratch, so
           this yields ``unknown`` for a missing dir — the same answer as before.
        """
        outcome = self._read_outcome(campaign_id)
        if outcome is not None:
            return outcome
        from robovast.execution.status_recovery import \
            reconstruct_status_from_disk
        return reconstruct_status_from_disk(self._campaigns_root() / campaign_id)

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
            storage = in_pod_storage.storage_client_for(cfg)
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
        """Resolve (spec, project_dir, cfg, registry) for a build request.

        Raises ``ValueError`` (→ 400) with an actionable message when the project
        has no ``build:`` section, the section is invalid, or the deployment has no
        registry configured (registry details live only in the cluster config).
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.service.image_build import (extract_build_spec,
                                                   validate_build_spec)
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        spec = extract_build_spec(campaign_config)
        if spec is None:
            raise ValueError(
                "project has no 'build:' section — nothing to build (set a build: "
                "section and execution.image: build:<tag>)")
        project_dir = Path(project.config_path).resolve().parent
        problems = validate_build_spec(spec, project_dir)
        if problems:
            raise ValueError("invalid build: section:\n  - " + "\n  - ".join(problems))
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
        return project, campaign_config, spec, project_dir, cfg, registry, bucket

    def _resolve_build_ref(self, spec, project_dir, registry) -> "tuple[str, str]":
        """Return (concrete_registry_ref, image_hash) for a project's build image."""
        from robovast.common.execution import resolve_robovast_image
        from robovast.execution.cluster_execution.cluster_image_build import \
            concrete_image_ref
        from robovast.service.image_build import build_hash
        base_ref = (spec.base_image or registry.base_experiment_image
                    or resolve_robovast_image())
        image_hash = build_hash(spec, project_dir, base_ref)
        ref = concrete_image_ref(registry.registry_prefix, spec.tag, image_hash)
        return ref, image_hash

    def build_image(self, request):
        (_project, _cc, spec, project_dir, cfg, registry, bucket) = \
            self._build_context(request)
        return self._start_cluster_build(spec, project_dir, cfg, registry, bucket)

    def _start_cluster_build(self, spec, project_dir, cfg, registry, bucket):
        """Core (idempotent) launch shared by build_image + the campaign preflight."""
        from robovast.execution.cluster_execution.cluster_image_build import (
            build_id_for, build_job_manifest, cache_image_ref, context_prefix,
            s3_init_env, stage_context_to_s3)
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.service.image_build import generate_dockerfile
        from robovast.service.interface import ImageBuildRef, ImageBuildStatus
        from robovast.common.config import BUILD_IMAGE_PREFIX
        from robovast.common.execution import resolve_robovast_image

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
                        or resolve_robovast_image())
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

    def _ensure_build_image(self, project, campaign_config) -> "str | None":
        """Cluster preflight: build (or reuse) the project's build: image.

        Overrides the LocalTransport (docker) preflight, working directly from the
        already-resolved project/config. Returns the concrete registry ref to pin as
        the explicit image, or ``None`` when there is no ``build:`` section. Blocks
        until the build finishes; raises ``RuntimeError`` on failure.
        """
        from robovast.service.image_build import (extract_build_spec,
                                                   validate_build_spec)
        spec = extract_build_spec(campaign_config)
        if spec is None:
            return None
        project_dir = Path(project.config_path).resolve().parent
        problems = validate_build_spec(spec, project_dir)
        if problems:
            raise ValueError("invalid build: section:\n  - " + "\n  - ".join(problems))
        cfg = self._cluster_config()
        registry = self._resolve_registry_objects(cfg.get_registry_config())
        if not registry.enabled():
            raise ValueError(
                "execution.image is a build:<tag> ref but no container registry is "
                "configured for this cluster (see 'vast exec cluster setup').")
        from robovast.execution.cluster_execution.cluster_image_build import \
            build_context_bucket
        bucket = build_context_bucket(cfg)
        image_ref, _hash = self._resolve_build_ref(spec, project_dir, registry)
        ref = self._start_cluster_build(spec, project_dir, cfg, registry, bucket)
        status = self._await_cluster_build(ref.build_id)
        if status.phase not in ("succeeded", "cached"):
            err = status.error
            detail = f" ({err.message})" if err and err.message else ""
            raise RuntimeError(
                f"experiment image build '{spec.tag}' failed{detail}; "
                f"see the build log (build_id={ref.build_id})")
        return image_ref

    def _await_cluster_build(self, build_id: str):
        while True:
            status = self.get_image_build_status(build_id)
            if status.done:
                return status
            time.sleep(3.0)

    def _read_outcome(self, campaign_id: str) -> "Status | None":
        """Read the campaign's durable terminal outcome from the object store."""
        from robovast.execution.cluster_execution import in_pod_storage
        try:
            cfg = self._cluster_config()
            bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
            storage = in_pod_storage.storage_client_for(cfg)
            raw = storage.read_object(bucket, f"{prefix}_execution/outcome.json")
        except Exception as e:  # noqa: BLE001 - best-effort; fall back to bare phase
            logger.debug("could not read outcome for %s: %s", campaign_id, e)
            return None
        if not raw:
            return None
        try:
            return Status.model_validate_json(raw)
        except Exception as e:  # noqa: BLE001 - malformed record; ignore
            logger.debug("malformed outcome.json for %s: %s", campaign_id, e)
            return None

    # list_campaigns is inherited from LocalTransport: off-cluster the driver runs
    # in this process and writes each campaign under the local results dir, so the
    # disk scan surfaces both live campaigns and ones from before a `vast serve`
    # restart. (`_summary_for` uses the in-memory snapshot while a campaign is being
    # driven and otherwise derives `postprocessed` from the on-disk data.db.)

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

    # -- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Stop running campaigns, then tear down the shared MinIO port-forward."""
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
        count = bucket_ops.cleanup_campaigns(
            self._cluster_config(), namespace=self.namespace,
            context=self.kube_context, campaign_id=request.campaign_id,
            running_campaigns=running)
        return ActionResult(
            ok=True, message=f"Removed {count} bucket(s) from the object store.")

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
        # 3. Service-local caches: the fetch scratch and any in-pod driver dir.
        shutil.rmtree(Path("/tmp") / "robovast-campaigns" / campaign_id,  # noqa: S108
                      ignore_errors=True)
        shutil.rmtree(self._campaign_dir(campaign_id), ignore_errors=True)
        with self._lock:
            self._campaigns.pop(campaign_id, None)
        return ActionResult(
            ok=True,
            message=f"Deleted campaign {campaign_id!r} (object store, jobs, cache).")

    # -- data / results -----------------------------------------------------

    def _data_dir(self, campaign_id: str):
        """Data-query campaign dir: pull it from the object store (the durable home)
        into a local cache, so ``describe_campaign_data``/``query_campaign_data_sql``
        (inherited from :class:`LocalTransport`) read the fetched ``data.db``."""
        return self.fetch_campaign(campaign_id)

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

    def _campaign_object_location(self, campaign_id: str):
        """``(storage, bucket, prefix)`` for a campaign's objects."""
        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        return in_pod_storage.storage_client_for(cfg), bucket, prefix

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
        dest = Path("/tmp") / "robovast-campaigns" / campaign_id  # noqa: S108 - pod scratch
        dest.mkdir(parents=True, exist_ok=True)
        storage = in_pod_storage.storage_client_for(cfg)
        with self._fetch_locks_guard:
            lock = self._fetch_locks.setdefault(campaign_id, threading.Lock())
        # Serialize fetches of the same campaign: the first request populates the
        # cache while the rest wait, then find it complete and skip re-downloading
        # (immutable objects, matching size). Different campaigns still fetch in
        # parallel.
        with lock:
            try:
                n = storage.download_prefix(bucket, prefix, str(dest), force=force)
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
        logger.info("Fetched campaign %s (%d file(s)) from %s/%s to %s",
                    campaign_id, n, bucket, prefix, dest)
        return dest
