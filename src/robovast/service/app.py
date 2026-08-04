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
from typing import Literal

from robovast.common import file_address
from robovast.service.interface import (ActionResult, BuildImageRequest,
                                        ExecRequest, ExecResult, ExecStopResult,
                                        CampaignRef,
                                        CreateCampaignRequest,
                                        CleanupDataRequest,
                                        CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest,
                                        FileMeta,
                                        ImageBuildRef, ImageBuildStatus,
                                        ListCampaignsResponse,
                                        ListJobsResponse,
                                        ListWorkspacesResponse, LogChunk,
                                        PreviewResponse,
                                        ResourceUsage,
                                        RobovastInterface, Routes,
                                        RunPostprocessingRequest, RunShareRequest,
                                        Status,
                                        UpdatePostprocessingRequest, UploadGrant,
                                        ValidationReport, VariationTypesResponse,
                                        VersionInfo, WorkspaceInfo, WriteFileRequest,
                                        CampaignDataStatus,
                                        DataDescribe, DataQueryResult,
                                        CampaignPlotsResponse,
                                        CampaignPanelsResponse,
                                        PanelsSource, UpdatePanelsSourceRequest,
                                        PostprocessingSource,
                                        UpdatePostprocessingSourceRequest,
                                        CampaignVisualizationsResponse)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8800

#: Routes FastAPI registers for itself. Real, but they describe FastAPI, not this service.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

#: Route groups, in the order a reader wants them: what a client does first, reference
#: last. Every route carries one as its ``tags=`` — the grouping is authored at the route
#: so the generated documentation and the OpenAPI page agree, and a new route cannot land
#: ungrouped. Kept here rather than in the docs extension because it is a property of the
#: API, and :mod:`tests.service.test_route_docs` checks the two stay in step.
ROUTE_TAG_ORDER = ("meta", "authoring", "workspaces", "uploads", "files", "campaigns",
                   "image-builds", "exec", "results", "plugin-endpoints")


def api_routes(app):
    """The routes this service defines: no FastAPI extras, no SPA mount."""
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or path == "/" or path in FRAMEWORK_PATHS:
            continue
        if not hasattr(route, "methods"):
            continue
        out.append(route)
    return out


def route_description(route) -> str:
    """One-line description of *route*, or ``""`` when it genuinely has none.

    Three sources, in order: an explicit ``summary=`` on the decorator, the handler's own
    docstring (FastAPI puts it in ``description``), and otherwise the docstring of the
    matching :class:`RobovastInterface` method. The last one carries most routes: they are
    one-line delegations, so the description belongs beside the *contract* rather than
    copied onto every handler — and then it cannot describe the HTTP layer while the
    interface says something else.

    Returning ``""`` rather than a placeholder is deliberate: the generated route table
    fails on it, because a blank description makes a route look documented.
    """
    for text in (getattr(route, "summary", None), getattr(route, "description", None)):
        if text and text.strip():
            return text.strip().split("\n")[0].strip()
    op = getattr(RobovastInterface, getattr(route.endpoint, "__name__", ""), None)
    doc = getattr(op, "__doc__", None)
    if doc and doc.strip():
        return doc.strip().split("\n")[0].strip()
    return ""


