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
        # Indexed in the object store: a cluster campaign whose local scratch is gone, so
        # neither the map nor the on-disk marker can answer. Falling through to the local
        # default would route it to the lane that cannot read its records, and it would
        # report `unknown` with no counts. Free — the same cached listing the cluster
        # lane's discovery already uses.
        if campaign_id in self._cluster._durable_campaign_ids():  # noqa: SLF001
            return _CLUSTER
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
        # No results root: this serve holds *both* lanes, so the local tree would be
        # the right answer for a local campaign and simply absent for a cluster one —
        # a path that is correct per campaign cannot be advertised per service.
        v.results_root = None
        v.sources_root = None
        return v

    def resource_usage(self, backend: Optional[str] = None) -> ResourceUsage:
        lane = backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            return self._cluster.resource_usage()
        return LocalTransport.resource_usage(self)

    # -- container exec -----------------------------------------------------

    def exec_in_container(self, request):
        """Run the check on the lane the caller named, or the default one.

        ``LocalTransport`` drops ``backend`` (``del backend # a multi-backend one
        overrides``) because it has one lane. Here it selects, and it must: without this
        override ``backend="cluster"`` was accepted, validated and transported, then
        silently answered by Docker on the serve host — the wrong image, the wrong
        kernel, and no sign of it in the result.
        """
        lane = request.backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            return self._cluster.exec_in_container(request)
        return LocalTransport.exec_in_container(self, request)

    def resolve_image(self, request):
        """Resolve on the lane the caller named, or the default one — same reason
        ``exec_in_container`` overrides: today ``plan_containers``/``resolve_robovast_image``
        do not read ``backend`` and would answer identically either way, but dispatching
        explicitly means this stays correct if a lane's build registry ever diverges,
        rather than silently answering from whichever lane ``self`` happens to be.
        """
        lane = request.backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            return self._cluster.resolve_image(request)
        return LocalTransport.resolve_image(self, request)

    def describe_world(self, workspace_id: str, path: str = "", targets: str = "",
                       entities: bool = False, backend: str = ""):
        """Ask the simulator on the lane the caller named, or the default one.

        Same reason ``exec_in_container`` overrides: the query runs a container, so without this
        a caller asking the cluster would be answered by Docker on the serve host — a different
        image cache, and on a cluster-only deployment no Docker at all — with nothing in the
        reply to say so.
        """
        lane = backend or self._default_backend()
        if lane not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {lane!r}; use 'local' or 'cluster'")
        if lane == _CLUSTER:
            return self._cluster.describe_world(workspace_id, path, targets, entities)
        return LocalTransport.describe_world(self, workspace_id, path, targets, entities)

    def stop_exec_container(self, backend: Optional[str] = None):
        """Stop the held container on the named lane, or on **both** when none is named.

        Both, rather than the default lane, because each lane holds its own container and
        a caller saying "stop the container" wants the resource freed — one left running
        on the other lane would sit there until its deadline with nothing pointing at it.
        """
        if backend and backend not in (_LOCAL, _CLUSTER):
            raise ValueError(f"unknown backend {backend!r}; use 'local' or 'cluster'")
        if backend == _CLUSTER:
            return self._cluster.stop_exec_container()
        if backend == _LOCAL:
            return LocalTransport.stop_exec_container(self)
        local = LocalTransport.stop_exec_container(self)
        cluster = self._cluster.stop_exec_container()
        if cluster.stopped and not local.stopped:
            return cluster
        return local

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

    def retrigger_campaign(self, campaign_id: str) -> CampaignRef:
        """Relaunch on the lane that ran the source, and record the new campaign's lane.

        Routed by the *source* id like every other per-campaign call, rather than by the
        replayed request's ``backend``: the source's images are pinned refs, and a local built
        ref means nothing to the cluster (nor a registry digest to a Docker host that never
        pulled it). Then the new id is registered exactly as ``create_campaign`` does — the
        retrigger creates a campaign this router now owns.
        """
        lane = self._lane_for(campaign_id)
        if lane == _CLUSTER:
            ref = self._cluster.retrigger_campaign(campaign_id)
        else:
            ref = LocalTransport.retrigger_campaign(self, campaign_id)
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

    def _workspaces_in_use(self) -> dict[str, list[str]]:
        """Both lanes' live campaigns, keyed by the workspace each reads from.

        The lanes keep separate registries, so the inherited single-lane answer would
        report a workspace as free while a cluster campaign was still reading it —
        and this answer exists precisely to stop a push landing on one that is busy.
        A gap here fails open, which is the one way it must not fail.
        """
        merged = LocalTransport._workspaces_in_use(self)  # noqa: SLF001 - this lane
        for workspace_id, ids in self._cluster._workspaces_in_use().items():  # noqa: SLF001
            merged.setdefault(workspace_id, []).extend(ids)
        return merged

    def _durable_campaign_ids(self) -> set[str]:
        """The cluster lane's stored campaigns, so the inherited listing includes them.

        The sibling of :meth:`_extra_live_ids`, for the other thing the local lane cannot
        see: a *finished* cluster campaign whose home is the object store. Off-cluster both
        lanes share one results dir, so the disk scan usually covers it — until that scratch
        is cleaned, after which the object store's index is the only record it exists.
        """
        return self._cluster._durable_campaign_ids()  # noqa: SLF001

    def _started_at_for(self, cid: str) -> Optional[str]:
        """The cluster lane's launch times too — the counterpart of the two id hooks.

        Those put a cluster campaign into the listing before (``_extra_live_ids``) and
        after (``_durable_campaign_ids``) its results directory exists; the inherited
        helper only knows the *local* registry and disk, so such a campaign would have no
        start time and sort last — precisely the campaign a caller is looking for. The
        cluster lane answers for both cases, from its registry and from the index.
        """
        started = LocalTransport._started_at_for(self, cid)
        if started is not None:
            return started
        return self._cluster._started_at_for(cid)  # noqa: SLF001

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        """Both lanes scan the same results dir, so local's list gives the correct
        order + pagination; re-summarize the cluster-owned rows through the cluster
        lane (its ``postprocessed``/live status reads the object store).

        The lane is stamped onto each row: it is resolved here anyway, and it is the one
        fact about a campaign in *this* service that its own results cannot state — two
        campaigns sitting in the same results dir, one run by Docker on the serve host and
        one by a Kubernetes Job, are otherwise indistinguishable to every reader.
        """
        base = LocalTransport.list_campaigns(self, request)
        rows = []
        for c in base.campaigns:
            lane = self._lane_for(c.campaign_id)
            row = self._cluster._summary_for(c.campaign_id) if lane == _CLUSTER else c  # noqa: SLF001
            row.backend = lane
            rows.append(row)
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

    def campaign_scene_status(self, campaign_id: str, config_name: str, run_id: str):
        # Routed, like data-status: what geometry costs is the per-lane difference (a local docker run
        # against an aux pod, an image id against a registry digest), and a request that landed on the
        # wrong lane would not find the campaign at all. `exec_in_container` documents a `backend` field
        # this class never routes -- this must not inherit that.
        return self._route(campaign_id, "campaign_scene_status", campaign_id, config_name, run_id)

    def run_campaign_scene(self, campaign_id: str, config_name: str, run_id: str):
        return self._route(campaign_id, "run_campaign_scene", campaign_id, config_name, run_id)

    def resolve_campaign_scene_asset(self, campaign_id: str, path: str):
        return self._route(campaign_id, "resolve_campaign_scene_asset", campaign_id, path)

    def campaign_screenshot(self, campaign_id: str, config_name: str, run_id: str, **kwargs):
        return self._route(campaign_id, "campaign_screenshot", campaign_id, config_name,
                           run_id, **kwargs)

    def campaign_data_status(self, campaign_id: str):
        # Routed, not answered here: whether a query transfers anything is exactly the
        # per-lane difference — a local-lane campaign in this service fetches nothing.
        return self._route(campaign_id, "campaign_data_status", campaign_id)

    def query_campaign_data_sql(self, campaign_id: str, sql: str, max_rows: int = 500,
                                extra_campaign_ids: Optional[list] = None):
        return self._route(campaign_id, "query_campaign_data_sql", campaign_id, sql,
                           max_rows, extra_campaign_ids)

    def list_campaign_plots(self, campaign_id: str):
        return self._route(campaign_id, "list_campaign_plots", campaign_id)

    def list_campaign_panels(self, campaign_id: str):
        return self._route(campaign_id, "list_campaign_panels", campaign_id)

    # -- files --------------------------------------------------------------
    # ``/results`` is per-campaign, so it routes by the campaign in the address.
    # ``/sources`` is one workspace store shared by both lanes, so it never routes —
    # the inherited implementation is the only one.

    def _route_address(self, address: str, method: str, *args, **kwargs):
        from robovast.common import file_address
        namespace, owner, _ = file_address.parse_address(address)
        if namespace == file_address.RESULTS:
            return self._route(owner, method, *args, **kwargs)
        return getattr(LocalTransport, method)(self, *args, **kwargs)

    def list_files(self, address: str, recursive: bool = False, detail: bool = False,
                   offset: int = 0, limit: int = 100):
        return self._route_address(address, "list_files", address, recursive, detail,
                                   offset, limit)

    def read_file(self, address: str, lines: int = 200, offset: int = 0):
        return self._route_address(address, "read_file", address, lines, offset)

    def read_file_bytes(self, address: str):
        return self._route_address(address, "read_file_bytes", address)

    def local_file(self, address: str):
        # Routed like its neighbours, and for a sharper reason: unrouted, a *cluster*
        # campaign's binary read was answered by the local transport, which resolves the
        # address against the local results tree — a different campaign's file if the id
        # existed on both lanes, and a 404 otherwise. The HTTP layer cannot catch that for
        # us: every transport here has this method, so it has nothing to test for.
        return self._route_address(address, "local_file", address)

    def list_campaign_visualizations(self, campaign_id: str):
        return self._route(campaign_id, "list_campaign_visualizations", campaign_id)

    def render_campaign_notebook(self, campaign_id: str, workload: str, level: str,
                                 config_name: str = "", run_id: Optional[int] = None,
                                 theme: str = "light", batch: Optional[int] = None):
        return self._route(campaign_id, "render_campaign_notebook", campaign_id,
                           workload, level, config_name, run_id, theme, batch)

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """Wind down both lanes (local worker threads + cluster Job teardown)."""
        try:
            LocalTransport.shutdown(self)
        finally:
            self._cluster.shutdown()
