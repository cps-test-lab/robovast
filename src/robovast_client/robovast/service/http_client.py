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

"""``HTTPTransport`` + the ``RobovastClient`` factory.

:class:`HTTPTransport` talks to a running ``robovast-service`` (a local
``vast serve``, a remote VM, or an in-cluster deployment) over the HTTP contract
in :class:`robovast.service.interface.Routes`. :func:`RobovastClient` is the
transport-agnostic factory: a URL selects the HTTP transport, empty selects the
in-process :class:`~robovast.service.local_transport.LocalTransport`.

Split out of the former single ``client`` module; ``client`` now re-exports both
so existing imports keep working.
"""

import logging
from typing import Optional

from robovast.client import file_address
from robovast.client.app_version import running_version
from robovast.client.status import Status
from robovast.service.auth import USER_HEADER
from robovast.service.interface import (ActionResult, BuildImageRequest, CampaignRef,
                                        CreateCampaignRequest, CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest, FileListing,
                                        FileMeta, FileText, ImageBuildRef, ImageBuildStatus,
                                        ImportCampaignRequest,
                                        ListCampaignsRequest, ListCampaignsResponse,
                                        JobState, ListJobsResponse, ListWorkspacesResponse,
                                        LogChunk,
                                        PreviewResponse, ResourceUsage, RetriggerReport,
                                        RobovastInterface, Routes, SearchHistory,
                                        ServiceError, UploadGrant,
                                        UpgradeInfo,
                                        ValidationReport, WorkOrder,
                                        VariationTypesResponse, VersionInfo, WorkspaceInfo,
                                        WorldDescription, WriteFileRequest)

logger = logging.getLogger(__name__)


