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

Runs inside the ``robovast-service`` Deployment. It reuses
:class:`~robovast.service.client.LocalTransport` for the workspace / file /
read ops (the service pod owns the workspace store + the object store), and
overrides the campaign lifecycle to launch **one controller pod per campaign**
via the kubernetes API — replacing the host's ``kubectl``-driven
``controller_launcher`` with an object-store hand-off:

1. stage the workspace's project into the object store;
2. create a controller pod whose entrypoint is
   :mod:`robovast.execution.cluster_bootstrap` (downloads the project, execs the
   controller);
3. monitor / stop the campaign through the controller's existing ``/status`` +
   ``/command`` channel — reached **directly in-cluster** (pod IP), no
   port-forward.

The per-campaign controller (batch/search loop, Job creation, result upload)
is unchanged; the service only takes over the host's launch/monitor role.
"""

import json
import logging
import os
from pathlib import Path

from robovast.execution.control_server import Status
from robovast.service.client import LocalTransport
from robovast.service.interface import (ActionResult, CampaignRef,
                                        CampaignSummary, CreateCampaignRequest,
                                        ListCampaignsRequest,
                                        ListCampaignsResponse, VersionInfo)

logger = logging.getLogger(__name__)

CONTROLLER_LABEL = "app=robovast-controller"
CONTROL_PORT = 8099
CONTROLLER_SERVICE_ACCOUNT = "robovast-controller"


class ClusterService(LocalTransport):
    """Interface implementation that launches campaigns as controller pods."""

    def __init__(self, namespace=None, cluster_config_name=None,
                 cluster_config_kwargs=None, image=None, store=None):
        super().__init__(store=store)
        self.namespace = namespace or os.environ.get("ROBOVAST_NAMESPACE", "default")
        self._config_name = cluster_config_name or os.environ.get(
            "ROBOVAST_CLUSTER_CONFIG_NAME")
        self._config_kwargs = cluster_config_kwargs
        if self._config_kwargs is None:
            raw = os.environ.get("ROBOVAST_CLUSTER_CONFIG_KWARGS")
            self._config_kwargs = json.loads(raw) if raw else {}
        self._image = image  # resolved lazily

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
        except Exception:  # noqa: BLE001 - dev/local fallback
            config.load_kube_config()
        return client.CoreV1Api()

    def _staging_location(self, campaign_id: str):
        """``(bucket, prefix)`` for the staged project inputs of *campaign_id*.

        ``campaign_storage_location`` returns an **empty** prefix for a per-campaign
        MinIO bucket and a ``<campaign>/`` prefix for a shared (GCS) bucket, so join
        carefully — a leading slash would make upload/download keys disagree.
        """
        from robovast.execution.cluster_execution import in_pod_storage
        bucket, prefix = in_pod_storage.campaign_storage_location(
            self._cluster_config(), campaign_id)
        prefix = prefix.rstrip("/")
        staging = f"{prefix}/_staging_inputs" if prefix else "_staging_inputs"
        return bucket, staging

    def _stage_project(self, campaign_id: str, project_dir) -> tuple[str, str]:
        from robovast.execution.cluster_execution import in_pod_storage
        cfg = self._cluster_config()
        bucket, prefix = self._staging_location(campaign_id)
        storage = in_pod_storage.storage_client_for(cfg)
        n = storage.upload_dir(str(project_dir), bucket, prefix)
        logger.info("Staged %d project file(s) for %s to %s/%s",
                    n, campaign_id, bucket, prefix)
        return bucket, prefix

    def _controller_pod_manifest(self, campaign_id, bucket, prefix, request):
        from robovast.execution.cluster_execution.cluster_execution import \
            _label_safe_campaign
        env = [
            {"name": "ROBOVAST_CLUSTER_CONFIG_NAME", "value": self._config_name},
            {"name": "ROBOVAST_CLUSTER_CONFIG_KWARGS",
             "value": json.dumps(self._config_kwargs or {})},
            {"name": "ROBOVAST_NAMESPACE", "value": self.namespace},
            {"name": "ROBOVAST_CAMPAIGN_ID", "value": campaign_id},
            {"name": "ROBOVAST_STAGING_BUCKET", "value": bucket},
            {"name": "ROBOVAST_STAGING_PREFIX", "value": prefix},
            # Results are delivered via the object store (the service pulls them);
            # no external share is required (see plan 0.8).
            {"name": "ROBOVAST_SKIP_SHARE", "value": "1"},
        ]
        if request.runs and request.runs > 0:
            env.append({"name": "ROBOVAST_RUNS", "value": str(request.runs)})
        if request.config_filter:
            env.append({"name": "ROBOVAST_CONFIG_FILTER", "value": request.config_filter})
        if request.config_path:
            # Which .vast to run when the staged project holds several (else the
            # controller auto-picks the sole one). Workspace-relative.
            env.append({"name": "ROBOVAST_CONFIG_PATH", "value": request.config_path})
        if request.postprocess:
            # Chain analysis postprocessing (rosbags→CSV Job + data.db) once the runs
            # finish — the controller does it in-pod, before its finalize upload, so
            # data.db ships with the campaign (parity with the local backend).
            env.append({"name": "ROBOVAST_POSTPROCESS", "value": "1"})
            env.append({"name": "ROBOVAST_CONTROLLER_IMAGE", "value": self._resolve_image()})
        pod_name = f"robovast-controller-{_label_safe_campaign(campaign_id)}"
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.namespace,
                "labels": {"app": "robovast-controller",
                           "campaign-id": _label_safe_campaign(campaign_id)},
            },
            "spec": {
                "restartPolicy": "Never",
                "serviceAccountName": CONTROLLER_SERVICE_ACCOUNT,
                "containers": [{
                    "name": "controller",
                    "image": self._resolve_image(),
                    "imagePullPolicy": "Always",
                    "command": ["python", "-m", "robovast.execution.cluster_bootstrap"],
                    "ports": [{"containerPort": CONTROL_PORT, "name": "control"}],
                    "env": env,
                }],
            },
        }

    # -- campaign lifecycle -------------------------------------------------

    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        from kubernetes.client.rest import ApiException
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.execution.controller import campaign_id_for

        from robovast.common.config_plugins import ensure_workspace_plugins

        project = self._project_for_workspace(request.workspace_id, request.config_path)
        project_dir = self.store.registry.project_dir(request.workspace_id)
        campaign_config = validate_config(load_config(project.config_path))
        campaign_id = campaign_id_for(campaign_config)

        # Reject a config_filter typo *before* launching a controller pod (whose
        # crash would otherwise only show up in its log). Same check, same message
        # as the local path and the legacy `vast exec cluster run` pre-flight.
        if request.config_filter and campaign_config.search is None:
            from robovast.service.client import _validate_config_filter
            _validate_config_filter(project.config_path, request.config_filter)

        # Install the .vast's declared variation plugins into the workspace's
        # .robovast_plugins/ *here* (the service can reach the source and holds any
        # git credentials), so the populated dir is staged into the controller pod
        # with the project below — the pod imports it off sys.path, never cloning.
        # Synchronous on purpose: a resolution failure surfaces to the caller (MCP)
        # now, not later as a confusing "Unknown variation class" in pod logs. Install
        # next to the .vast (its dir is the vast_dir the pod's composition uses).
        # force=True: materialize every declared plugin into .robovast_plugins/ so the
        # bare controller pod gets them, regardless of what the service venv has.
        ensure_workspace_plugins(
            os.path.dirname(project.config_path), campaign_config.plugins, force=True)

        bucket, prefix = self._stage_project(campaign_id, project_dir)
        manifest = self._controller_pod_manifest(campaign_id, bucket, prefix, request)
        core = self._k8s()
        try:
            core.create_namespaced_pod(self.namespace, manifest)
        except ApiException as e:
            raise RuntimeError(f"failed to launch controller pod: {e.reason}") from e
        logger.info("Launched controller pod for campaign %s", campaign_id)
        return CampaignRef(campaign_id=campaign_id)

    def list_campaigns(
        self, request: "ListCampaignsRequest | None" = None
    ) -> ListCampaignsResponse:
        """List cluster campaigns from their controller pods (newest first).

        Enumerates ``app=robovast-controller`` pods (running + recently finished,
        which linger) and maps pod phase to a campaign phase. Historical campaigns
        whose pods have been reaped live in the object store; surfacing those is a
        later enhancement.
        """
        request = request or ListCampaignsRequest()
        core = self._k8s()
        pods = core.list_namespaced_pod(
            self.namespace, label_selector=CONTROLLER_LABEL).items
        phase_map = {"Succeeded": "finished", "Failed": "failed",
                     "Pending": "starting", "Running": "running"}
        summaries = []
        for pod in pods:
            cid = (pod.metadata.labels or {}).get("campaign-id", pod.metadata.name)
            summaries.append(CampaignSummary(
                campaign_id=cid,
                phase=phase_map.get(pod.status.phase, "unknown"),
                started_at=(pod.status.start_time.isoformat()
                            if pod.status.start_time else None)))
        summaries.sort(key=lambda s: s.started_at or "", reverse=True)
        total = len(summaries)
        window = summaries[request.offset:request.offset + request.limit]
        return ListCampaignsResponse(campaigns=window, total=total)

    def _controller_pod(self, campaign_id):
        """Return the controller pod object for *campaign_id*, or None."""
        from robovast.execution.cluster_execution.cluster_execution import \
            _label_safe_campaign
        core = self._k8s()
        selector = f"{CONTROLLER_LABEL},campaign-id={_label_safe_campaign(campaign_id)}"
        pods = core.list_namespaced_pod(self.namespace, label_selector=selector).items
        return pods[-1] if pods else None

    def get_status(self, campaign_id: str) -> Status:
        import requests
        pod = self._controller_pod(campaign_id)
        if pod is None:
            return Status(phase="unknown", campaign_id=campaign_id)
        ip = pod.status.pod_ip
        # Reach the controller's control channel directly in-cluster.
        if ip and pod.status.phase == "Running":
            try:
                resp = requests.get(f"http://{ip}:{CONTROL_PORT}/status", timeout=5)
                resp.raise_for_status()
                return Status.model_validate(resp.json())
            except Exception as e:  # noqa: BLE001 - fall back to pod phase
                logger.debug("controller /status unreachable for %s: %s", campaign_id, e)
        phase = {"Succeeded": "finished", "Failed": "failed",
                 "Pending": "starting", "Running": "running"}.get(
                     pod.status.phase, "unknown")
        if phase == "failed":
            # The controller recorded *why* it died to _execution/outcome.json in
            # the object store (its in-pod /status channel is gone with the pod).
            # Surface that reason instead of a bare "failed".
            outcome = self._read_outcome(campaign_id)
            if outcome is not None:
                return outcome
        return Status(phase=phase, campaign_id=campaign_id)

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

    def _data_dir(self, campaign_id: str):
        """Data-query campaign dir: pull it from the object store (the durable home)
        into a local cache, so ``describe_campaign_data``/``query_campaign_data_sql``
        (inherited from :class:`LocalTransport`) read the fetched ``data.db``."""
        return self.fetch_campaign(campaign_id)

    def run_postprocessing(self, request) -> ActionResult:
        """(Re)run analysis postprocessing for a cluster campaign.

        Overrides :class:`LocalTransport`'s in-process implementation, which cannot
        work here: this pod has no local results root and no ROS runtime. Instead the
        rosbag→CSV step runs as a Job in the campaign's own execution image and the
        ``data.db`` step runs here (pure Python) — the same two stages the controller
        chains after a campaign, via one shared implementation.

        Backs the web "Run postprocessing" button, the MCP ``run_postprocessing``
        tool, and the CLI for cluster campaigns.
        """
        from robovast.execution.cluster_execution.postprocess_job import (
            postprocess_campaign)
        from robovast.execution.cluster_execution import in_pod_storage

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

        The object store is the durable home (the controller published the full
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

    def stop(self, campaign_id: str) -> ActionResult:
        import requests
        pod = self._controller_pod(campaign_id)
        if pod is None:
            return ActionResult(ok=False, message=f"no controller pod for {campaign_id}")
        ip = pod.status.pod_ip
        if ip and pod.status.phase == "Running":
            try:
                requests.post(f"http://{ip}:{CONTROL_PORT}/command",
                              json={"name": "stop", "args": {}}, timeout=10)
                return ActionResult(ok=True, message="cooperative stop requested")
            except Exception as e:  # noqa: BLE001
                logger.warning("cooperative stop failed for %s: %s", campaign_id, e)
        # Fall back to deleting the pod.
        self._k8s().delete_namespaced_pod(pod.metadata.name, self.namespace)
        return ActionResult(ok=True, message="controller pod deleted")