def build_app(impl: RobovastInterface):
    """Build the FastAPI app bound to *impl* (lazy import; needs ``fastapi``)."""
    from contextlib import \
        asynccontextmanager  # pylint: disable=import-outside-toplevel

    import anyio  # pylint: disable=import-outside-toplevel
    from fastapi import (Body, FastAPI,  # pylint: disable=import-outside-toplevel
                         HTTPException, Query, Request)

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

    # Whether the service has begun shutting down. The SSE generators below loop
    # forever and only exit on client disconnect, so an open browser tab would
    # keep them alive and hang uvicorn's graceful shutdown ("Waiting for
    # connections to close") on Ctrl+C. ``serve()`` replaces this with a probe of
    # uvicorn's ``should_exit`` — set at the very start of shutdown, before the
    # connection wait — so the loops close their streams promptly. Default False
    # keeps ``build_app`` usable (e.g. in tests) without a running server.
    app.state.should_exit = lambda: False

    def _guard(fn):
        """Map interface exceptions to clean HTTP errors instead of 500s."""
        try:
            return fn()
        except ValueError as e:            # bad input / not-initialized
            raise HTTPException(status_code=400, detail=str(e)) from e
        except KeyError as e:              # unknown id
            # ``str(KeyError("x"))`` is ``"'x'"``; take the message itself so the
            # detail is not delivered wrapped in stray quotes.
            detail = e.args[0] if e.args else str(e)
            raise HTTPException(status_code=404, detail=str(detail)) from e
        except RuntimeError as e:          # conflict (e.g. single-flight)
            raise HTTPException(status_code=409, detail=str(e)) from e

    # -- SSE log streaming --------------------------------------------------
    # The browser streams logs over Server-Sent Events; MCP/CLI keep the pull
    # endpoints (``.../logs``, ``.../job-log``). Both share the one assembly seam:
    # an SSE stream is just a server-side loop over the same ``LogChunk`` pull, so
    # there is no second implementation of assembly/offset to drift.
    import json as _json  # pylint: disable=import-outside-toplevel

    from fastapi.responses import \
        StreamingResponse  # pylint: disable=import-outside-toplevel

    #: Poll cadence of the server-side tail loop. Sub-second, so lines reach the
    #: browser far faster than the old 1.5 s client poll, without N clients issuing
    #: their own HTTP round-trips.
    _SSE_POLL_S = 0.5
    #: Disable proxy/CDN buffering so events are delivered as they are produced.
    _SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    def _last_event_offset(request: Request) -> int:
        """Resume offset from the browser's ``Last-Event-ID`` (0 on first connect)."""
        raw = request.headers.get("last-event-id")
        try:
            return int(raw) if raw else 0
        except (TypeError, ValueError):
            return 0

    async def _sse_log_stream(request: Request, fetch, start_offset: int):
        """SSE generator tailing a ``fetch(offset) -> LogChunk`` pull.

        Each non-empty delta is one ``message`` event whose ``id`` is the byte
        offset to resume from — the browser echoes it as ``Last-Event-ID`` on an
        automatic reconnect, so a dropped connection resumes exactly (no gap, no
        dupe). A terminal log ends with an ``eof`` event; an application error (e.g.
        the pod is gone with no durable copy) is sent as a ``streamerror`` event so
        the client renders it instead of the panel silently freezing. Deltas are
        JSON-encoded so embedded newlines can never break SSE framing.
        """
        offset = max(0, start_offset)
        yield ": open\n\n"  # prompt proxies to flush the response headers
        while not app.state.should_exit():
            if await request.is_disconnected():
                return
            try:
                chunk = await anyio.to_thread.run_sync(fetch, offset)
            except Exception as e:  # noqa: BLE001 - surface, never 500 the stream
                yield f"event: streamerror\ndata: {_json.dumps(str(e))}\n\n"
                yield "event: eof\ndata: {}\n\n"
                return
            if chunk.text:
                offset = chunk.next_offset
                yield f"id: {offset}\ndata: {_json.dumps(chunk.text)}\n\n"
            if chunk.eof:
                yield "event: eof\ndata: {}\n\n"
                return
            await anyio.sleep(_SSE_POLL_S)

    #: Poll cadence of the campaign-list stream's server-side loop.
    _SSE_LIST_POLL_S = 1.0

    async def _sse_campaign_list(request: Request):
        """SSE generator pushing the campaign list whenever it changes.

        A server-side loop over the same ``list_campaigns`` pull the CLI/MCP use, so
        the list has one source of truth (no second enumeration to drift). The full
        list is sent on connect — that is the client's initial state — and again on
        every change; quiet ticks send a heartbeat comment so proxies hold the
        connection. A dropped connection is resumed by the browser's native
        ``EventSource`` reconnect, which re-runs this handler and re-sends the list,
        so no client-side polling fallback is needed.
        """
        from robovast.service.interface import \
            ListCampaignsRequest  # pylint: disable=import-outside-toplevel
        yield ": open\n\n"
        last = None
        while not app.state.should_exit():
            if await request.is_disconnected():
                return
            try:
                resp = await anyio.to_thread.run_sync(
                    lambda: impl.list_campaigns(
                        ListCampaignsRequest(limit=100, offset=0)))
                encoded = _json.dumps(resp.model_dump(), default=str)
            except Exception as e:  # noqa: BLE001 - surface, never 500 the stream
                yield f"event: streamerror\ndata: {_json.dumps(str(e))}\n\n"
                return
            if encoded != last:
                last = encoded
                yield f"data: {encoded}\n\n"
            else:
                yield ": heartbeat\n\n"
            await anyio.sleep(_SSE_LIST_POLL_S)

    @app.get(Routes.HEALTHZ, tags=["meta"])
    def healthz() -> dict:
        """Liveness probe: answers ``{"ok": true}`` as soon as the app is serving."""
        return {"ok": True}

    #: Client hosts allowed to learn the service's filesystem roots. Real loopback
    #: addresses only — a harness name here would make the guard unverifiable by
    #: reading it, which is the one thing a security-relevant check must not be.
    _LOOPBACK = {"127.0.0.1", "::1", "localhost"}

    @app.get(Routes.VERSION, response_model=VersionInfo, tags=["meta"])
    def version(request: Request) -> VersionInfo:
        info = _guard(impl.version)
        # The roots are only useful to a caller on this machine, and only true for one:
        # over a tunnel or a port-forward the same path names the *server's* disk, which
        # the client cannot open. Advertising it there would be a path that looks
        # actionable and is not — so the address space is the only route it gets.
        host = request.client.host if request.client else ""
        if host not in _LOOPBACK:
            info.results_root = None
            info.sources_root = None
        return info

    @app.get(Routes.USAGE, response_model=ResourceUsage, tags=["meta"])
    def resource_usage(backend: str | None = None) -> ResourceUsage:
        return _guard(lambda: impl.resource_usage(backend))

    # -- authoring help (static; config editor) -----------------------------

    @app.get(Routes.CONFIG_SCHEMA, tags=["authoring"])
    def get_config_schema() -> dict:
        return _guard(impl.get_config_schema)

    @app.get(Routes.VARIATION_TYPES, response_model=VariationTypesResponse, tags=["authoring"])
    def list_variation_types() -> VariationTypesResponse:
        return _guard(impl.list_variation_types)

    @app.get(Routes.variation_asset("{name}", "{path:path}"), tags=["authoring"])
    def variation_asset(name: str, path: str):
        """Serve a variation plugin's web-preview asset (Module Federation remote).

        External variation plugins ship a built ``remoteEntry.js`` + chunks as
        package data under their ``WEB_PREVIEW`` dir; the config editor loads them
        at runtime. Built-in types have no assets (they render host-native).
        """
        from fastapi.responses import \
            FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(lambda: _resolve_variation_asset(name, path))))

    @app.get(Routes.panel_types_asset("{name}", "{path:path}"), tags=["authoring"])
    def panel_type_asset(name: str, path: str):
        """Serve a package-provided run-view panel's web asset (Module Federation remote).

        A panel plugin (entry-point group ``robovast.panel_types``) ships a built
        ``remoteEntry.js`` + chunks as package data under its ``WEB_PANEL`` dir; the run
        view loads them at runtime. Core built-in panels have no assets (host-native)."""
        from fastapi.responses import \
            FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(
            lambda: _resolve_plugin_asset("robovast.panel_types", name, path, "WEB_PANEL"))))

    @app.get(Routes.campaign_panel_asset("{campaign_id}", "{path:path}"), tags=["authoring"])
    def campaign_panel_asset(campaign_id: str, path: str):
        """Serve a user-authored ``custom`` panel's bundle, staged into the campaign's
        immutable ``_config/`` snapshot (Module Federation remoteEntry + chunks)."""
        from fastapi.responses import \
            FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(
            lambda: impl.resolve_campaign_panel_asset(campaign_id, path))))

    # -- workspaces ---------------------------------------------------------

    @app.post(Routes.WORKSPACES, response_model=WorkspaceInfo, tags=["workspaces"])
    def create_workspace(request: CreateWorkspaceRequest) -> WorkspaceInfo:
        return _guard(lambda: impl.create_workspace(request))

    @app.get(Routes.WORKSPACES, response_model=ListWorkspacesResponse, tags=["workspaces"])
    def list_workspaces() -> ListWorkspacesResponse:
        return _guard(impl.list_workspaces)

    @app.get(Routes.workspace("{workspace_id}"), response_model=WorkspaceInfo, tags=["workspaces"])
    def get_workspace(workspace_id: str) -> WorkspaceInfo:
        return _guard(lambda: impl.get_workspace(workspace_id))

    @app.delete(Routes.workspace("{workspace_id}"), response_model=ActionResult, tags=["workspaces"])
    def delete_workspace(workspace_id: str) -> ActionResult:
        return _guard(lambda: impl.delete_workspace(workspace_id))

    # -- validation / preview (config editor) -------------------------------

    @app.post(Routes.workspace_validate("{workspace_id}"), response_model=ValidationReport,
              tags=["workspaces"])
    def validate_project(
        workspace_id: str, path: str = Body("", embed=True)
    ) -> ValidationReport:
        return _guard(lambda: impl.validate_project(workspace_id, path))

    @app.post(Routes.workspace_preview("{workspace_id}"), response_model=PreviewResponse,
              tags=["workspaces"])
    def preview_configurations(
        workspace_id: str, max_configs: int = Body(0, embed=True),
        path: str = Body("", embed=True),
    ) -> PreviewResponse:
        return _guard(lambda: impl.preview_configurations(workspace_id, max_configs, path))

    # -- file side channel: grant + raw PUT ---------------------------------

    @app.post(Routes.UPLOADS, response_model=UploadGrant, tags=["uploads"])
    def create_upload(request: CreateUploadRequest) -> UploadGrant:
        """Grant a one-time PUT for a ``/sources`` address.

        Not under ``/workspaces/{id}/``: the request already names the workspace in its
        address, and a path segment that had to agree with it would be an argument the
        handler either ignores (it did) or has to re-check.
        """
        grant = _guard(lambda: impl.create_upload(request))
        grant.url = f"{Routes.upload(grant.token)}"
        return grant

    @app.put(Routes.UPLOAD, response_model=FileMeta, tags=["uploads"])
    async def put_upload(token: str, req: Request) -> FileMeta:
        """Redeem a one-time TTL token and store the raw body.

        This is the token-free path: the client streams bytes here directly
        (``curl -X PUT --data-binary @file``), so run files, notebooks and
        binaries never pass through an LLM's context.
        """
        body = await req.body()
        redeem = getattr(impl, "redeem_upload", None)
        if redeem is None:
            raise HTTPException(status_code=501,
                                detail="this service has no workspace store")
        return _guard(lambda: redeem(token, body))

    # -- files: one address space -------------------------------------------
    #
    # ``/results/<campaign>/<path>`` and ``/sources/<workspace>/<path>``: the address a
    # caller passes to ``read_file`` is literally the URL that serves it (see
    # :mod:`robovast.common.file_address`). Content lives in its own namespaces rather
    # than under ``/campaigns/{id}/`` or ``/workspaces/{id}/`` because those are control
    # namespaces whose literal segments would shadow user-chosen file names.
    #
    # ``/results`` is read-only by *registration*: no PUT/POST/DELETE route exists under
    # it, so a write is a 405 from the router rather than a check each handler must
    # remember. The permission is the prefix, dispatched once.
    #
    # A trailing slash means "directory" — ``/results/<c>/nav/`` lists, ``/results/<c>/nav``
    # reads — and a listing suffixes its directory entries the same way, so the shape a
    # caller sees in a response is the shape it sends back. A bare owner
    # (``/results/<c>``) is registered separately and always lists: it can only be a
    # directory, and 404-ing the most obvious URL in the address space would be a poor
    # way to teach it.

    def _serve_address(address: str, as_: str, lines: int, offset: int,
                       recursive: bool, detail: bool, limit: int):
        if file_address.is_directory(address):
            if as_:
                raise HTTPException(
                    status_code=400,
                    detail=f"{address!r} is a directory; 'as' selects a "
                           "representation of a file")
            return _guard(lambda: impl.list_files(address, recursive, detail,
                                                  offset, limit))
        if as_ == "text":
            return _guard(lambda: impl.read_file(address, lines, offset))
        import mimetypes  # pylint: disable=import-outside-toplevel

        from fastapi.responses import (  # pylint: disable=import-outside-toplevel
            FileResponse, Response)
        media_type = mimetypes.guess_type(address)[0] or "application/octet-stream"

        # Stream from disk where the lane has a disk. A campaign's rosbag runs to tens of
        # megabytes and beyond, and buffering it whole per request costs that much service
        # memory to hand back bytes it never looks at. FileResponse also brings Range and
        # conditional requests, which is what lets a browser seek a .webm rather than
        # download it before playing.
        local = getattr(impl, "local_file", None)
        if local is not None:
            return _guard(lambda: FileResponse(local(address), media_type=media_type))
        # A cluster campaign's results are object-store entries with no path to stream
        # from; ranged object reads are a separate change.
        return Response(content=_guard(lambda: impl.read_file_bytes(address)),
                        media_type=media_type)

    @app.get(Routes.RESULTS + "/{campaign_id}/{path:path}", tags=["files"])
    def get_results_file(campaign_id: str, path: str,
                         as_: Literal["", "text"] = Query("", alias="as"), lines: int = 200,
                         offset: int = 0, recursive: bool = False,
                         detail: bool = False, limit: int = 100):
        """A campaign's outputs: one file's bytes, its text page, or a directory listing."""
        return _serve_address(file_address.format_address(
            file_address.RESULTS, campaign_id, path), as_, lines,
                              offset, recursive, detail, limit)

    @app.get(Routes.SOURCES + "/{workspace_id}/{path:path}", tags=["files"])
    def get_sources_file(workspace_id: str, path: str,
                         as_: Literal["", "text"] = Query("", alias="as"), lines: int = 200,
                         offset: int = 0, recursive: bool = False,
                         detail: bool = False, limit: int = 100):
        """A workspace's authored inputs — same representations as ``/results``."""
        return _serve_address(file_address.format_address(
            file_address.SOURCES, workspace_id, path), as_, lines,
                              offset, recursive, detail, limit)

    @app.get(Routes.RESULTS + "/{campaign_id}", tags=["files"])
    def list_results_root(campaign_id: str, recursive: bool = False,
                          detail: bool = False, offset: int = 0, limit: int = 100):
        """A campaign root, with or without the trailing slash — always a listing."""
        return _guard(lambda: impl.list_files(
            file_address.format_address(file_address.RESULTS, campaign_id),
            recursive, detail, offset, limit))

    @app.get(Routes.SOURCES + "/{workspace_id}", tags=["files"])
    def list_sources_root(workspace_id: str, recursive: bool = False,
                          detail: bool = False, offset: int = 0, limit: int = 100):
        """A workspace root, with or without the trailing slash — always a listing."""
        return _guard(lambda: impl.list_files(
            file_address.format_address(file_address.SOURCES, workspace_id),
            recursive, detail, offset, limit))

    @app.put(Routes.SOURCES + "/{workspace_id}/{path:path}", response_model=FileMeta,
             tags=["files"])
    def put_sources_file(workspace_id: str, path: str,
                         content: str = Body("", embed=True)) -> FileMeta:
        """Write a ``.vast``/``.osc`` file inline (other types → the upload grant)."""
        return _guard(lambda: impl.write_file(WriteFileRequest(
            address=file_address.format_address(file_address.SOURCES, workspace_id,
                                                path),
            content=content)))

    @app.post(Routes.SOURCES + "/{workspace_id}/{path:path}", response_model=FileMeta,
              tags=["files"])
    def edit_sources_file(workspace_id: str, path: str,
                          old_string: str = Body("", embed=True),
                          new_string: str = Body("", embed=True)) -> FileMeta:
        """Replace a unique substring — the token-cheap validate→fix loop."""
        return _guard(lambda: impl.edit_file(EditFileRequest(
            address=file_address.format_address(file_address.SOURCES, workspace_id,
                                                path),
            old_string=old_string, new_string=new_string)))

    @app.delete(Routes.SOURCES + "/{workspace_id}/{path:path}", response_model=ActionResult,
                tags=["files"])
    def delete_sources_file(workspace_id: str, path: str) -> ActionResult:
        """Delete a file in a workspace. There is no such route under ``/results``:
        result files are read-only by registration, so deleting one is a 405."""
        return _guard(lambda: impl.delete_file(
            file_address.format_address(file_address.SOURCES, workspace_id, path)))

    @app.post(Routes.CAMPAIGNS, response_model=CampaignRef, tags=["campaigns"])
    def create_campaign(request: CreateCampaignRequest) -> CampaignRef:
        return _guard(lambda: impl.create_campaign(request))

    @app.get(Routes.CAMPAIGNS, response_model=ListCampaignsResponse, tags=["campaigns"])
    def list_campaigns(limit: int = 20, offset: int = 0) -> ListCampaignsResponse:
        from robovast.service.interface import \
            ListCampaignsRequest  # pylint: disable=import-outside-toplevel
        return _guard(
            lambda: impl.list_campaigns(ListCampaignsRequest(limit=limit, offset=offset)))

    @app.get(Routes.CAMPAIGNS_STREAM, tags=["campaigns"])
    async def stream_campaigns(request: Request):
        """Server-sent events: the campaign list, pushed on every change."""
        return StreamingResponse(
            _sse_campaign_list(request),
            media_type="text/event-stream", headers=_SSE_HEADERS)

    @app.get(Routes.campaign_status("{campaign_id}"), response_model=Status, tags=["campaigns"])
    def get_status(campaign_id: str) -> Status:
        return _guard(lambda: impl.get_status(campaign_id))

    @app.get(Routes.campaign_logs("{campaign_id}"), response_model=LogChunk, tags=["campaigns"])
    def get_campaign_logs(campaign_id: str, offset: int = 0) -> LogChunk:
        return _guard(lambda: impl.get_campaign_logs(campaign_id, offset))

    @app.get(Routes.campaign_jobs("{campaign_id}"), response_model=ListJobsResponse,
             tags=["campaigns"])
    def list_jobs(campaign_id: str) -> ListJobsResponse:
        return _guard(lambda: impl.list_jobs(campaign_id))

    @app.get(Routes.job_log("{campaign_id}"), response_model=LogChunk, tags=["campaigns"])
    def get_job_log(campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        return _guard(lambda: impl.get_job_log(campaign_id, job_name, offset))

    @app.get(Routes.campaign_logs_stream("{campaign_id}"), tags=["campaigns"])
    async def stream_campaign_logs(campaign_id: str, request: Request):
        """Server-sent events: a campaign's controller log, tailed live. Resumable —
        send ``Last-Event-ID`` to continue from the last line received."""
        return StreamingResponse(
            _sse_log_stream(
                request,
                lambda off: impl.get_campaign_logs(campaign_id, off),
                _last_event_offset(request)),
            media_type="text/event-stream", headers=_SSE_HEADERS)

    @app.get(Routes.job_log_stream("{campaign_id}"), tags=["campaigns"])
    async def stream_job_log(campaign_id: str, request: Request, job_name: str):
        """Server-sent events: one running job's log, tailed live (``Last-Event-ID``
        resumes). A finished job whose pod was garbage-collected has no live log."""
        return StreamingResponse(
            _sse_log_stream(
                request,
                lambda off: impl.get_job_log(campaign_id, job_name, off),
                _last_event_offset(request)),
            media_type="text/event-stream", headers=_SSE_HEADERS)

    @app.post(Routes.campaign_stop("{campaign_id}"), response_model=ActionResult, tags=["campaigns"])
    def stop(campaign_id: str) -> ActionResult:
        return _guard(lambda: impl.stop(campaign_id))

    @app.post(Routes.CLEANUP_DATA, response_model=ActionResult, tags=["campaigns"])
    def cleanup_campaign_data(request: "CleanupDataRequest | None" = None) -> ActionResult:
        # Body optional: no body means "all finished campaigns" (live ones skipped).
        return _guard(lambda: impl.cleanup_campaign_data(request or CleanupDataRequest()))

    @app.delete(Routes.campaign("{campaign_id}"), response_model=ActionResult, tags=["campaigns"])
    def delete_campaign(campaign_id: str) -> ActionResult:
        # Wholesale delete of one campaign's durable home. Refuses a running
        # campaign (409 via _guard's RuntimeError mapping); idempotent otherwise.
        return _guard(lambda: impl.delete_campaign(campaign_id))

    # -- image builds -------------------------------------------------------

    @app.post(Routes.IMAGE_BUILDS, response_model=ImageBuildRef, tags=["image-builds"])
    def build_image(request: BuildImageRequest) -> ImageBuildRef:
        return _guard(lambda: impl.build_image(request))

    @app.get(Routes.image_build_status("{build_id}"), response_model=ImageBuildStatus,
             tags=["image-builds"])
    def get_image_build_status(build_id: str) -> ImageBuildStatus:
        return _guard(lambda: impl.get_image_build_status(build_id))

    @app.get(Routes.image_build_log("{build_id}"), response_model=LogChunk, tags=["image-builds"])
    def get_image_build_log(build_id: str, offset: int = 0) -> LogChunk:
        return _guard(lambda: impl.get_image_build_log(build_id, offset))

    # -- container exec (diagnostic; produces no campaign) ------------------

    @app.post(Routes.EXEC, response_model=ExecResult, tags=["exec"])
    def exec_in_container(request: ExecRequest) -> ExecResult:
        return _guard(lambda: impl.exec_in_container(request))

    @app.delete(Routes.EXEC, response_model=ExecStopResult, tags=["exec"])
    def stop_exec_container(backend: str = "") -> ExecStopResult:
        return _guard(lambda: impl.stop_exec_container(backend or None))

    @app.get(Routes.campaign_archive("{campaign_id}"), tags=["results"])
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

    @app.get(Routes.campaign_postprocessing("{campaign_id}"), tags=["results"])
    def get_postprocessing(campaign_id: str):
        return _guard(lambda: impl.get_postprocessing(campaign_id))

    @app.post(Routes.campaign_postprocessing("{campaign_id}"), tags=["results"])
    def update_postprocessing(campaign_id: str, request: UpdatePostprocessingRequest):
        return _guard(lambda: impl.update_postprocessing(request))

    @app.get(Routes.campaign_postprocessing_source("{campaign_id}"),
             response_model=PostprocessingSource, tags=["results"])
    def get_postprocessing_source(campaign_id: str) -> PostprocessingSource:
        return _guard(lambda: impl.get_postprocessing_source(campaign_id))

    @app.post(Routes.campaign_postprocessing_source("{campaign_id}"),
              response_model=PostprocessingSource, tags=["results"])
    def update_postprocessing_source(
        campaign_id: str, request: UpdatePostprocessingSourceRequest
    ) -> PostprocessingSource:
        return _guard(lambda: impl.update_postprocessing_source(request))

    @app.post(Routes.campaign_postprocessing_run("{campaign_id}"), response_model=ActionResult,
              tags=["results"])
    def run_postprocessing(campaign_id: str, request: RunPostprocessingRequest) -> ActionResult:
        return _guard(lambda: impl.run_postprocessing(request))

    @app.post(Routes.campaign_share_run("{campaign_id}"), response_model=ActionResult, tags=["results"])
    def run_share(campaign_id: str, request: RunShareRequest) -> ActionResult:
        return _guard(lambda: impl.run_share(request))

    # -- results data query (eval viewer) -----------------------------------

    @app.get(Routes.campaign_describe("{campaign_id}"), response_model=DataDescribe, tags=["results"])
    def describe_campaign_data(campaign_id: str) -> DataDescribe:
        return _guard(lambda: impl.describe_campaign_data(campaign_id))

    @app.get(Routes.campaign_data_status("{campaign_id}"), response_model=CampaignDataStatus,
             tags=["results"])
    def campaign_data_status(campaign_id: str) -> CampaignDataStatus:
        """Whether querying this campaign transfers data first — ask *before* the wait.

        Cheap by contract (two metadata lookups). On a cluster campaign whose databases
        are not cached yet, a first ``/describe`` or ``/query`` fetches them from the
        object store inside the request; this says so in advance, so a client can show
        why instead of appearing to hang.
        """
        return _guard(lambda: impl.campaign_data_status(campaign_id))

    @app.post(Routes.campaign_query("{campaign_id}"), response_model=DataQueryResult, tags=["results"])
    def query_campaign_data_sql(
        campaign_id: str, sql: str = Body(..., embed=True),
        max_rows: int = Body(500, embed=True),
        extra_campaign_ids: list[str] = Body(default_factory=list, embed=True),
    ) -> DataQueryResult:
        return _guard(lambda: impl.query_campaign_data_sql(
            campaign_id, sql, max_rows, extra_campaign_ids))

    @app.get(Routes.campaign_query_csv("{campaign_id}"), tags=["results"])
    def query_campaign_data_csv(campaign_id: str, sql: str,
                                extra_campaign_ids: str = ""):
        """Stream the same read-only ``SELECT`` as CSV, with no row cap.

        The JSON query clamps at 5000 rows and says ``truncated``; this is where the rest
        of the result lives. Streamed, so a result larger than memory is fine at both
        ends, and an MCP tool can hand over this URL instead of spending context on rows.
        """
        from fastapi.responses import \
            StreamingResponse  # pylint: disable=import-outside-toplevel
        extras = [c for c in extra_campaign_ids.split(",") if c]
        # Called inside _guard so a rejected (non-read) query is a 400 with the same
        # message the JSON path gives, rather than a 500 mid-stream.
        rows = _guard(lambda: impl.stream_campaign_query_csv(campaign_id, sql, extras))
        return StreamingResponse(
            rows, media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="{campaign_id}-query.csv"'})

    @app.get(Routes.campaign_plots("{campaign_id}"), response_model=CampaignPlotsResponse,
             tags=["results"])
    def list_campaign_plots(campaign_id: str) -> CampaignPlotsResponse:
        return _guard(lambda: impl.list_campaign_plots(campaign_id))

    @app.get(Routes.campaign_panels("{campaign_id}"), response_model=CampaignPanelsResponse,
             tags=["results"])
    def list_campaign_panels(campaign_id: str) -> CampaignPanelsResponse:
        return _guard(lambda: impl.list_campaign_panels(campaign_id))

    @app.get(Routes.campaign_panels_source("{campaign_id}"), response_model=PanelsSource,
             tags=["results"])
    def get_panels_source(campaign_id: str) -> PanelsSource:
        return _guard(lambda: impl.get_panels_source(campaign_id))

    @app.post(Routes.campaign_panels_source("{campaign_id}"), response_model=PanelsSource,
              tags=["results"])
    def update_panels_source(
        campaign_id: str, request: UpdatePanelsSourceRequest
    ) -> PanelsSource:
        return _guard(lambda: impl.update_panels_source(request))

    @app.get(Routes.campaign_visualizations("{campaign_id}"),
             response_model=CampaignVisualizationsResponse, tags=["results"])
    def list_campaign_visualizations(campaign_id: str) -> CampaignVisualizationsResponse:
        return _guard(lambda: impl.list_campaign_visualizations(campaign_id))

    # Sync ``def`` so Starlette runs the (blocking, multi-second) notebook execution in
    # the threadpool, off the event loop. Returns the executed notebook's HTML verbatim
    # for the Explorer's iframe; a cache hit (see notebook_render) makes repeats instant.
    @app.get(Routes.campaign_notebook("{campaign_id}"), tags=["results"])
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

    # -- package-provided run-data endpoints --------------------------------
    # Installed ``robovast.service_endpoints`` plugins each get a route
    # ``GET /campaigns/{id}/<name>?config_name&run_id&…`` → JSON, dispatched to the plugin
    # handler with a RunDataContext. Registered after the core routes and before the SPA
    # catch-all mount. Cluster-transparent: dispatch resolves the campaign dir via
    # ``impl.resolve_data_dir`` (ClusterService fetches from the object store).
    from robovast.service.endpoint_plugin import (  # pylint: disable=import-outside-toplevel
        RunDataContext, load_service_endpoints)

    def _make_endpoint_route(endpoint):
        def route(campaign_id: str, request: Request):
            ctx = RunDataContext(
                campaign_id=campaign_id,
                params=dict(request.query_params),
                data_dir=str(impl.resolve_data_dir(campaign_id)))
            return _guard(lambda: endpoint.handle(ctx))
        return route

    for _name, _endpoint in load_service_endpoints().items():
        # ``_name`` may contain '/' (namespacing, e.g. "nav/costmap") → a nested path.
        app.add_api_route(
            f"/campaigns/{{campaign_id}}/{_name}",
            _make_endpoint_route(_endpoint), methods=["GET"],
            # Tagged like every other route so the generated route table (and the
            # OpenAPI page) group them; which of these exist depends on what is
            # installed, so the tag says so rather than implying a fixed set.
            tags=["plugin-endpoints"],
            summary=f"Run data from the {_name!r} endpoint plugin.")

    _mount_ui(app)
    return app


def _resolve_plugin_asset(group: str, name: str, rel_path: str, asset_attr: str):
    """Resolve a plugin's web asset file (Module Federation bundle), confined to the
    asset dir the plugin class declares via *asset_attr* (relative to the class's module).

    Shared by variation-type web previews (``WEB_PREVIEW``) and package-provided run-view
    panels (``WEB_PANEL``). Raises ``KeyError`` (→ 404) for an unknown *name* / no asset
    attr / missing file, ``ValueError`` (→ 400) if *rel_path* escapes the asset directory.
    """
    import inspect  # pylint: disable=import-outside-toplevel
    import os  # pylint: disable=import-outside-toplevel
    from importlib.metadata import \
        entry_points  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    cls = next((ep.load() for ep in entry_points(group=group)
                if ep.name == name), None)
    if cls is None:
        raise KeyError(f"unknown {group} entry {name!r}")
    asset_rel = getattr(cls, asset_attr, None)
    if not asset_rel:
        raise KeyError(f"{group} entry {name!r} has no {asset_attr}")
    asset_root = (Path(inspect.getfile(cls)).resolve().parent / asset_rel).resolve()
    target = (asset_root / rel_path).resolve()
    if target != asset_root and not str(target).startswith(str(asset_root) + os.sep):
        raise ValueError("path escapes the asset directory")
    if not target.is_file():
        raise KeyError(f"asset not found: {rel_path}")
    return target


def _resolve_variation_asset(name: str, rel_path: str):
    """A variation type's web-preview asset (see :func:`_resolve_plugin_asset`)."""
    return _resolve_plugin_asset("robovast.variation_types", name, rel_path, "WEB_PREVIEW")


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
    # Drive uvicorn via an explicit Server so the SSE generators can probe
    # ``should_exit`` (set when a Ctrl+C begins shutdown, before the connection
    # wait) and close their streams instead of hanging it. ``timeout_graceful_
    # shutdown`` is a backstop for any other lingering connection.
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level,
                            log_config=_quiet_access_log_config(),
                            timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    app.state.should_exit = lambda: server.should_exit
    server.run()


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
