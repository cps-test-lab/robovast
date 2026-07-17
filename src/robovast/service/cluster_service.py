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
from pathlib import Path

from robovast.execution.control_server import Status
from robovast.service.client import LocalTransport
from robovast.service.interface import (ActionResult, CampaignSummary,
                                        ListCampaignsRequest,
                                        ListCampaignsResponse, VersionInfo)

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
        # active context; in-cluster the incluster config is used and this is moot.
        self.kube_context = kube_context
        self._config_name = cluster_config_name or os.environ.get(
            "ROBOVAST_CLUSTER_CONFIG_NAME")
        self._config_kwargs = cluster_config_kwargs
        if self._config_kwargs is None:
            raw = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
            self._config_kwargs = json.loads(raw) if raw else {}
        self._image = image  # resolved lazily
        if reap_on_start:
            self.reap_orphans()

    # -- version ------------------------------------------------------------

    def version(self) -> VersionInfo:
        v = super().version()
        v.backend = "kubernetes"
        return v

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
        return cfg

    def _resolve_image(self):
        from robovast.common.execution import resolve_controller_image
        return self._image or resolve_controller_image()

    def _k8s(self):
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001 - off-cluster: use the selected context
            config.load_kube_config(context=self.kube_context)
        return client.CoreV1Api()

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
                                 kube_context=self.kube_context)

    def _run_options(self, request):
        from robovast.execution.backends import RunOptions
        # postprocess travels in the options (not the process env): one process
        # drives many campaigns, and an env var could not tell them apart.
        return RunOptions(gui=False,
                          postprocess=bool(request.postprocess),
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
        return Status(phase="unknown", campaign_id=campaign_id)

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

    def list_campaigns(
        self, request: "ListCampaignsRequest | None" = None
    ) -> ListCampaignsResponse:
        """List campaigns this service is driving (newest first).

        Live campaigns come from the in-process registry. Historical campaigns live
        in the object store; surfacing those is a later enhancement (the previous
        implementation enumerated controller pods, which no longer exist).
        """
        request = request or ListCampaignsRequest()
        with self._lock:
            entries = list(self._campaigns.values())
        summaries = [
            CampaignSummary(campaign_id=e.campaign_id,
                            phase=e.state.snapshot().phase)
            for e in entries
        ]
        summaries.sort(key=lambda s: s.campaign_id, reverse=True)
        total = len(summaries)
        window = summaries[request.offset:request.offset + request.limit]
        return ListCampaignsResponse(campaigns=window, total=total)

    def stop(self, campaign_id: str) -> ActionResult:
        """Cooperatively stop a campaign this process is driving.

        The driver is in this process, so ``stop`` is a direct state flag rather than
        an HTTP command to a controller pod. The loop ends after the current batch and
        the worker's teardown deletes the aux pod; in-flight scenario Jobs are left to
        finish (``vast exec cluster run-cleanup`` removes them).
        """
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(
                ok=False, message=f"campaign {campaign_id} is not running here")
        entry.state.request_stop()
        return ActionResult(ok=True, message="cooperative stop requested")

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

    def upload_to_share(self, campaign_id: str, overrides=None) -> ActionResult:
        """Upload a finished campaign to the external share. Stateless; repeatable.

        Replaces the old "keep the controller pod parked in ``wait_for_retrigger``
        so the user can retry" dance: the campaign is durable in the object store,
        which ``upload_campaign`` compresses straight from — so a failed upload is
        retried by simply calling this again, with *overrides* carrying any
        corrected (or switched) share credentials. No live process required.
        """
        from robovast.execution.cluster_execution import in_pod_upload
        try:
            provider = in_pod_upload.load_provider_from_env(overrides)
        except Exception as e:  # noqa: BLE001 - a misconfigured share is user error
            return ActionResult(ok=False, message=f"share provider misconfigured: {e}")
        if provider is None:
            return ActionResult(
                ok=False,
                message="no share destination configured (set ROBOVAST_SHARE_TYPE, "
                        "or pass overrides); results remain in the object store")
        try:
            # Pre-flight the credentials before burning time on the compress step.
            in_pod_upload.verify_share_access(provider)
        except Exception as e:  # noqa: BLE001
            return ActionResult(
                ok=False, message=f"share credential check failed: {e}")
        ok = in_pod_upload.upload_campaign(self._cluster_config(), campaign_id, provider)
        if not ok:
            return ActionResult(
                ok=False,
                message=f"upload to {provider.SHARE_TYPE} failed; the campaign is "
                        "safe in the object store — fix the cause and call again")
        return ActionResult(ok=True, message=f"uploaded to {provider.SHARE_TYPE}")

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

        campaign_root = self.fetch_campaign(request.campaign_id)
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

    def fetch_campaign(self, campaign_id: str):
        """Pull a finished campaign from the object store to a local dir; return it.

        The object store is the durable home (the campaign loop published the full
        campaign there via ``finalize_campaign``). The stateless service pulls it
        into ephemeral scratch on demand — to serve a download or re-postprocess.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = in_pod_storage.campaign_storage_location(cfg, campaign_id)
        dest = Path("/tmp") / "robovast-campaigns" / campaign_id  # noqa: S108 - pod scratch
        dest.mkdir(parents=True, exist_ok=True)
        storage = in_pod_storage.storage_client_for(cfg)
        n = storage.download_prefix(bucket, prefix, str(dest))
        logger.info("Fetched campaign %s (%d file(s)) from %s/%s to %s",
                    campaign_id, n, bucket, prefix, dest)
        return dest
