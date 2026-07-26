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

from robovast.execution.control_server import Status
from robovast.service.interface import (ActionResult, BuildImageRequest,
                                        CampaignRef, CreateCampaignRequest,
                                        CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest,
                                        FileContent, FileMeta, ImageBuildRef,
                                        ImageBuildStatus, ListCampaignsRequest,
                                        ListCampaignsResponse, ListJobsResponse,
                                        ListFilesResponse, ListWorkspacesResponse,
                                        LogChunk, PreviewResponse, ResourceUsage,
                                        RobovastInterface, Routes, UploadGrant,
                                        ValidationReport, VariationTypesResponse,
                                        VersionInfo, WorkspaceInfo,
                                        WriteFileRequest)
from robovast.service.local_transport import (LocalTransport,
                                              _robovast_version)

logger = logging.getLogger(__name__)


class HTTPTransport(RobovastInterface):
    """Talks to a running ``robovast-service`` over the :class:`Routes` contract.

    The base URL is typically ``http://127.0.0.1:<port>`` reached via an SSH
    tunnel (remote VM) or ``kubectl port-forward`` (cluster); the tunnel is
    managed by the caller (e.g. ``vast mcp serve``), not here.
    """

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, **params):
        import requests
        resp = requests.get(f"{self.base_url}{path}", params=params or None,
                            timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json=None):
        import requests
        resp = requests.post(f"{self.base_url}{path}", json=json,
                            timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, **params):
        import requests
        resp = requests.delete(f"{self.base_url}{path}", params=params or None,
                               timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def version(self) -> VersionInfo:
        return VersionInfo.model_validate(self._get(Routes.VERSION))

    def resource_usage(self) -> ResourceUsage:
        return ResourceUsage.model_validate(self._get(Routes.USAGE))

    def check_compatibility(self) -> dict:
        """Compare this client's robovast version with the service's (handshake).

        Returns ``{compatible, client_version, service_version, backend}`` and
        logs a warning on mismatch so a stale service surfaces instead of failing
        obscurely. Best-effort: an unreachable service yields ``compatible=None``.
        """
        client_v = _robovast_version()
        try:
            info = self.version()
        except Exception as e:  # noqa: BLE001 - unreachable service
            logger.warning("could not reach service for version check: %s", e)
            return {"compatible": None, "client_version": client_v,
                    "service_version": None, "backend": None}
        compatible = info.robovast_version == client_v
        if not compatible:
            logger.warning(
                "robovast version mismatch: client %s vs service %s. Re-run "
                "'vast exec cluster setup' to upgrade the in-cluster service.",
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

    def write_project_file(self, request: WriteFileRequest) -> FileMeta:
        return FileMeta.model_validate(self._post(
            Routes.workspace_file(request.workspace_id), json=request.model_dump()))

    def edit_project_file(self, request: EditFileRequest) -> FileMeta:
        return FileMeta.model_validate(self._post(
            Routes.workspace_edit(request.workspace_id), json=request.model_dump()))

    def read_project_file(self, workspace_id: str, path: str) -> FileContent:
        return FileContent.model_validate(
            self._get(Routes.workspace_file(workspace_id), path=path))

    def list_project_files(self, workspace_id: str) -> ListFilesResponse:
        return ListFilesResponse.model_validate(
            self._get(Routes.workspace_files(workspace_id)))

    def delete_project_file(self, workspace_id: str, path: str) -> ActionResult:
        return ActionResult.model_validate(
            self._delete(Routes.workspace_file(workspace_id), path=path))

    def create_upload(self, request: CreateUploadRequest) -> UploadGrant:
        grant = UploadGrant.model_validate(self._post(
            Routes.workspace_upload(request.workspace_id), json=request.model_dump()))
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

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        from robovast.service.interface import LogChunk
        return LogChunk.model_validate(
            self._get(Routes.campaign_logs(campaign_id), offset=offset))

    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        return ListJobsResponse.model_validate(
            self._get(Routes.campaign_jobs(campaign_id)))

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        return LogChunk.model_validate(
            self._get(Routes.job_log(campaign_id), job_name=job_name, offset=offset))

    def stop(self, campaign_id: str) -> ActionResult:
        return ActionResult.model_validate(self._post(Routes.campaign_stop(campaign_id)))

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

    def validate_project(self, workspace_id: str, path: str = "") -> ValidationReport:
        return ValidationReport.model_validate(
            self._post(Routes.workspace_validate(workspace_id), json={"path": path}))

    def preview_configurations(
        self, workspace_id: str, max_configs: int = 0, path: str = ""
    ) -> PreviewResponse:
        return PreviewResponse.model_validate(self._post(
            Routes.workspace_preview(workspace_id),
            json={"max_configs": max_configs, "path": path}))

    def get_config_schema(self) -> dict:
        return self._get(Routes.CONFIG_SCHEMA)

    def list_variation_types(self) -> VariationTypesResponse:
        return VariationTypesResponse.model_validate(self._get(Routes.VARIATION_TYPES))

    def describe_campaign_data(self, campaign_id: str) -> "DataDescribe":
        from robovast.service.interface import DataDescribe
        return DataDescribe.model_validate(self._get(Routes.campaign_describe(campaign_id)))

    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        extra_campaign_ids=None,
    ) -> "DataQueryResult":
        from robovast.service.interface import DataQueryResult
        return DataQueryResult.model_validate(self._post(
            Routes.campaign_query(campaign_id),
            json={"sql": sql, "max_rows": max_rows,
                  "extra_campaign_ids": extra_campaign_ids or []}))

    def list_campaign_plots(self, campaign_id: str) -> "CampaignPlotsResponse":
        from robovast.service.interface import CampaignPlotsResponse
        return CampaignPlotsResponse.model_validate(
            self._get(Routes.campaign_plots(campaign_id)))

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

    def get_run_file(
        self, campaign_id: str, config_name: str, run_id: int, path: str,
    ) -> bytes:
        import requests
        resp = requests.get(
            f"{self.base_url}"
            f"{Routes.campaign_run_file(campaign_id, config_name, run_id, path)}",
            timeout=self.timeout)
        resp.raise_for_status()
        return resp.content

    def list_campaign_visualizations(
        self, campaign_id: str
    ) -> "CampaignVisualizationsResponse":
        from robovast.service.interface import CampaignVisualizationsResponse
        return CampaignVisualizationsResponse.model_validate(
            self._get(Routes.campaign_visualizations(campaign_id)))

    def render_campaign_notebook(
        self, campaign_id: str, workload: str, level: str,
        config_name: str = "", run_id=None, theme: str = "light",
    ) -> str:
        import requests
        params = {"workload": workload, "level": level, "theme": theme}
        if config_name:
            params["config_name"] = config_name
        if run_id is not None:
            params["run_id"] = run_id
        resp = requests.get(
            f"{self.base_url}{Routes.campaign_notebook(campaign_id)}",
            params=params, timeout=max(self.timeout, 600))
        resp.raise_for_status()
        return resp.text


# ---------------------------------------------------------------------------
# Facade / factory
# ---------------------------------------------------------------------------


def RobovastClient(service_url: str = "", timeout: float = 30.0) -> RobovastInterface:  # noqa: N802
    """Return a transport-agnostic client.

    * ``service_url`` set → :class:`HTTPTransport` to that ``robovast-service``.
    * empty (default) → :class:`LocalTransport` (in-process local Docker).

    Callers resolve *service_url* explicitly (the CLI/MCP auto-detect a service on
    the conventional local port via
    :func:`robovast.common.cli.service_target.detected_service_url`); there is no
    ambient environment-variable selection.
    """
    if service_url:
        return HTTPTransport(service_url, timeout=timeout)
    return LocalTransport()
