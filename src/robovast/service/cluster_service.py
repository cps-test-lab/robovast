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
import logging
import os
import threading
from pathlib import Path

from robovast.execution.control_server import Phase, Status, is_running
from robovast.service.client import LocalTransport
from robovast.service.interface import (ActionResult, JobCounts, JobSummary,
                                        ListJobsResponse, LogChunk,
                                        ResourceUsage, VersionInfo)

logger = logging.getLogger(__name__)

CONTROLLER_LABEL = "app=robovast-controller"
AUX_LABEL = "app=robovast-aux"
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"


class ClusterService(LocalTransport):
    """Interface implementation that drives campaigns in-process over Kubernetes."""

    def __init__(self, namespace=None, cluster_config_name=None,
                 cluster_config_kwargs=None, image=None, store=None,
                 reap_on_start=True, kube_context=None):
        super().__init__(store=store)
        self.namespace = namespace or os.environ.get("ROBOVAST_NAMESPACE", "default")
        # Which kubeconfig context to dispatch into. None off-cluster means the
        # active context; in-cluster the incluster config is used for the API
        # client, but the context *name* still resolves per-cluster resource
        # lists — deploy stamps it into ROBOVAST_KUBE_CONTEXT for the in-pod driver.
        self.kube_context = kube_context or os.environ.get("ROBOVAST_KUBE_CONTEXT")
        self._config_name = cluster_config_name or os.environ.get(
            "ROBOVAST_CLUSTER_CONFIG_NAME")
        self._config_kwargs = cluster_config_kwargs
        if self._config_kwargs is None:
            raw = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
            self._config_kwargs = json.loads(raw) if raw else {}
        self._image = image  # resolved lazily
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
        if reap_on_start:
            self.reap_orphans()

    # -- version ------------------------------------------------------------

    def version(self) -> VersionInfo:
        v = super().version()
        v.backend = "kubernetes"
        return v

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
                lambda: cfg.resolve_driver_s3_endpoint(self._minio_port_forward_endpoint))
        return cfg

    def _minio_port_forward_endpoint(self) -> str:
        """Return ``http://localhost:<port>`` for the shared MinIO port-forward,
        opening (or re-opening a dead) forward under the lock."""
        from robovast.execution.cluster_execution.bucket_ops import \
            open_minio_port_forward
        with self._pf_lock:
            if self._minio_pf is not None and self._minio_pf.poll() is not None:
                self._minio_pf = None  # forward died; drop it and reopen below
            if self._minio_pf is None:
                self._minio_pf, port = open_minio_port_forward(
                    self.namespace, self.kube_context)
                self._minio_pf_endpoint = f"http://localhost:{port}"
                logger.info("Opened MinIO port-forward for driver S3 at %s",
                            self._minio_pf_endpoint)
            return self._minio_pf_endpoint

    def _resolve_image(self):
        from robovast.common.execution import resolve_controller_image
        return self._image or resolve_controller_image()

    def _load_kube(self):
        from kubernetes import config
        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 - off-cluster: use the selected context
            config.load_kube_config(context=self.kube_context)

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
                          namespace=self.namespace,
                          controller_image=self._resolve_image())

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

        Overrides the local disk lookup: the durable home is the object store, so a
        past campaign (or one lost to a service restart) is explained from the
        ``_execution/outcome.json`` published there.
        """
        outcome = self._read_outcome(campaign_id)
        if outcome is not None:
            return outcome
        return Status(phase=Phase.UNKNOWN, campaign_id=campaign_id)

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

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a running Job's live pod log from byte *offset* onward.

        Finds the Job's pod by the auto-added ``job-name`` label and streams its
        ``robovast`` container log. Live source only: a pod still ``Pending`` has no
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
        try:
            text = core.read_namespaced_pod_log(
                name=pod.metadata.name, namespace=self.namespace, container="robovast")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise KeyError(
                    f"pod for job {job_name!r} is gone (campaign {campaign_id!r})") from e
            raise
        raw = text.encode("utf-8", "replace")
        terminal = bool(pod.status and pod.status.phase in ("Succeeded", "Failed"))
        return LogChunk(text=raw[offset:].decode("utf-8", "replace"),
                        next_offset=len(raw), eof=terminal)

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
        batch wait loop unblocks. Scoped to this campaign (``"Hold"``, not
        ``"HoldAndDrain"``), so other campaigns are not preempted.
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
                pf, self._minio_pf = self._minio_pf, None
                self._minio_pf_endpoint = None
            if pf is not None and pf.poll() is None:
                pf.terminate()
                try:
                    pf.wait(timeout=5)
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pf.kill()

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

    # -- data / results -----------------------------------------------------

    def _data_dir(self, campaign_id: str):
        """Data-query campaign dir: pull it from the object store (the durable home)
        into a local cache, so ``describe_campaign_data``/``query_campaign_data_sql``
        (inherited from :class:`LocalTransport`) read the fetched ``data.db``."""
        return self.fetch_campaign(campaign_id)

    def run_postprocessing(self, request) -> ActionResult:
        """(Re)run analysis postprocessing for a cluster campaign.

        Overrides :class:`LocalTransport`'s in-process implementation, which cannot
        work here: this pod has no ROS runtime. Instead the rosbag→CSV step runs as a
        Job in the campaign's own execution image and the ``data.db`` step runs here
        (pure Python) — the same two stages the campaign loop chains, via one shared
        implementation.

        Backs the web "Run postprocessing" button, the MCP ``run_postprocessing``
        tool, and the CLI for cluster campaigns.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        from robovast.execution.cluster_execution.postprocess_job import \
            postprocess_campaign

        campaign_root = self.fetch_campaign(request.campaign_id, force=True)
        cfg = self._cluster_config()
        ok, message = postprocess_campaign(
            cfg, request.campaign_id, str(campaign_root), self.namespace,
            self._resolve_image(), force=request.force, skip=list(request.skip or []))
        if not ok:
            return ActionResult(ok=False, message=message)
        # Publish the refreshed derived data back to the durable home.
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, request.campaign_id)
        storage = in_pod_storage.storage_client_for(cfg)
        n = storage.upload_dir(str(Path(campaign_root) / "_execution"),
                               bucket, f"{prefix}_execution")
        return ActionResult(ok=True, message=f"{message}; published {n} file(s)")

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
