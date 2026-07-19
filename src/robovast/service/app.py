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

"""The ``robovast-service`` FastAPI app — HTTP binding of the interface.

:func:`build_app` wraps *any* :class:`~robovast.service.interface.RobovastInterface`
implementation and exposes it over the :class:`~robovast.service.interface.Routes`
contract, so the same app serves:

* a **local** ``vast serve`` (impl = :class:`~robovast.service.client.LocalTransport`,
  Docker backend, local filesystem) — persistent single-host service (mode 2);
* a **cluster** deployment (impl = the cluster service core) — mode 3.

This generalises the per-campaign FastAPI control channel in
:mod:`robovast.execution.control_server` into a persistent, campaign-spanning
service. FastAPI auto-emits OpenAPI at ``/docs``, so the same contract serves the
CLI, the MCP server, and a future web UI. ``fastapi``/``uvicorn`` are imported
lazily so importing this module stays cheap.
"""

import logging

from robovast.service.interface import (ActionResult, CampaignRef,
                                        CreateCampaignRequest,
                                        CleanupDataRequest,
                                        CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest,
                                        FileContent, FileMeta,
                                        ListCampaignsResponse, ListFilesResponse,
                                        ListJobsResponse,
                                        ListWorkspacesResponse, LogChunk,
                                        PreviewResponse,
                                        ResourceUsage,
                                        RobovastInterface, Routes,
                                        RunPostprocessingRequest, Status,
                                        UpdatePostprocessingRequest, UploadGrant,
                                        ValidationReport, VariationTypesResponse,
                                        VersionInfo, WorkspaceInfo, WriteFileRequest,
                                        DataDescribe, DataQueryResult,
                                        CampaignPlotsResponse,
                                        CampaignPanelsResponse, CostmapFrame,
                                        CampaignVisualizationsResponse)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8800


