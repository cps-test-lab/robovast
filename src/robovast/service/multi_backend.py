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

"""A single ``vast serve`` exposing **both** a local Docker lane and a cluster
lane, choosing the backend per campaign.

This is the dev-host mode (``vast serve --backend local+cluster``) that lets an
agent pilot a campaign locally and scale the same session to the cluster without
re-pointing serve. It exists **only** off-cluster (a laptop / build node with both
Docker and kubeconfig); the deployed in-cluster service stays single-backend.

Design — spend no new invariant on the deployed path:

* :class:`ClusterService` already subclasses :class:`LocalTransport`; the two lanes
  differ only in the execution-specific methods the cluster class overrides, and
  share the same results dir/store for everything else. So this router **subclasses
  LocalTransport** — ``self`` *is* the local lane and the whole backend-agnostic
  surface (workspaces, project files, validation, config schema, …) is inherited and
  correct — and holds **one** :class:`ClusterService` for the cluster lane.
* It overrides only what already branches on backend: ``create_campaign`` (which lane
  to launch on) and the per-``campaign_id`` / ``build_id`` methods (which lane owns the
  thing), plus ``version`` / ``resource_usage`` / ``list_campaigns``. A new interface
  method needs attention here only if ``ClusterService`` itself had to specialize it.

Lane resolution (:meth:`_lane_for`): the in-memory map set at create time is
authoritative while this process drives the campaign; a small ``_execution/backend``
marker (written self-healingly) resolves it after a restart; the historical default is
local. ``CreateCampaignRequest.backend`` unset defaults to **cluster** (the scaled
backend; local is the explicit pilot choice).
"""

import logging
import threading
from pathlib import Path
from typing import Optional

from robovast.service.cluster_service import ClusterService
from robovast.service.interface import (ActionResult, BuildImageRequest,
                                        CampaignRef, CleanupDataRequest,
                                        CreateCampaignRequest, ImageBuildRef,
                                        ListCampaignsRequest,
                                        ListCampaignsResponse, ResourceUsage,
                                        VersionInfo)
from robovast.service.local_transport import LocalTransport

logger = logging.getLogger(__name__)

_LOCAL = "local"
_CLUSTER = "cluster"
#: Per-campaign marker file (under the campaign's ``_execution/``) naming the lane
#: that ran it, so lane resolution survives a ``vast serve`` restart.
_BACKEND_MARKER = "backend"