class HTTPTransport(RobovastInterface):
    """Talks to a running ``robovast-service`` over the :class:`Routes` contract.

    The base URL is either the service on this machine (``http://127.0.0.1:<port>``)
    or the deployed one behind its Ingress (``https://robovast.<domain>``) — see
    ``robovast.client.service_target`` for how a command chooses.

    Every request goes through one :class:`requests.Session`, which is what carries the
    credentials to the *eight* places that talk HTTP here: the four verb helpers and the
    four routes that build their own request (byte reads, the streamed CSV, screenshots,
    the rendered notebook). Adding a header per call site is how one of them ends up
    forgotten and 401s only for the one user who tried that feature. The session also
    brings connection pooling, which this class never had.
    """

    def __init__(self, base_url: str, timeout: float = 30.0,
                 token: str = "", user: str = ""):
        import requests
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        if user:
            # Self-declared and unverified; the service records it as such.
            self.session.headers[USER_HEADER] = user

    @staticmethod
    def raise_for_status(resp) -> None:
        """Raise :class:`ServiceError` carrying the service's own message.

        ``requests``' own ``raise_for_status`` reports the status line and the URL and
        throws the body away — which is where every actionable refusal this service
        writes was being lost. One helper, so no call site can forget; used for the
        streaming reads too, which do not go through :meth:`_get`.
        """
        if resp.ok:
            return
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail") or ""
                # FastAPI's request-validation errors are a list of per-field dicts, not
                # a string; render them rather than printing a repr at the caller.
                if isinstance(detail, list):
                    detail = "; ".join(
                        f"{'.'.join(str(p) for p in d.get('loc', []))}: {d.get('msg', '')}"
                        for d in detail if isinstance(d, dict))
        except ValueError:
            detail = (resp.text or "").strip()[:500]
        raise ServiceError(resp.status_code,
                           detail or f"{resp.status_code} {resp.reason}",
                           resp.url)

    # First arg is the URL *route*; **params are query params — named `route` (not
    # `path`) so an endpoint whose query param is itself `path` (workspace file
    # read/delete) doesn't collide with this positional. `timeout` is keyword-only for the
    # same reason and is likewise reserved: no route takes a `timeout` query param.
    def _get(self, route: str, *, timeout: "float | None" = None, **params):
        resp = self.session.get(f"{self.base_url}{route}", params=params or None,
                            timeout=timeout or self.timeout)
        self.raise_for_status(resp)
        return resp.json()

    def _post(self, route: str, json=None, *, timeout: "float | None" = None, **params):
        # ``params`` for the routes whose argument cannot be a path segment (a job name
        # contains '/'), the same way ``_delete`` takes them. ``None`` values are dropped
        # so an omitted optional argument is absent rather than the string "None".
        query = {k: v for k, v in params.items() if v is not None}
        resp = self.session.post(f"{self.base_url}{route}", json=json,
                             params=query or None,
                             timeout=timeout or self.timeout)
        self.raise_for_status(resp)
        return resp.json()

    def _put(self, route: str, json=None):
        resp = self.session.put(f"{self.base_url}{route}", json=json,
                            timeout=self.timeout)
        self.raise_for_status(resp)
        return resp.json()

    def _delete(self, route: str, **params):
        resp = self.session.delete(f"{self.base_url}{route}", params=params or None,
                               timeout=self.timeout)
        self.raise_for_status(resp)
        return resp.json()

    def version(self) -> VersionInfo:
        return VersionInfo.model_validate(self._get(Routes.VERSION))

    def resource_usage(self) -> ResourceUsage:
        return ResourceUsage.model_validate(self._get(Routes.USAGE))

    def upgrade_info(self) -> UpgradeInfo:
        return UpgradeInfo.model_validate(self._get(Routes.ADMIN_UPGRADE))

    def upgrade_service(self, force: bool = False) -> ActionResult:
        return ActionResult.model_validate(self._post(Routes.ADMIN_UPGRADE, force=force))

    def get_service_log(self, offset: int = 0) -> LogChunk:
        """This service's own recent log (see ``Routes.ADMIN_LOG``).

        Not on :class:`RobovastInterface`, so it is not an abstract method the other
        transports must answer: the log describes the *serving process*, and an in-process
        ``LocalTransport`` caller is already inside the process whose stderr it is. This is
        the wire client for a route that only a remote caller needs.
        """
        return LogChunk.model_validate(self._get(Routes.ADMIN_LOG, offset=offset))

    def check_compatibility(self) -> dict:
        """Compare this client's robovast version with the service's (handshake).

        Returns ``{compatible, client_version, service_version, backend}`` and
        logs a warning on mismatch so a stale service surfaces instead of failing
        obscurely. Best-effort: an unreachable service yields ``compatible=None``.
        """
        client_v = running_version()
        try:
            info = self.version()
        except Exception as e:  # noqa: BLE001 - unreachable service
            logger.warning("could not reach service for version check: %s", e)
            return {"compatible": None, "client_version": client_v,
                    "service_version": None, "backend": None}
        compatible = info.robovast_version == client_v
        if not compatible:
            logger.warning(
                "robovast version mismatch: client %s vs service %s. Upgrade the "
                "in-cluster service with 'vast cluster cleanup' then "
                "'vast cluster setup <cluster-config>' (or "
                "'vast cluster setup --force <cluster-config>').",
                client_v, info.robovast_version)
        return {"compatible": compatible, "client_version": client_v,
                "service_version": info.robovast_version, "backend": info.backend}

    # -- workspaces ---------------------------------------------------------

    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceInfo:
        return WorkspaceInfo.model_validate(
            self._post(Routes.WORKSPACES, json=request.model_dump()))

    def list_workspaces(self) -> ListWorkspacesResponse:
        return ListWorkspacesResponse.model_validate(self._get(Routes.WORKSPACES))

    def get_workspace(self, workspace_id: str) -> WorkspaceInfo:
        return WorkspaceInfo.model_validate(self._get(Routes.workspace(workspace_id)))

    def delete_workspace(self, workspace_id: str) -> ActionResult:
        return ActionResult.model_validate(self._delete(Routes.workspace(workspace_id)))

    # -- files --------------------------------------------------------------
    # The address *is* the route: no URL is built here beyond prepending the base.

    def list_files(self, address: str, recursive: bool = False, detail: bool = False,
                   offset: int = 0, limit: int = 100) -> FileListing:
        # Normalized to directory form: the route reads the trailing slash, and this
        # call means "list" regardless of how the caller spelled the address.
        return FileListing.model_validate(self._get(
            Routes.file(file_address.as_directory(address)),
            recursive=int(recursive), detail=int(detail),
            offset=offset, limit=limit))

    def read_file(self, address: str, lines: int = 200, offset: int = 0) -> FileText:
        return FileText.model_validate(self._get(
            Routes.file(address), **{"as": "text", "lines": lines, "offset": offset}))

    def read_file_bytes(self, address: str) -> bytes:
        resp = self.session.get(f"{self.base_url}{Routes.file(address)}",
                            timeout=self.timeout)
        self.raise_for_status(resp)
        return resp.content

    def write_file(self, request: WriteFileRequest) -> FileMeta:
        return FileMeta.model_validate(self._put(
            Routes.file(request.address), json={"content": request.content}))

    def edit_file(self, request: EditFileRequest) -> FileMeta:
        return FileMeta.model_validate(self._post(
            Routes.file(request.address),
            json={"old_string": request.old_string, "new_string": request.new_string}))

    def delete_file(self, address: str) -> ActionResult:
        return ActionResult.model_validate(self._delete(Routes.file(address)))

    def create_upload(self, request: CreateUploadRequest) -> UploadGrant:
        grant = UploadGrant.model_validate(self._post(
            Routes.UPLOADS, json=request.model_dump()))
        # The service returns a relative path (it doesn't know its external base);
        # always hand the caller an absolute URL to `curl -X PUT --data-binary @file`.
        grant.url = f"{self.base_url}{Routes.upload(grant.token)}"
        return grant

    # -- campaigns ----------------------------------------------------------

    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        return CampaignRef.model_validate(
            self._post(Routes.CAMPAIGNS, json=request.model_dump()))

    def get_status(self, campaign_id: str) -> Status:
        return Status.model_validate(self._get(Routes.campaign_status(campaign_id)))

    def get_search_history(self, campaign_id: str) -> SearchHistory:
        return SearchHistory.model_validate(
            self._get(Routes.campaign_search_history(campaign_id)))

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        return LogChunk.model_validate(
            self._get(Routes.campaign_logs(campaign_id), offset=offset))

    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        return ListJobsResponse.model_validate(
            self._get(Routes.campaign_jobs(campaign_id)))

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        return LogChunk.model_validate(
            self._get(Routes.job_log(campaign_id), job_name=job_name, offset=offset))

    def get_job_state(self, campaign_id: str, job_name: str) -> JobState:
        return JobState.model_validate(
            self._get(Routes.job_state(campaign_id), job_name=job_name))

    def exec_in_job(self, campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> "ExecResult":
        # Imported here, as ``exec_in_container`` below does: the module-level import list is
        # already the client's whole surface and this model is only needed on two paths.
        from robovast.service.interface import ExecResult
        return ExecResult.model_validate(
            self._post(Routes.job_exec(campaign_id), job_name=job_name, command=command,
                       container=container, source=source))

    def stop(self, campaign_id: str) -> ActionResult:
        return ActionResult.model_validate(self._post(Routes.campaign_stop(campaign_id)))

    def stop_job(self, campaign_id: str, job_name: str,
                 reason: Optional[str] = None, source: str = "api") -> ActionResult:
        return ActionResult.model_validate(
            self._post(Routes.job_stop(campaign_id), job_name=job_name,
                       reason=reason, source=source))

    def retrigger_campaign(self, campaign_id: str) -> CampaignRef:
        return CampaignRef.model_validate(
            self._post(Routes.campaign_retrigger(campaign_id)))

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        request = request or ListCampaignsRequest()
        return ListCampaignsResponse.model_validate(
            self._get(Routes.CAMPAIGNS, limit=request.limit, offset=request.offset))

    def cleanup_campaign_data(self, request) -> ActionResult:
        return ActionResult.model_validate(
            self._post(Routes.CLEANUP_DATA,
                       {"campaign_id": request.campaign_id, "force": request.force}))

    def delete_campaign(self, campaign_id: str) -> ActionResult:
        return ActionResult.model_validate(self._delete(Routes.campaign(campaign_id)))

    def create_archive_upload(self) -> UploadGrant:
        grant = UploadGrant.model_validate(self._post(Routes.CAMPAIGN_ARCHIVES))
        # As in create_upload: the service answers a relative path because it cannot know
        # its external base, so the caller is handed something it can PUT to directly.
        grant.url = f"{self.base_url}{Routes.campaign_archive_upload(grant.token)}"
        return grant

    def list_share_archives(self) -> "ShareListing":
        from robovast.service.interface import ShareListing
        return ShareListing.model_validate(self._get(Routes.SHARE_ARCHIVES))

    def import_campaign(self, request: ImportCampaignRequest) -> CampaignRef:
        return CampaignRef.model_validate(
            self._post(Routes.CAMPAIGN_IMPORT, json=request.model_dump()))

    # -- image builds -------------------------------------------------------

    def build_image(self, request: BuildImageRequest) -> ImageBuildRef:
        return ImageBuildRef.model_validate(
            self._post(Routes.IMAGE_BUILDS, json=request.model_dump()))

    def get_image_build_status(self, build_id: str) -> ImageBuildStatus:
        return ImageBuildStatus.model_validate(
            self._get(Routes.image_build_status(build_id)))

    def get_image_build_log(self, build_id: str, offset: int = 0) -> LogChunk:
        return LogChunk.model_validate(
            self._get(Routes.image_build_log(build_id), offset=offset))

    # -- container exec -----------------------------------------------------

    def exec_in_container(self, request):
        # The read timeout must outlast the command's own limit, or a legitimately
        # long-running exec would surface as a transport failure rather than its result.
        from robovast.service.interface import COMMAND_LIMIT_S, ExecResult
        return ExecResult.model_validate(
            self._post(Routes.EXEC, json=request.model_dump(),
                       timeout=COMMAND_LIMIT_S + 30))

    def stop_exec_container(self):
        from robovast.service.interface import ExecStopResult
        return ExecStopResult.model_validate(self._delete(Routes.EXEC))

    def resolve_image(self, request):
        from robovast.service.interface import ImageResolution
        return ImageResolution.model_validate(
            self._post(Routes.EXEC_RESOLVE_IMAGE, json=request.model_dump()))

    def get_postprocessing(self, campaign_id: str):
        from robovast.service.interface import PostprocessingInfo
        return PostprocessingInfo.model_validate(
            self._get(Routes.campaign_postprocessing(campaign_id)))

    def update_postprocessing(self, request):
        from robovast.service.interface import PostprocessingRevision
        return PostprocessingRevision.model_validate(self._post(
            Routes.campaign_postprocessing(request.campaign_id),
            json=request.model_dump()))

    def get_postprocessing_source(self, campaign_id: str):
        from robovast.service.interface import PostprocessingSource
        return PostprocessingSource.model_validate(
            self._get(Routes.campaign_postprocessing_source(campaign_id)))

    def update_postprocessing_source(self, request):
        from robovast.service.interface import PostprocessingSource
        return PostprocessingSource.model_validate(self._post(
            Routes.campaign_postprocessing_source(request.campaign_id),
            json=request.model_dump()))

    def run_postprocessing(self, request) -> ActionResult:
        return ActionResult.model_validate(self._post(
            Routes.campaign_postprocessing_run(request.campaign_id),
            json=request.model_dump()))

    def run_share(self, request) -> ActionResult:
        return ActionResult.model_validate(self._post(
            Routes.campaign_share_run(request.campaign_id),
            json=request.model_dump()))

    # -- validation / preview / authoring help (config editor) --------------

    def materialize_retrigger_workspace(self, campaign_id: str,
                                        workspace_name: str) -> WorkOrder:
        return WorkOrder.model_validate(
            self._post(Routes.campaign_retrigger_workspace(campaign_id),
                       json={"workspace_name": workspace_name}))

    def check_retrigger(self, campaign_id: str) -> RetriggerReport:
        return RetriggerReport.model_validate(
            self._get(Routes.campaign_retrigger_check(campaign_id)))

    def validate_project(self, workspace_id: str, path: str = "",
                         check_world: bool = True) -> ValidationReport:
        from robovast.service.interface import COMMAND_LIMIT_S
        return ValidationReport.model_validate(
            self._post(Routes.workspace_validate(workspace_id),
                       json={"path": path, "check_world": check_world},
                       # The world check runs a container: cold, that is seconds on the
                       # local lane and can be well over ten on a busy cluster, which the
                       # default read timeout would cut short mid-check.
                       timeout=COMMAND_LIMIT_S))

    def preview_configurations(
        self, workspace_id: str, max_configs: int = 0, path: str = ""
    ) -> PreviewResponse:
        return PreviewResponse.model_validate(self._post(
            Routes.workspace_preview(workspace_id),
            json={"max_configs": max_configs, "path": path}))

    def describe_world(self, workspace_id: str, path: str = "", targets: str = "",
                       entities: bool = False, backend: str = "") -> WorldDescription:
        # The simulator answers inside a container, and on a node that has never pulled this
        # campaign's image the first thing that happens is the pull -- the same reasoning (and
        # the same budget) as DATA_TIMEOUT below, or a caller sees a ReadTimeout it cannot
        # distinguish from a broken service.
        return WorldDescription.model_validate(self._post(
            Routes.workspace_world(workspace_id),
            json={"path": path, "targets": targets, "entities": entities,
                  "backend": backend},
            timeout=self.SCREENSHOT_TIMEOUT))

    def get_config_schema(self) -> dict:
        return self._get(Routes.CONFIG_SCHEMA)

    def list_variation_types(self) -> VariationTypesResponse:
        return VariationTypesResponse.model_validate(self._get(Routes.VARIATION_TYPES))

    #: A data call can spend minutes inside the request — a query is answered by the index
    #: now rather than by a fetch, but a wide aggregate over a large campaign still runs
    #: there — and the default 30 s would abort the client mid-answer, leaving the caller
    #: with a ReadTimeout indistinguishable from a broken service. The web UI never hit
    #: this because ``fetch`` sets no timeout at all.
    DATA_TIMEOUT = 900.0

    #: A screenshot renders inside the request, and on a node that has never run this
    #: campaign's simulator the first thing it does is pull the image. Same reasoning as
    #: ``DATA_TIMEOUT``, same budget: a client that gives up at 30 s reports a timeout where
    #: the honest answer is that the pull is still going.
    SCREENSHOT_TIMEOUT = 900.0

    def describe_campaign_data(self, campaign_id: str) -> "DataDescribe":
        from robovast.service.interface import DataDescribe
        return DataDescribe.model_validate(self._get(
            Routes.campaign_describe(campaign_id),
            timeout=max(self.timeout, self.DATA_TIMEOUT)))

    def stream_campaign_query_csv(self, campaign_id: str, sql: str,
                                  extra_campaign_ids=None):
        """Stream the CSV export through, chunk by chunk.

        Not ``_get``: that decodes a JSON body, and the point of this route is that the
        result may be larger than memory at either end. ``DATA_TIMEOUT`` because the first
        query on a cluster campaign fetches its databases inside the request, exactly as
        the JSON query does.
        """
        params = {"sql": sql}
        if extra_campaign_ids:
            params["extra_campaign_ids"] = ",".join(extra_campaign_ids)
        resp = self.session.get(f"{self.base_url}{Routes.campaign_query_csv(campaign_id)}",
                            params=params, timeout=self.DATA_TIMEOUT, stream=True)
        self.raise_for_status(resp)
        return resp.iter_content(chunk_size=64 * 1024, decode_unicode=True)

    def campaign_tar_stream(self, campaign_id: str):
        """Stream the campaign archive through, chunk by chunk.

        Not ``_get``: the body is a gzip stream that can run to ~1TB, so neither end
        may hold it. ``vast campaign download`` writes these chunks to a file;
        :func:`~robovast.service.project_push.download_campaign_archive` is that, with
        a progress bar and an atomic rename.
        """
        resp = self.session.get(f"{self.base_url}{Routes.campaign_archive(campaign_id)}",
                                timeout=self.DATA_TIMEOUT, stream=True)
        self.raise_for_status(resp)
        return resp.iter_content(chunk_size=1024 * 1024)

    def campaign_data_status(self, campaign_id: str) -> "CampaignDataStatus":
        # Deliberately the *default* timeout: this is the cheap probe, and if it hangs the
        # answer is "the service is unwell", not "be patient".
        from robovast.service.interface import CampaignDataStatus
        return CampaignDataStatus.model_validate(
            self._get(Routes.campaign_data_status(campaign_id)))

    def campaign_scene_status(self, campaign_id: str, config_name: str,
                              run_id: str) -> "SceneStatus":
        # The default timeout, as for data-status: this is the cheap probe, and it never builds.
        from robovast.service.interface import SceneStatus
        return SceneStatus.model_validate(self._get(
            Routes.campaign_scene(campaign_id),
            config_name=config_name, run_id=str(run_id)))

    def run_campaign_scene(self, campaign_id: str, config_name: str,
                           run_id: str) -> "ActionResult":
        # Returns as soon as the build is dispatched, so the default timeout is right here too --
        # progress is read from campaign_scene_status, not from this call.
        from urllib.parse import urlencode
        query = urlencode({"config_name": config_name, "run_id": str(run_id)})
        return ActionResult.model_validate(self._post(
            f"{Routes.campaign_scene_run(campaign_id)}?{query}"))

    def campaign_screenshot(self, campaign_id: str, config_name: str, run_id: str, *,
                            at=None, view=None, focus=None, camera=None,
                            size: str = "960x720") -> str:
        """POST the render and land the PNG in a temp dir, keeping the local contract.

        The interface returns a *path* because the service builds one, and a path means
        nothing across HTTP — so the bytes are written into the same directory shape
        ``screenshot.render`` produces, and ``screenshot.discard`` removes it either way. One
        cleanup rule for both, rather than a caller that has to know which lane answered.

        **A long timeout, deliberately.** This is the one call that may pull a 2 GB image
        before it can start, inside the request; the default would give up on a cold node and
        report a timeout where the honest answer is "still pulling".
        """
        import tempfile
        from pathlib import Path
        from urllib.parse import urlencode


        params = [("config_name", config_name), ("run_id", str(run_id)), ("size", size)]
        if at is not None:
            params.append(("at", str(at)))
        if camera:
            params.append(("camera", camera))
        params += [("view", f"{k}={v}") for k, v in sorted((view or {}).items())]
        params += [("focus", str(f)) for f in (focus or [])]
        resp = self.session.post(
            f"{self.base_url}{Routes.campaign_screenshot(campaign_id)}?{urlencode(params)}",
            timeout=self.SCREENSHOT_TIMEOUT)
        self.raise_for_status(resp)
        out = Path(tempfile.mkdtemp(prefix="robovast-screenshot-")) / "render"
        out.mkdir()
        frame = out / "frame.png"
        frame.write_bytes(resp.content)
        return str(frame)

    def workspace_scene_status(self, workspace_id: str, path: str = "") -> "SceneStatus":
        from robovast.service.interface import SceneStatus
        return SceneStatus.model_validate(self._get(
            Routes.workspace_scene(workspace_id), path=path))

    def run_workspace_scene(self, workspace_id: str, path: str = "") -> "ActionResult":
        from urllib.parse import urlencode
        query = urlencode({"path": path})
        return ActionResult.model_validate(self._post(
            f"{Routes.workspace_scene_run(workspace_id)}?{query}"))

    def resolve_workspace_scene_asset(self, workspace_id: str, path: str) -> str:
        # Same as its campaign sibling: a path on the service's disk means nothing here.
        raise NotImplementedError(
            "a scene asset is fetched over HTTP from SceneStatus.url, not resolved to a local path")

    def resolve_campaign_scene_asset(self, campaign_id: str, path: str) -> str:
        # A *path on the service's disk* has no meaning across HTTP; a remote caller fetches the bytes
        # from the address the status reports. Refusing beats returning a path that is not there.
        raise NotImplementedError(
            "a scene asset is fetched over HTTP from SceneStatus.url, not resolved to a local path")

    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        extra_campaign_ids=None, max_bytes=None,
    ) -> "DataQueryResult":
        from robovast.service.interface import DataQueryResult
        body = {"sql": sql, "max_rows": max_rows,
                "extra_campaign_ids": extra_campaign_ids or []}
        # Only sent when asked for: an older service rejects an unknown body key, and the
        # default is the one this client's callers (MCP tools) want anyway.
        if max_bytes is not None:
            body["max_bytes"] = max_bytes
        return DataQueryResult.model_validate(self._post(
            Routes.campaign_query(campaign_id), json=body,
            timeout=max(self.timeout, self.DATA_TIMEOUT)))

    def list_campaign_plots(self, campaign_id: str) -> "CampaignPlotsResponse":
        from robovast.service.interface import CampaignPlotsResponse
        return CampaignPlotsResponse.model_validate(
            self._get(Routes.campaign_plots(campaign_id),
                      timeout=max(self.timeout, self.DATA_TIMEOUT)))

    def list_campaign_panels(self, campaign_id: str) -> "CampaignPanelsResponse":
        from robovast.service.interface import CampaignPanelsResponse
        return CampaignPanelsResponse.model_validate(
            self._get(Routes.campaign_panels(campaign_id)))

    def get_panels_source(self, campaign_id: str) -> "PanelsSource":
        from robovast.service.interface import PanelsSource
        return PanelsSource.model_validate(
            self._get(Routes.campaign_panels_source(campaign_id)))

    def update_panels_source(self, request) -> "PanelsSource":
        from robovast.service.interface import PanelsSource
        return PanelsSource.model_validate(self._post(
            Routes.campaign_panels_source(request.campaign_id),
            json=request.model_dump()))

    def list_campaign_visualizations(
        self, campaign_id: str
    ) -> "CampaignVisualizationsResponse":
        from robovast.service.interface import CampaignVisualizationsResponse
        return CampaignVisualizationsResponse.model_validate(
            self._get(Routes.campaign_visualizations(campaign_id)))

    def render_campaign_notebook(
        self, campaign_id: str, workload: str, level: str,
        config_name: str = "", run_id=None, theme: str = "light", batch=None,
    ) -> str:
        params = {"workload": workload, "level": level, "theme": theme}
        if config_name:
            params["config_name"] = config_name
        if run_id is not None:
            params["run_id"] = run_id
        if batch is not None:
            params["batch"] = batch
        resp = self.session.get(
            f"{self.base_url}{Routes.campaign_notebook(campaign_id)}",
            params=params, timeout=max(self.timeout, 600))
        self.raise_for_status(resp)
        return resp.text