def build_app(impl: RobovastInterface):
    """Build the FastAPI app bound to *impl* (lazy import; needs ``fastapi``)."""
    from contextlib import \
        asynccontextmanager  # pylint: disable=import-outside-toplevel

    import anyio  # pylint: disable=import-outside-toplevel
    from fastapi import (Body, FastAPI,  # pylint: disable=import-outside-toplevel
                         HTTPException, Request)

    @asynccontextmanager
    async def _lifespan(_app):
        """Run ``impl.shutdown()`` on service teardown (Ctrl+C on ``vast serve``).

        uvicorn runs the lifespan shutdown when it catches SIGINT, but it does not
        wait on the daemon worker threads a local campaign runs on. Stopping them
        here — off the event loop, since the join blocks — lets a Ctrl+C tear down
        the running campaign's containers instead of orphaning them.
        """
        yield
        try:
            await anyio.to_thread.run_sync(impl.shutdown)
        except Exception:  # noqa: BLE001 - teardown must never mask the real exit
            logger.exception("error during service shutdown")

    app = FastAPI(title="robovast-service", docs_url="/docs", lifespan=_lifespan)

    def _guard(fn):
        """Map interface exceptions to clean HTTP errors instead of 500s."""
        try:
            return fn()
        except ValueError as e:            # bad input / not-initialized
            raise HTTPException(status_code=400, detail=str(e)) from e
        except KeyError as e:              # unknown id
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RuntimeError as e:          # conflict (e.g. single-flight)
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.get(Routes.HEALTHZ)
    def healthz() -> dict:
        return {"ok": True}

    @app.get(Routes.VERSION, response_model=VersionInfo)
    def version() -> VersionInfo:
        return _guard(impl.version)

    @app.get(Routes.USAGE, response_model=ResourceUsage)
    def resource_usage() -> ResourceUsage:
        return _guard(impl.resource_usage)

    # -- authoring help (static; config editor) -----------------------------

    @app.get(Routes.CONFIG_SCHEMA)
    def get_config_schema() -> dict:
        return _guard(impl.get_config_schema)

    @app.get(Routes.VARIATION_TYPES, response_model=VariationTypesResponse)
    def list_variation_types() -> VariationTypesResponse:
        return _guard(impl.list_variation_types)

    @app.get("/variation_types/{name}/assets/{path:path}")
    def variation_asset(name: str, path: str):
        """Serve a variation plugin's web-preview asset (Module Federation remote).

        External variation plugins ship a built ``remoteEntry.js`` + chunks as
        package data under their ``WEB_PREVIEW`` dir; the config editor loads them
        at runtime. Built-in types have no assets (they render host-native).
        """
        from fastapi.responses import \
            FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(lambda: _resolve_variation_asset(name, path))))

    # -- workspaces ---------------------------------------------------------

    @app.post(Routes.WORKSPACES, response_model=WorkspaceInfo)
    def create_workspace(request: CreateWorkspaceRequest) -> WorkspaceInfo:
        return _guard(lambda: impl.create_workspace(request))

    @app.get(Routes.WORKSPACES, response_model=ListWorkspacesResponse)
    def list_workspaces() -> ListWorkspacesResponse:
        return _guard(impl.list_workspaces)

    @app.get("/workspaces/{workspace_id}", response_model=WorkspaceInfo)
    def get_workspace(workspace_id: str) -> WorkspaceInfo:
        return _guard(lambda: impl.get_workspace(workspace_id))

    @app.delete("/workspaces/{workspace_id}", response_model=ActionResult)
    def delete_workspace(workspace_id: str) -> ActionResult:
        return _guard(lambda: impl.delete_workspace(workspace_id))

    # -- workspace files ----------------------------------------------------

    @app.get("/workspaces/{workspace_id}/files", response_model=ListFilesResponse)
    def list_project_files(workspace_id: str) -> ListFilesResponse:
        return _guard(lambda: impl.list_project_files(workspace_id))

    @app.post("/workspaces/{workspace_id}/file", response_model=FileMeta)
    def write_project_file(workspace_id: str, request: WriteFileRequest) -> FileMeta:
        return _guard(lambda: impl.write_project_file(request))

    @app.get("/workspaces/{workspace_id}/file", response_model=FileContent)
    def read_project_file(workspace_id: str, path: str) -> FileContent:
        return _guard(lambda: impl.read_project_file(workspace_id, path))

    @app.delete("/workspaces/{workspace_id}/file", response_model=ActionResult)
    def delete_project_file(workspace_id: str, path: str) -> ActionResult:
        return _guard(lambda: impl.delete_project_file(workspace_id, path))

    @app.post("/workspaces/{workspace_id}/edit", response_model=FileMeta)
    def edit_project_file(workspace_id: str, request: EditFileRequest) -> FileMeta:
        return _guard(lambda: impl.edit_project_file(request))

    # -- validation / preview (config editor) -------------------------------

    @app.post("/workspaces/{workspace_id}/validate", response_model=ValidationReport)
    def validate_project(
        workspace_id: str, path: str = Body("", embed=True)
    ) -> ValidationReport:
        return _guard(lambda: impl.validate_project(workspace_id, path))

    @app.post("/workspaces/{workspace_id}/preview", response_model=PreviewResponse)
    def preview_configurations(
        workspace_id: str, max_configs: int = Body(0, embed=True),
        path: str = Body("", embed=True),
    ) -> PreviewResponse:
        return _guard(lambda: impl.preview_configurations(workspace_id, max_configs, path))

    # -- file side channel: grant + raw PUT ---------------------------------

    @app.post("/workspaces/{workspace_id}/uploads", response_model=UploadGrant)
    def create_upload(workspace_id: str, request: CreateUploadRequest) -> UploadGrant:
        grant = _guard(lambda: impl.create_upload(request))
        grant.url = f"{Routes.upload(grant.token)}"
        return grant

    @app.put("/uploads/{token}", response_model=FileMeta)
    async def put_upload(token: str, req: Request) -> FileMeta:
        """Redeem a one-time TTL token and store the raw body.

        This is the token-free path: the client streams bytes here directly
        (``curl -X PUT --data-binary @file``), so run files, notebooks and
        binaries never pass through an LLM's context.
        """
        body = await req.body()
        store = getattr(impl, "store", None)
        if store is None:
            raise HTTPException(status_code=501,
                                detail="this service has no workspace store")
        return _guard(lambda: FileMeta.model_validate(store.write_upload(token, body)))

    @app.post(Routes.CAMPAIGNS, response_model=CampaignRef)
    def create_campaign(request: CreateCampaignRequest) -> CampaignRef:
        return _guard(lambda: impl.create_campaign(request))

    @app.get(Routes.CAMPAIGNS, response_model=ListCampaignsResponse)
    def list_campaigns(limit: int = 20, offset: int = 0) -> ListCampaignsResponse:
        from robovast.service.interface import \
            ListCampaignsRequest  # pylint: disable=import-outside-toplevel
        return _guard(
            lambda: impl.list_campaigns(ListCampaignsRequest(limit=limit, offset=offset)))

    @app.get("/campaigns/{campaign_id}/status", response_model=Status)
    def get_status(campaign_id: str) -> Status:
        return _guard(lambda: impl.get_status(campaign_id))

    @app.get("/campaigns/{campaign_id}/logs", response_model=LogChunk)
    def get_campaign_logs(campaign_id: str, offset: int = 0) -> LogChunk:
        return _guard(lambda: impl.get_campaign_logs(campaign_id, offset))

    @app.get(Routes.campaign_jobs("{campaign_id}"), response_model=ListJobsResponse)
    def list_jobs(campaign_id: str) -> ListJobsResponse:
        return _guard(lambda: impl.list_jobs(campaign_id))

    @app.get(Routes.job_log("{campaign_id}"), response_model=LogChunk)
    def get_job_log(campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        return _guard(lambda: impl.get_job_log(campaign_id, job_name, offset))

    @app.post("/campaigns/{campaign_id}/stop", response_model=ActionResult)
    def stop(campaign_id: str) -> ActionResult:
        return _guard(lambda: impl.stop(campaign_id))

    @app.post(Routes.CLEANUP_DATA, response_model=ActionResult)
    def cleanup_campaign_data(request: "CleanupDataRequest | None" = None) -> ActionResult:
        # Body optional: no body means "all finished campaigns" (live ones skipped).
        return _guard(lambda: impl.cleanup_campaign_data(request or CleanupDataRequest()))

    @app.get("/campaigns/{campaign_id}/archive")
    def download_campaign_archive(campaign_id: str):
        """Stream a ``tar.gz`` of the **postprocessed** campaign from the object store.

        Backs the ``postprocessed`` variant of ``vast results download`` for a
        **cluster** service: objects are fetched from the object store and tarred on
        the fly (``impl.campaign_tar_stream``) straight into the response — **no
        scratch is used on the service and nothing is buffered in memory**, decisive
        for ~1TB campaigns. Internal ``_postproc/`` staging is excluded so the download
        is the clean campaign layout.

        A **local** service refuses: its results already live on the same filesystem,
        so there is nothing to download.
        """
        from fastapi.responses import \
            StreamingResponse  # pylint: disable=import-outside-toplevel

        if not hasattr(impl, "campaign_tar_stream"):  # local service
            results_dir = getattr(getattr(impl, "store", None), "root", None)
            hint = f" under {results_dir}" if results_dir else ""
            raise HTTPException(
                status_code=409,
                detail=(f"this service runs locally; campaign '{campaign_id}' results "
                        f"are already on this host's filesystem{hint} — no download needed"))

        return StreamingResponse(
            _guard(lambda: impl.campaign_tar_stream(campaign_id)),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{campaign_id}.tar.gz"'})

    @app.get("/campaigns/{campaign_id}/postprocessing")
    def get_postprocessing(campaign_id: str):
        return _guard(lambda: impl.get_postprocessing(campaign_id))

    @app.post("/campaigns/{campaign_id}/postprocessing")
    def update_postprocessing(campaign_id: str, request: UpdatePostprocessingRequest):
        return _guard(lambda: impl.update_postprocessing(request))

    @app.post("/campaigns/{campaign_id}/postprocessing/run", response_model=ActionResult)
    def run_postprocessing(campaign_id: str, request: RunPostprocessingRequest) -> ActionResult:
        return _guard(lambda: impl.run_postprocessing(request))

    # -- results data query (eval viewer) -----------------------------------

    @app.get("/campaigns/{campaign_id}/describe", response_model=DataDescribe)
    def describe_campaign_data(campaign_id: str) -> DataDescribe:
        return _guard(lambda: impl.describe_campaign_data(campaign_id))

    @app.post("/campaigns/{campaign_id}/query", response_model=DataQueryResult)
    def query_campaign_data_sql(
        campaign_id: str, sql: str = Body(..., embed=True),
        max_rows: int = Body(500, embed=True),
        extra_campaign_ids: list[str] = Body(default_factory=list, embed=True),
    ) -> DataQueryResult:
        return _guard(lambda: impl.query_campaign_data_sql(
            campaign_id, sql, max_rows, extra_campaign_ids))

    @app.get("/campaigns/{campaign_id}/plots", response_model=CampaignPlotsResponse)
    def list_campaign_plots(campaign_id: str) -> CampaignPlotsResponse:
        return _guard(lambda: impl.list_campaign_plots(campaign_id))

    @app.get("/campaigns/{campaign_id}/panels", response_model=CampaignPanelsResponse)
    def list_campaign_panels(campaign_id: str) -> CampaignPanelsResponse:
        return _guard(lambda: impl.list_campaign_panels(campaign_id))

    @app.get("/campaigns/{campaign_id}/costmap", response_model=CostmapFrame | None)
    def get_costmap_frame(
        campaign_id: str, config_name: str, run_id: int, topic: str, t: float,
    ) -> CostmapFrame | None:
        return _guard(lambda: impl.get_costmap_frame(
            campaign_id, config_name, run_id, topic, t))

    @app.get("/campaigns/{campaign_id}/visualizations",
             response_model=CampaignVisualizationsResponse)
    def list_campaign_visualizations(campaign_id: str) -> CampaignVisualizationsResponse:
        return _guard(lambda: impl.list_campaign_visualizations(campaign_id))

    # Sync ``def`` so Starlette runs the (blocking, multi-second) notebook execution in
    # the threadpool, off the event loop. Returns the executed notebook's HTML verbatim
    # for the Explorer's iframe; a cache hit (see notebook_render) makes repeats instant.
    @app.get("/campaigns/{campaign_id}/notebook")
    def render_campaign_notebook(
        campaign_id: str, workload: str, level: str,
        config_name: str = "", run_id: int | None = None, theme: str = "light",
    ):
        from fastapi.responses import \
            HTMLResponse  # pylint: disable=import-outside-toplevel
        from nbclient.exceptions import \
            CellExecutionError  # pylint: disable=import-outside-toplevel
        from robovast.results_processing.notebook_render import \
            message_page_html  # pylint: disable=import-outside-toplevel

        def _render():
            try:
                return impl.render_campaign_notebook(
                    campaign_id, workload, level, config_name, run_id, theme)
            except CellExecutionError as e:
                # A notebook can `raise SystemExit("...")` to bail cleanly when there's nothing
                # to show (e.g. no rosbag data for this node). Render that as a neutral empty-state
                # page in the iframe rather than a red execution error.
                if getattr(e, "ename", None) == "SystemExit":
                    return message_page_html(e.evalue or "Nothing to display.", theme)
                # A real cell failure: surface the concise "<Error>: <message>" as a 422 so the
                # Explorer shows a readable error instead of a raw 500 ASGI traceback.
                detail = f"{e.ename}: {e.evalue}" if getattr(e, "ename", None) else str(e)
                raise HTTPException(status_code=422,
                                    detail=f"Notebook execution failed — {detail}") from e

        html = _guard(_render)
        return HTMLResponse(content=html)

    _mount_ui(app)
    return app


def _resolve_variation_asset(name: str, rel_path: str):
    """Resolve a variation type's web-preview asset file, confined to its asset dir.

    Raises ``KeyError`` (→ 404) for an unknown type / no ``WEB_PREVIEW`` / missing
    file, ``ValueError`` (→ 400) if *rel_path* escapes the asset directory.
    """
    import inspect  # pylint: disable=import-outside-toplevel
    import os  # pylint: disable=import-outside-toplevel
    from importlib.metadata import \
        entry_points  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    cls = next((ep.load() for ep in entry_points(group="robovast.variation_types")
                if ep.name == name), None)
    if cls is None:
        raise KeyError(f"unknown variation type {name!r}")
    asset_rel = getattr(cls, "WEB_PREVIEW", None)
    if not asset_rel:
        raise KeyError(f"variation type {name!r} has no web preview")
    asset_root = (Path(inspect.getfile(cls)).resolve().parent / asset_rel).resolve()
    target = (asset_root / rel_path).resolve()
    if target != asset_root and not str(target).startswith(str(asset_root) + os.sep):
        raise ValueError("path escapes the asset directory")
    if not target.is_file():
        raise KeyError(f"asset not found: {rel_path}")
    return target


def _ui_dist() -> "Optional[Path]":
    """Locate the built web UI (``ui/dist``), or ``None`` if it isn't built.

    Order: ``ROBOVAST_UI_DIST`` env override, then the repo-root ``ui/dist`` relative
    to this source file (the dev / ``vast serve`` layout). Packaged images set the
    env var to the assets baked into the image.
    """
    import os  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    env = os.environ.get("ROBOVAST_UI_DIST")
    candidate = Path(env) if env else Path(__file__).resolve().parents[3] / "ui" / "dist"
    return candidate if (candidate / "index.html").is_file() else None


def _mount_ui(app) -> None:
    """Serve the built SPA (``ui/dist``) from the service so the UI starts with it.

    The RobovastInterface routes are registered first and win; this mounts the SPA at
    ``/`` for everything else (``html=True`` serves ``index.html`` at the root). Served
    same-origin with the API, so no CORS. Silently no-ops when the UI isn't built —
    the service then runs API-only.
    """
    dist = _ui_dist()
    if dist is None:
        logger.info("web UI build not found — serving API only "
                    "(build it with `cd ui && npm run build`)")
        return
    from fastapi.staticfiles import \
        StaticFiles  # pylint: disable=import-outside-toplevel
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    logger.info("serving web UI from %s", dist)


def serve(impl: RobovastInterface, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          log_level: str = "info") -> None:
    """Run the service in the foreground (blocking) via uvicorn.

    Binds ``127.0.0.1`` by default: the service is unauthenticated in v1, so it
    must stay behind a localhost / SSH-tunnel / port-forward boundary (see
    ``docs/deployment.rst``). A remote VM binds VM-localhost and is reached over
    an SSH tunnel.
    """
    import uvicorn  # pylint: disable=import-outside-toplevel

    app = build_app(impl)
    logger.info("robovast-service listening on %s:%d (OpenAPI at /docs)", host, port)
    uvicorn.run(app, host=host, port=port, log_level=log_level,
                log_config=_quiet_access_log_config())


def _quiet_access_log_config() -> dict:
    """uvicorn logging config that demotes per-request access logs to DEBUG.

    The default access logger emits every ``"GET /... 200 OK"`` line at INFO,
    which drowns out the interesting startup/error output. We reclassify those
    records to DEBUG and raise the access handler to INFO, so they stay hidden
    at the default level but reappear when serving at ``--log-level debug``.
    """
    import copy  # pylint: disable=import-outside-toplevel
    from uvicorn.config import LOGGING_CONFIG  # pylint: disable=import-outside-toplevel

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["demote_access"] = {
        "()": f"{__name__}._DemoteToDebugFilter",
    }
    config["handlers"]["access"]["level"] = "INFO"
    config["loggers"]["uvicorn.access"].setdefault("filters", []).append("demote_access")
    return config


class _DemoteToDebugFilter(logging.Filter):
    """Rewrite a log record's level to DEBUG (attached to the access logger).

    Applied on the *logger* (not the handler) so the demoted level is in place
    before the handler's own INFO threshold is checked, causing the record to
    be dropped at the default level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True