class MultiBackendService(LocalTransport):
    """Route each campaign to a local Docker lane or a cluster lane in one service.

    ``self`` is the local lane (inherited ``LocalTransport``); ``self._cluster`` is the
    cluster lane. Both are constructed against the **same** workspace/results store so
    the shared surface and the results dir are one.
    """

    def __init__(self, cluster: ClusterService, *, store=None, workspace_dir=None):
        super().__init__(store=store, workspace_dir=workspace_dir)
        if cluster.store is not self.store:
            # Both lanes must read/write the one results dir + workspace set; a split
            # store would hide each lane's campaigns from the other's list/status.
            raise ValueError(
                "MultiBackendService requires the cluster lane to share this "
                "service's store (construct ClusterService(store=<shared store>))")
        self._cluster = cluster
        self._lane_map: dict[str, str] = {}
        self._lane_map_lock = threading.Lock()
        self._build_lane_map: dict[str, str] = {}

    # -- lane resolution ----------------------------------------------------

    def _default_backend(self) -> str:
        """The lane a request with no explicit backend runs on: cluster (scaled)."""
        return _CLUSTER

    def _marker_path(self, campaign_id: str) -> Path:
        return self._campaigns_root() / campaign_id / "_execution" / _BACKEND_MARKER

    def _persist_marker(self, campaign_id: str, lane: str) -> None:
        """Best-effort write the lane marker once the campaign dir exists (self-healing).

        The worker thread creates the campaign dir asynchronously, so the marker
        cannot always be written at create time; the in-memory map covers the campaign
        for this process's lifetime and this persists it for the next one.
        """
        path = self._marker_path(campaign_id)
        if path.exists() or not path.parent.exists():
            return
        try:
            path.write_text(lane, encoding="utf-8")
        except OSError as e:
            logger.debug("could not write backend marker for %s: %s", campaign_id, e)

    def _read_marker(self, campaign_id: str) -> Optional[str]:
        try:
            lane = self._marker_path(campaign_id).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return lane if lane in (_LOCAL, _CLUSTER) else None

    def _lane_for(self, campaign_id: str) -> str:
        """Which lane owns ``campaign_id`` — in-memory first, then disk, then local."""
        with self._lane_map_lock:
            lane = self._lane_map.get(campaign_id)
        if lane is not None:
            self._persist_marker(campaign_id, lane)
            return lane
        # Live in one of the lanes this process is driving?
        with self._lock:
            if campaign_id in self._campaigns:
                return _LOCAL
        with self._cluster._lock:  # noqa: SLF001 - sibling lane, one feature
            if campaign_id in self._cluster._campaigns:  # noqa: SLF001
                return _CLUSTER
        marker = self._read_marker(campaign_id)
        if marker is not None:
            return marker
        # No record (a campaign from before this feature, or a bare dir): the
        # historical single-backend default is local.
        return _LOCAL

    def _route(self, campaign_id: str, method: str, *args, **kwargs):
        """Call ``method`` on the lane that owns ``campaign_id``.

        Local runs the inherited ``LocalTransport`` implementation on ``self`` (calling
        ``self.method`` would recurse into this router); cluster delegates to the
        held :class:`ClusterService`.
        """
        if self._lane_for(campaign_id) == _CLUSTER:
            return getattr(self._cluster, method)(*args, **kwargs)
        return getattr(LocalTransport, method)(self, *args, **kwargs)

    def _lane_for_build(self, build_id: str) -> str:
        lane = self._build_lane_map.get(build_id)
        if lane is not None:
            return lane
        # Unknown build (e.g. after a restart): probe the cluster lane, else local.
        # Builds are in-process/ephemeral, so a miss usually means it is already gone.
        return _LOCAL

    def _route_build(self, build_id: str, method: str, *args, **kwargs):
        if self._lane_for_build(build_id) == _CLUSTER:
            return getattr(self._cluster, method)(*args, **kwargs)
        return getattr(LocalTransport, method)(self, *args, **kwargs)

    # -- version / capacity -------------------------------------------------

    def version(self) -> VersionInfo:
        v = super().version()
        v.backends = [_LOCAL, _CLUSTER]
        # The cluster-lane facts come from the lane that owns them. Reported even
        # though this serve is local-first: a campaign with no explicit backend
        # defaults to *cluster* (see _default_backend), so "which cluster?" is a
        # question about the default lane, not an optional detail.
        cv = self._cluster.version()
        v.kube_context = cv.kube_context
        v.kube_context_source = cv.kube_context_source
        v.namespace = cv.namespace
        v.in_pod = cv.in_pod
        v.api_server = cv.api_server
        return v

    def resource_usage(self, backend: Optional[str] = None) -> ResourceUsage:
        lane = backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            return self._cluster.resource_usage()
        return LocalTransport.resource_usage(self)

    # -- campaign lifecycle -------------------------------------------------

    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        lane = request.backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            ref = self._cluster.create_campaign(request)
        else:
            ref = LocalTransport.create_campaign(self, request)
        with self._lane_map_lock:
            self._lane_map[ref.campaign_id] = lane
        self._persist_marker(ref.campaign_id, lane)
        return ref

    def get_status(self, campaign_id: str):
        return self._route(campaign_id, "get_status", campaign_id)

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        return self._route(campaign_id, "get_campaign_logs", campaign_id, offset)

    def list_jobs(self, campaign_id: str):
        return self._route(campaign_id, "list_jobs", campaign_id)

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0):
        return self._route(campaign_id, "get_job_log", campaign_id, job_name, offset)

    def stop(self, campaign_id: str) -> ActionResult:
        return self._route(campaign_id, "stop", campaign_id)

    def delete_campaign(self, campaign_id: str) -> ActionResult:
        return self._route(campaign_id, "delete_campaign", campaign_id)

    def _extra_live_ids(self) -> set[str]:
        """The cluster lane's live registry, so the inherited listing includes it.

        ``list_campaigns`` starts from the *local* lane's "disk ∪ in-memory" id set;
        the cluster lane keeps its own registry, so without this a cluster campaign
        is missing from every listing until its results directory appears — the id is
        registered the instant the campaign is accepted, but unlistable for as long
        as the lane's pre-flight (project push, image build) takes. A start whose
        response was lost then looks, to every read path, like a campaign that was
        never created.
        """
        with self._cluster._lock:  # noqa: SLF001 - sibling lane, one feature
            return set(self._cluster._campaigns)  # noqa: SLF001

    def _started_at_for(self, cid: str) -> Optional[str]:
        """The cluster lane's launch times too — the counterpart of _extra_live_ids.

        That method puts a pre-flight cluster campaign into the listing before its
        results directory exists; the inherited helper only knows the *local* registry,
        so such a campaign would have no start time and sort last — precisely the
        just-launched campaign that belongs first.
        """
        started = LocalTransport._started_at_for(self, cid)
        if started is not None:
            return started
        with self._cluster._lock:  # noqa: SLF001 - sibling lane, one feature
            entry = self._cluster._campaigns.get(cid)  # noqa: SLF001
        return entry.created_at if entry is not None else None

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        """Both lanes scan the same results dir, so local's list gives the correct
        order + pagination; re-summarize the cluster-owned rows through the cluster
        lane (its ``postprocessed``/live status reads the object store)."""
        base = LocalTransport.list_campaigns(self, request)
        rows = []
        for c in base.campaigns:
            if self._lane_for(c.campaign_id) == _CLUSTER:
                rows.append(self._cluster._summary_for(c.campaign_id))  # noqa: SLF001
            else:
                rows.append(c)
        return ListCampaignsResponse(campaigns=rows, total=base.total)

    def cleanup_campaign_data(self, request: CleanupDataRequest) -> ActionResult:
        """A named campaign routes to its lane; a bulk sweep (no id) fans out to both."""
        if request.campaign_id:
            return self._route(request.campaign_id, "cleanup_campaign_data", request)
        local = LocalTransport.cleanup_campaign_data(self, request)
        cluster = self._cluster.cleanup_campaign_data(request)
        return ActionResult(
            ok=local.ok and cluster.ok,
            message=f"local: {local.message} | cluster: {cluster.message}")

    # -- image builds -------------------------------------------------------

    def build_image(self, request: BuildImageRequest) -> ImageBuildRef:
        lane = request.backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            ref = self._cluster.build_image(request)
        else:
            ref = LocalTransport.build_image(self, request)
        self._build_lane_map[ref.build_id] = lane
        return ref

    def get_image_build_status(self, build_id: str):
        return self._route_build(build_id, "get_image_build_status", build_id)

    def get_image_build_log(self, build_id: str, offset: int = 0):
        return self._route_build(build_id, "get_image_build_log", build_id, offset)

    # -- postprocessing / panels (per-campaign; run on the owning lane) -----

    def get_postprocessing(self, campaign_id: str):
        return self._route(campaign_id, "get_postprocessing", campaign_id)

    def update_postprocessing(self, request):
        return self._route(request.campaign_id, "update_postprocessing", request)

    def get_postprocessing_source(self, campaign_id: str):
        return self._route(campaign_id, "get_postprocessing_source", campaign_id)

    def update_postprocessing_source(self, request):
        return self._route(request.campaign_id, "update_postprocessing_source", request)

    def run_postprocessing(self, request) -> ActionResult:
        return self._route(request.campaign_id, "run_postprocessing", request)

    def run_share(self, request) -> ActionResult:
        return self._route(request.campaign_id, "run_share", request)

    def get_panels_source(self, campaign_id: str):
        return self._route(campaign_id, "get_panels_source", campaign_id)

    def update_panels_source(self, request):
        return self._route(request.campaign_id, "update_panels_source", request)

    # -- results data query (per-campaign; the dir is resolved per lane) ----

    def describe_campaign_data(self, campaign_id: str):
        return self._route(campaign_id, "describe_campaign_data", campaign_id)

    def query_campaign_data_sql(self, campaign_id: str, sql: str, max_rows: int = 500,
                                extra_campaign_ids: Optional[list] = None):
        return self._route(campaign_id, "query_campaign_data_sql", campaign_id, sql,
                           max_rows, extra_campaign_ids)

    def list_campaign_plots(self, campaign_id: str):
        return self._route(campaign_id, "list_campaign_plots", campaign_id)

    def list_campaign_panels(self, campaign_id: str):
        return self._route(campaign_id, "list_campaign_panels", campaign_id)

    def get_run_file(self, campaign_id: str, config_name: str, run_id: int, path: str):
        return self._route(campaign_id, "get_run_file", campaign_id, config_name,
                           run_id, path)

    def list_campaign_visualizations(self, campaign_id: str):
        return self._route(campaign_id, "list_campaign_visualizations", campaign_id)

    def render_campaign_notebook(self, campaign_id: str, workload: str, level: str,
                                 config_name: str = "", run_id: Optional[int] = None,
                                 theme: str = "light"):
        return self._route(campaign_id, "render_campaign_notebook", campaign_id,
                           workload, level, config_name, run_id, theme)

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """Wind down both lanes (local worker threads + cluster Job teardown)."""
        try:
            LocalTransport.shutdown(self)
        finally:
            self._cluster.shutdown()