# ---------------------------------------------------------------------------
# Facade / factory
# ---------------------------------------------------------------------------


def RobovastClient(service_url: str = "", timeout: float = 30.0,  # noqa: N802  # pylint: disable=invalid-name
                   token: str | None = None,
                   user: str | None = None) -> RobovastInterface:
    """Return a transport-agnostic client.

    * ``service_url`` set → :class:`HTTPTransport` to that ``robovast-service``.
    * empty (default) → :class:`LocalTransport` (in-process local Docker), imported only
      on that branch: the in-process server is 3,000 lines this module otherwise has no
      use for, and an install that ships only the client does not have it at all.

    Callers resolve *service_url* explicitly (see
    :func:`robovast.client.service_target.detected_service_url`); there is no
    ambient environment-variable selection of *which service*.

    Credentials, by contrast, **are** ambient when not given: ``token``/``user`` default
    to the stored ``vast login``. Every one of the eight construction sites would
    otherwise have to fetch and thread them, and the one that forgot would 401 only for
    remote users. Pass them explicitly to override.
    """
    if not service_url:
        try:
            from robovast.service.local_transport import \
                LocalTransport  # pylint: disable=import-outside-toplevel
        except ImportError as e:  # a client-only install has no in-process server
            raise RuntimeError(
                "no service URL was given, and this install has no in-process service "
                "to fall back to. Point at a running one: 'vast login <url>', or start "
                "one with 'vast serve'.") from e
        return LocalTransport()
    if token is None or user is None:
        from robovast.client.login import credentials
        _url, stored_token, stored_name = credentials()
        token = stored_token if token is None else token
        user = stored_name if user is None else user
    return HTTPTransport(service_url, timeout=timeout, token=token or "", user=user or "")
