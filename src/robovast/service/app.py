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

This generalizes the per-campaign FastAPI control channel in
:mod:`robovast.execution.control_server` into a persistent, campaign-spanning
service. FastAPI auto-emits OpenAPI at ``/docs``, so the same contract serves the
CLI, the MCP server, and a future web UI. ``fastapi``/``uvicorn`` are imported
lazily so importing this module stays cheap.
"""

import hmac
import logging
from pathlib import Path
from typing import List, Literal, Optional

from robovast.client import file_address
from robovast.service import auth
from robovast.service.interface import (ActionResult, BuildImageRequest, CampaignDataStatus,
                                        CampaignPanelsResponse, CampaignPlotsResponse, CampaignRef,
                                        CampaignVisualizationsResponse, CleanupDataRequest,
                                        CreateCampaignRequest, CreateUploadRequest,
                                        CreateWorkspaceRequest, DataDescribe, DataQueryResult,
                                        EditFileRequest, ExecRequest, ExecResult, ExecStopResult,
                                        FileMeta, ImageBuildRef, ImageBuildStatus, ImageResolution,
                                        ImportCampaignRequest, ShareListing,
                                        JobState, ListCampaignsResponse, ListJobsResponse,
                                        ListWorkspacesResponse, LogChunk, PanelsSource,
                                        PostprocessingSource, PreviewResponse, ResourceUsage,
                                        RetriggerReport, RobovastInterface, Routes,
                                        RunPostprocessingRequest,
                                        RunShareRequest, SceneStatus, SearchHistory,
                                        StagedArchive, Status,
                                        UpdatePanelsSourceRequest, UpdatePostprocessingRequest,
                                        UpdatePostprocessingSourceRequest, UploadGrant,
                                        ValidationReport, VariationTypesResponse, VersionInfo,
                                        WorkOrder,
                                        WorkspaceInfo, WorldDescription, WriteFileRequest)

logger = logging.getLogger(__name__)

# deliberately after the logger, see above
# pylint: disable-next=wrong-import-position
from robovast.service.interface import (  # noqa: F401
    DEFAULT_PORT)  # re-exported: callers import it from here


def _sse_pull_limiter():
    """Worker-thread budget for SSE stream pulls, separate from anyio's shared default.

    Sized well under anyio's 40-token default so the streams cannot consume the pool the
    ~56 sync routes are served from; see ``_pull_or_exit``. Created eagerly (anyio 4 binds
    the adapter to a backend on first use, not at construction) so it is one shared object
    across every request rather than a per-call limiter, which would bound nothing.

    Caveat, deliberately not papered over: this bounds *concurrent waiters*, not stalled
    threads. ``_pull_or_exit`` abandons its thread on cancellation, and anyio releases the
    token at that moment while the thread is still inside the blocking call — so a stream
    that keeps dropping and reconnecting can leave more live threads than there are tokens.
    What actually bounds those is the pull's own timeout budget (see
    ``in_pod_storage.storage_client_for(interactive=True)``, ~10 s); this limiter's job is
    isolation between subsystems, not a hard thread cap.
    """
    import anyio  # pylint: disable=import-outside-toplevel
    return anyio.CapacityLimiter(8)


_SSE_PULL_LIMITER = _sse_pull_limiter()

#: Routes FastAPI registers for itself. Real, but they describe FastAPI, not this service.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

#: Where the MCP server answers when :func:`build_app` mounts it (see ``mount_mcp``).
MCP_PATH = "/mcp"

#: The login page. Deliberately server-rendered and dependency-free: it has to work
#: before the SPA loads, and the SPA's bundle is behind the very session this page
#: issues. The name field is optional — an unattributed campaign records nothing rather
#: than an invented placeholder.
_LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RoboVAST</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; margin: 0; }
  form { display: grid; gap: .75rem; min-width: min(22rem, 90vw); }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  label { display: grid; gap: .25rem; font-size: .875rem; }
  input { font: inherit; padding: .5rem; }
  button { font: inherit; padding: .5rem; cursor: pointer; }
  .hint { font-size: .8125rem; opacity: .7; margin: 0; }
  .err { color: #b3261e; font-size: .875rem; margin: 0; }
</style></head>
<body><form method="post" action="/login">
  <h1>RoboVAST</h1>
  <!--error-->
  <label>Access token
    <input type="password" name="token" autofocus required autocomplete="current-password">
  </label>
  <label>Your name <span class="hint">(optional, shown on campaigns you start)</span>
    <input type="text" name="name" autocomplete="nickname">
  </label>
  <input type="hidden" name="next" value="{{next}}">
  <button type="submit">Sign in</button>
</form></body></html>
"""


def _html_escape(value: str) -> str:
    """Escape a value interpolated into the login page (the ``next`` target)."""
    import html
    return html.escape(value, quote=True)

#: Route groups, in the order a reader wants them: what a client does first, reference
#: last. Every route carries one as its ``tags=`` — the grouping is authored at the route
#: so the generated documentation and the OpenAPI page agree, and a new route cannot land
#: ungrouped. Kept here rather than in the docs extension because it is a property of the
#: API, and :mod:`tests.service.test_route_docs` checks the two stay in step.
ROUTE_TAG_ORDER = ("meta", "authoring", "workspaces", "uploads", "files", "campaigns",
                   "image-builds", "exec", "results", "plugin-endpoints")


def api_routes(app):
    """The routes this service defines: no FastAPI extras, no SPA mount, no MCP."""
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or path in ("/", MCP_PATH) or path in FRAMEWORK_PATHS:
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


def build_app(impl: RobovastInterface, mount_mcp: bool = True,
              auth_token: str | None = None):
    """Build the FastAPI app bound to *impl* (lazy import; needs ``fastapi``).

    ``mount_mcp`` puts the MCP server's own ASGI app at ``/mcp`` on this same app —
    see :func:`_build_mcp_app` — so a client only has one port to reach for the web UI,
    the REST API, *and* MCP tools together. It is inside the authentication gate like
    everything else, which is the reason the gate is ASGI middleware rather than a
    FastAPI dependency: a mounted sub-app does not run the parent's dependencies.

    ``auth_token`` is the shared secret. **There is no way to build an app without
    one**: an unset token is minted rather than disabling the check, so development and
    production run the same code and "reachable but open" is not a state you can reach
    by forgetting something. The resolved value is on ``app.state.auth_token`` so
    ``serve()`` can print a login URL for an ephemeral one.
    """
    from contextlib import asynccontextmanager  # pylint: disable=import-outside-toplevel
    from contextlib import AsyncExitStack

    import anyio  # pylint: disable=import-outside-toplevel
    from fastapi import (Body, FastAPI, HTTPException,  # pylint: disable=import-outside-toplevel
                         Query, Request)

    mcp_app = _build_mcp_app(impl) if mount_mcp else None

    @asynccontextmanager
    async def _lifespan(_app):
        """Run ``impl.shutdown()`` on service teardown (Ctrl+C on ``vast serve``).

        uvicorn runs the lifespan shutdown when it catches SIGINT, but it does not
        wait on the daemon worker threads a local campaign runs on. Stopping them
        here — off the event loop, since the join blocks — lets a Ctrl+C tear down
        the running campaign's containers instead of orphaning them.

        When the MCP app is mounted, its own lifespan has to run too — FastMCP's
        session manager is only started/stopped there, and a mount does not run a
        sub-app's lifespan on its own (see ``fastmcp.server.http``'s warning about
        exactly this).
        """
        async with AsyncExitStack() as stack:
            if mcp_app is not None:
                await stack.enter_async_context(mcp_app.lifespan(_app))
            yield
            try:
                await anyio.to_thread.run_sync(impl.shutdown)
            except Exception:  # noqa: BLE001 - teardown must never mask the real exit
                logger.exception("error during service shutdown")

    app = FastAPI(title="robovast-service", docs_url="/docs", lifespan=_lifespan)

    auth_token, _ephemeral = auth.resolve_token(auth_token)
    app.state.auth_token = auth_token
    app.add_middleware(auth.AuthMiddleware, token=auth_token)

    if mcp_app is not None:
        # Not ``app.mount()``: a ``Mount`` requires a literal trailing slash after its
        # prefix to match anything (Starlette compiles it as ``<prefix>/{path:path}``), so
        # a client hitting the bare ``/mcp`` — the URL FastMCP's own defaults and every
        # doc here use — gets a 404/405 instead of the server. A plain ``Route`` has no
        # such requirement: ``methods=None`` delegates every method straight to
        # ``mcp_app`` (itself, middleware included — nothing is unwrapped) at the exact
        # path FastMCP already anchored it to.
        from starlette.routing import Route  # pylint: disable=import-outside-toplevel
        app.router.routes.append(Route(MCP_PATH, mcp_app, methods=None))

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
        from robovast.common.errors import \
            ObjectStoreUnreachableError  # pylint: disable=import-outside-toplevel
        try:
            return fn()
        except ValueError as e:            # bad input / not-initialized
            raise HTTPException(status_code=400, detail=str(e)) from e
        except KeyError as e:              # unknown id
            # ``str(KeyError("x"))`` is ``"'x'"``; take the message itself so the
            # detail is not delivered wrapped in stray quotes.
            detail = e.args[0] if e.args else str(e)
            raise HTTPException(status_code=404, detail=str(detail)) from e
        except ObjectStoreUnreachableError as e:
            # Before the RuntimeError arm it subclasses: nothing about an unanswering
            # store is a conflict, and a 503 tells a client the call is worth retrying.
            # The message is already the whole diagnosis, so no traceback is logged.
            logger.warning("%s", e)
            raise HTTPException(status_code=503, detail=str(e)) from e
        except RuntimeError as e:          # conflict (e.g. single-flight)
            raise HTTPException(status_code=409, detail=str(e)) from e

    # -- SSE log streaming --------------------------------------------------
    # The browser streams logs over Server-Sent Events; MCP/CLI keep the pull
    # endpoints (``.../logs``, ``.../job-log``). Both share the one assembly seam:
    # an SSE stream is just a server-side loop over the same ``LogChunk`` pull, so
    # there is no second implementation of assembly/offset to drift.
    import json as _json  # pylint: disable=import-outside-toplevel

    from fastapi.responses import StreamingResponse  # pylint: disable=import-outside-toplevel

    #: Poll cadence of the server-side tail loop. Sub-second, so lines reach the
    #: browser far faster than the old 1.5 s client poll, without N clients issuing
    #: their own HTTP round-trips.
    _sse_poll_s = 0.5
    #: Disable proxy/CDN buffering so events are delivered as they are produced.
    _sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    def _last_event_offset(request: Request) -> int:
        """Resume offset from the browser's ``Last-Event-ID`` (0 on first connect)."""
        raw = request.headers.get("last-event-id")
        try:
            return int(raw) if raw else 0
        except (TypeError, ValueError):
            return 0

    #: How often the shutdown watchdog re-checks while a pull is in flight.
    _sse_exit_poll_s = 0.05

    #: What a stream sends on a tick that had nothing to report.
    #:
    #: A named event and not the SSE comment (``: heartbeat``) it replaces, because a
    #: comment is invisible to ``EventSource``: it holds proxies open and tells the
    #: browser nothing. Without a frame the client can see, a stream that is merely
    #: quiet and one whose socket died in a suspended laptop or a torn-down
    #: ``kubectl port-forward`` look identical — no error, ``readyState`` still OPEN,
    #: and never another byte. That zombie is what leaves a tab showing a campaign as
    #: still running long after it finished, so the cure is to keep saying "alive" and
    #: let the client reconnect when we stop.
    _sse_heartbeat = "event: heartbeat\ndata: {}\n\n"

    async def _pull_or_exit(pull):
        """Run the blocking ``pull()`` off the event loop, abandoning it on shutdown.

        The pulls behind these streams are network I/O — a cluster job log is an S3
        read over a ``kubectl port-forward``, which takes tens of seconds when the
        tunnel has stalled. Simply awaiting one means a Ctrl+C is not noticed until it
        returns: the stream then misses uvicorn's graceful-shutdown deadline, uvicorn
        cancels the response task, and (because the thread cannot be cancelled) the
        cancellation only lands after the pull finally finishes, logged as an
        "Exception in ASGI application" traceback *after* the server has already
        stopped. So a watchdog cancels the wait the moment ``should_exit`` flips and
        the worker thread is abandoned — it is a daemon and dies with the process.

        Returns the pulled value, ``None`` if shutdown won the race, or the exception
        the pull raised. The exception is *returned* rather than raised because a task
        group would wrap it in an ``ExceptionGroup``, hiding the message the caller
        has to put on the wire.

        Runs against :data:`_SSE_PULL_LIMITER` rather than anyio's shared 40-token default,
        so these streams cannot starve the rest of the API. Nearly every route here is a
        sync ``def`` served from that same pool, and stream pulls are the requests most
        likely to block for a long time *and* the most numerous — one per open browser tab,
        re-issued on a timer, each auto-reconnecting when it drops. Left sharing the default
        pool, a slow object store let them take every token, at which point unrelated
        endpoints stopped answering too. A separate pool converts that into "the streams are
        slow" instead of "the service is down".
        """
        outcome = None

        async def _watch():
            while not app.state.should_exit():
                await anyio.sleep(_sse_exit_poll_s)
            task_group.cancel_scope.cancel()

        async def _run():
            nonlocal outcome
            try:
                outcome = await anyio.to_thread.run_sync(
                    pull, abandon_on_cancel=True, limiter=_SSE_PULL_LIMITER)
            except Exception as exc:  # noqa: BLE001 - handed to the caller to report
                outcome = exc
            task_group.cancel_scope.cancel()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_watch)
            task_group.start_soon(_run)
        return outcome

    async def _sse_log_stream(request: Request, fetch, start_offset: int):
        """SSE generator tailing a ``fetch(offset) -> LogChunk`` pull.

        Each non-empty delta is one ``message`` event whose ``id`` is the byte
        offset to resume from — the browser echoes it as ``Last-Event-ID`` on an
        automatic reconnect, so a dropped connection resumes exactly (no gap, no
        dupe). A terminal log ends with an ``eof`` event; an application error (e.g.
        the pod is gone with no durable copy) is sent as a ``streamerror`` event so
        the client renders it instead of the panel silently freezing. Deltas are
        JSON-encoded so embedded newlines can never break SSE framing.

        A tick that produced no new bytes sends a ``heartbeat`` — see
        :data:`_sse_heartbeat`, which is what lets the client tell a log nothing is
        being written to from one whose connection has quietly died.
        """
        offset = max(0, start_offset)
        yield ": open\n\n"  # prompt proxies to flush the response headers
        while not app.state.should_exit():
            if await request.is_disconnected():
                return
            chunk = await _pull_or_exit(lambda: fetch(offset))
            if chunk is None:  # shutting down — close the stream, don't wait
                return
            if isinstance(chunk, Exception):  # surface, never 500 the stream
                yield f"event: streamerror\ndata: {_json.dumps(str(chunk))}\n\n"
                yield "event: eof\ndata: {}\n\n"
                return
            if chunk.text:
                offset = chunk.next_offset
                yield f"id: {offset}\ndata: {_json.dumps(chunk.text)}\n\n"
            else:
                yield _sse_heartbeat
            if chunk.eof:
                yield "event: eof\ndata: {}\n\n"
                return
            await anyio.sleep(_sse_poll_s)

    #: Poll cadence of the campaign-list stream's server-side loop.
    _sse_list_poll_s = 1.0

    async def _sse_campaign_list(request: Request):
        """SSE generator pushing the campaign list whenever it changes.

        A server-side loop over the same ``list_campaigns`` pull the CLI/MCP use, so
        the list has one source of truth (no second enumeration to drift). The full
        list is sent on connect — that is the client's initial state — and again on
        every change; quiet ticks send a ``heartbeat`` event (see
        :data:`_sse_heartbeat`). A dropped connection is resumed by the browser's
        native ``EventSource`` reconnect, which re-runs this handler and re-sends the
        list; a connection that died without the browser noticing is caught by the
        client's own heartbeat watchdog, so no polling fallback is needed either way.
        """
        from robovast.service.interface import \
            ListCampaignsRequest  # pylint: disable=import-outside-toplevel
        yield ": open\n\n"
        last = None
        while not app.state.should_exit():
            if await request.is_disconnected():
                return
            # Encoded on the worker thread with the pull it came from, so a
            # serialization failure takes the same streamerror path as a failed pull.
            encoded = await _pull_or_exit(
                lambda: _json.dumps(
                    impl.list_campaigns(
                        ListCampaignsRequest(limit=100, offset=0)).model_dump(),
                    default=str))
            if encoded is None:  # shutting down — close the stream, don't wait
                return
            if isinstance(encoded, Exception):  # surface, never 500 the stream
                yield f"event: streamerror\ndata: {_json.dumps(str(encoded))}\n\n"
                return
            if encoded != last:
                last = encoded
                yield f"data: {encoded}\n\n"
            else:
                yield _sse_heartbeat
            await anyio.sleep(_sse_list_poll_s)

    @app.get(Routes.HEALTHZ, tags=["meta"])
    def healthz() -> dict:
        """Liveness probe: answers ``{"ok": true}`` as soon as the app is serving."""
        return {"ok": True}

    @app.get(Routes.LOGIN, tags=["meta"], include_in_schema=False)
    # 'next' is the query parameter's name in the URL
    def login_page(next: str = "/", token: str = ""):  # pylint: disable=redefined-builtin
        """The browser's way in: a password box, or a ready-made ``?token=`` link.

        A page rather than a bare 401 because a person who types the URL should meet
        something they can act on. The ``token`` query parameter is what makes the URL
        ``vast serve`` prints clickable — the same shape Jupyter has used for years.
        """
        from fastapi.responses import HTMLResponse
        if token:
            return _login_response(token, next=next, name="")
        return HTMLResponse(_LOGIN_HTML.replace("{{next}}", _html_escape(next)))

    @app.post(Routes.LOGIN, tags=["meta"], include_in_schema=False)
    async def login_submit(request: Request):
        """Exchange the shared secret for a session cookie."""
        form = await request.form()
        return _login_response(str(form.get("token", "")),
                               next=str(form.get("next", "/") or "/"),
                               name=str(form.get("name", "")),
                               secure=request.url.scheme == "https")

    # matches login_page's query parameter
    def _login_response(token: str, *, next: str, name: str, secure: bool = False):  # pylint: disable=redefined-builtin
        from fastapi.responses import HTMLResponse, RedirectResponse
        if not auth_token or not hmac.compare_digest(token.encode(),
                                                     auth_token.encode()):
            return HTMLResponse(
                _LOGIN_HTML.replace("{{next}}", _html_escape(next)).replace(
                    "<!--error-->", '<p class="err">That token was not accepted.</p>'),
                status_code=401)
        # 303, so the browser follows with GET regardless of how it got here.
        response = RedirectResponse(next or "/", status_code=303)
        response.set_cookie(auth.SESSION_COOKIE, token, httponly=True, secure=secure,
                            samesite="strict", path="/")
        # Readable by the SPA on purpose: it is a label to display, not a credential.
        response.set_cookie(auth.NAME_COOKIE, name.strip(), httponly=False,
                            secure=secure, samesite="strict", path="/")
        return response

    @app.get(Routes.VERSION, response_model=VersionInfo, tags=["meta"])
    def version(request: Request) -> VersionInfo:
        info = _guard(impl.version)
        # ``results_root``/``sources_root`` are documented as non-null *only when the
        # caller can actually open them* -- a local-filesystem service AND a same-host
        # request. The transport can only answer the first half, and this is the second.
        # It was asserted rather than enforced (``local_transport`` even said "app.py
        # blanks them again for a non-loopback request", of code that did not exist), so
        # a `vast serve` reached through a tunnel handed a remote caller absolute paths
        # on the *service's* disk -- which it would then try, and fail, to open.
        if not _from_loopback(request):
            info.results_root = None
            info.sources_root = None
        return info

    @app.get(Routes.USAGE, response_model=ResourceUsage, tags=["meta"])
    def resource_usage() -> ResourceUsage:
        return _guard(impl.resource_usage)

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
        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(lambda: _resolve_variation_asset(name, path))))

    @app.get(Routes.panel_types_asset("{name}", "{path:path}"), tags=["authoring"])
    def panel_type_asset(name: str, path: str):
        """Serve a package-provided run-view panel's web asset (Module Federation remote).

        A panel plugin (entry-point group ``robovast.panel_types``) ships a built
        ``remoteEntry.js`` + chunks as package data under its ``WEB_PANEL`` dir; the run
        view loads them at runtime. Core built-in panels have no assets (host-native)."""
        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(
            lambda: _resolve_plugin_asset("robovast.panel_types", name, path, "WEB_PANEL"))))

    @app.get(Routes.campaign_scene("{campaign_id}"), response_model=SceneStatus,
             tags=["results"])
    def campaign_scene_status(campaign_id: str, config_name: str = "", run_id: str = ""):
        """Is this run's 3D geometry ready, and if not what is happening about it.

        Pure by design: never starts a build (that is the POST below), because a GET that launched a
        2 GB image pull would fire on a browser prefetch or a strict-mode double render."""
        return _guard(lambda: impl.campaign_scene_status(campaign_id, config_name, run_id))

    @app.post(Routes.campaign_scene_run("{campaign_id}"), response_model=ActionResult,
              tags=["results"])
    def run_campaign_scene(campaign_id: str, config_name: str = "", run_id: str = ""):
        """Build this run's geometry unless it is cached, and return at once.

        Joins an in-flight build of the same world instead of starting a second; one build serves every
        campaign that used that world. Poll the GET above."""
        return _guard(lambda: impl.run_campaign_scene(campaign_id, config_name, run_id))

    @app.post(Routes.campaign_screenshot("{campaign_id}"), tags=["results"])
    def campaign_screenshot(campaign_id: str, config_name: str = "", run_id: str = "0",
                            at: Optional[float] = None,
                            view: List[str] = Query(default=[]),
                            focus: List[str] = Query(default=[]),
                            camera: str = "", size: str = "960x720"):
        """Re-render one moment of a run from a viewpoint you choose, as a PNG.

        A POST because it *runs* the simulator, in the campaign's own pinned image — the same
        reason ``scene/run`` is one. Unlike that build it is synchronous and has no status
        sibling: a screenshot is keyed on a camera pose and a moment, so nothing is cacheable
        and there is nothing to poll. Seconds when the image is on the node, minutes when it
        has to be pulled first.

        ``view`` is repeated ``key=value`` (``?view=azimuth=90&view=distance=12``)."""
        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
        from starlette.background import BackgroundTask  # pylint: disable=import-outside-toplevel

        from robovast.common.simulators import parse_view  # pylint: disable=import-outside-toplevel
        from robovast.service import screenshot  # pylint: disable=import-outside-toplevel

        frame = _guard(lambda: impl.campaign_screenshot(
            campaign_id, config_name, run_id, at=at, view=parse_view(view),
            focus=list(focus), camera=camera or None, size=size))
        # Deleted after the bytes are on the wire, by the function that knows what render()
        # built — a path reassembled by hand here is how a cleanup deletes the wrong tree.
        return FileResponse(frame, media_type="image/png",
                            background=BackgroundTask(screenshot.discard, Path(frame)))

    @app.get(Routes.campaign_scene_asset("{campaign_id}", "{path:path}"), tags=["results"])
    def campaign_scene_asset(campaign_id: str, path: str):
        """Serve one file of a cached scene descriptor (``<key>/scene.json``, ``<key>/tex_0.png``, …).

        Served like a panel bundle rather than from ``/results``: the descriptor is not in the campaign's
        results at all, it is in the service's shared cache."""
        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(
            lambda: impl.resolve_campaign_scene_asset(campaign_id, path))))

    @app.get(Routes.workspace_scene("{workspace_id}"), response_model=SceneStatus,
             tags=["authoring"])
    def workspace_scene_status(workspace_id: str, path: str = ""):
        """Is this project's world compiled, and if not what is happening about it.

        The config view's geometry. Pure, exactly like the campaign one — and behind the same
        cache, so a project and a campaign launched from it share an entry."""
        return _guard(lambda: impl.workspace_scene_status(workspace_id, path))

    @app.post(Routes.workspace_scene_run("{workspace_id}"), response_model=ActionResult,
              tags=["authoring"])
    def run_workspace_scene(workspace_id: str, path: str = ""):
        """Compile this project's world unless it is cached, and return at once."""
        return _guard(lambda: impl.run_workspace_scene(workspace_id, path))

    @app.get(Routes.workspace_scene_asset("{workspace_id}", "{path:path}"), tags=["authoring"])
    def workspace_scene_asset(workspace_id: str, path: str):
        """One file of a cached scene descriptor, for the config view's loader."""
        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
        return FileResponse(str(_guard(
            lambda: impl.resolve_workspace_scene_asset(workspace_id, path))))

    @app.get(Routes.campaign_panel_asset("{campaign_id}", "{path:path}"), tags=["authoring"])
    def campaign_panel_asset(campaign_id: str, path: str):
        """Serve a user-authored ``custom`` panel's bundle, staged into the campaign's
        immutable ``_config/`` snapshot (Module Federation remoteEntry + chunks)."""
        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
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

    @app.post(Routes.workspace_world("{workspace_id}"), response_model=WorldDescription,
              tags=["workspaces"])
    def describe_world(
        workspace_id: str, path: str = Body("", embed=True),
        targets: str = Body("", embed=True), entities: bool = Body(False, embed=True),
        backend: str = Body("", embed=True),
    ) -> WorldDescription:
        return _guard(
            lambda: impl.describe_world(workspace_id, path, targets, entities, backend))

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

    # -- taking a campaign in: grant + streamed PUT, then one import op ------

    @app.post(Routes.CAMPAIGN_ARCHIVES, response_model=UploadGrant, tags=["results"])
    def create_archive_upload() -> UploadGrant:
        """Grant a one-time PUT for a campaign archive.

        Separate from ``POST /uploads`` because that one addresses ``/sources``: it needs
        workspaces configured, and an archive is not project input.
        """
        grant = _guard(impl.create_archive_upload)
        grant.url = f"{Routes.campaign_archive_upload(grant.token)}"
        return grant

    @app.put(Routes.CAMPAIGN_ARCHIVE_UPLOAD, response_model=StagedArchive, tags=["results"])
    async def put_campaign_archive(token: str, req: Request) -> StagedArchive:
        """Redeem a one-time token and stream a campaign archive to disk.

        **Streamed, not buffered** — unlike ``PUT /uploads/{token}``, whose payload is a
        ``.vast`` or a notebook. A campaign archive is routinely gigabytes, and reading one
        into memory to write it straight back out would put the service's own footprint at
        the mercy of what somebody uploads.

        Stores the bytes and stops there: ``POST /campaigns/import`` is the import, for this
        and every other caller, so the operation has a single implementation.
        """
        staged = _guard(lambda: impl.redeem_archive_upload(token))
        size = 0
        try:
            with open(staged, "wb") as fh:
                async for chunk in req.stream():
                    fh.write(chunk)
                    size += len(chunk)
        except OSError as e:
            staged.unlink(missing_ok=True)
            raise HTTPException(status_code=500,
                                detail=f"could not store the archive: {e}") from e
        except Exception:
            # A dropped connection leaves a truncated tarball that would only fail at import.
            staged.unlink(missing_ok=True)
            raise
        return StagedArchive(path=str(staged), size=size)

    @app.get(Routes.SHARE_ARCHIVES, response_model=ShareListing, tags=["results"])
    def list_share_archives() -> ShareListing:
        """What the configured share holds, read with this service's own credentials.

        For the web UI, which can hold none of its own. ``configured=false`` when this
        service has no share -- a different answer from an empty one, and a client that
        conflated them would offer to import from nowhere.
        """
        return _guard(impl.list_share_archives)

    @app.post(Routes.CAMPAIGN_IMPORT, response_model=CampaignRef, tags=["results"])
    def import_campaign(request: ImportCampaignRequest) -> CampaignRef:
        """Take a campaign in -- from a staged upload, a host path, or the share.

        Registration is the point: listings and the web UI answer from ``campaign.db``, so an
        archive that is merely extracted lists blank. An older archive migrates on the way in
        -- the ``.vast`` ladder in memory, the store on open -- and a raw one is postprocessed
        after it lands, since it arrives without the tables anybody would query.

        Returns as soon as the import is under way, with the id of the campaign that is
        already listed at phase ``importing`` -- the same shape ``create`` and ``retrigger``
        answer with, because all three mean "a campaign now exists, go watch it". Per-stage
        verdicts are written to its ``_execution/import.json``, because "import failed"
        would hide which stage did and what recovers it.
        """
        return _guard(lambda: impl.import_campaign(request))

    # -- files: one address space -------------------------------------------
    #
    # ``/results/<campaign>/<path>`` and ``/sources/<workspace>/<path>``: the address a
    # caller passes to ``read_file`` is literally the URL that serves it (see
    # :mod:`robovast.client.file_address`). Content lives in its own namespaces rather
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

        from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
        media_type = mimetypes.guess_type(address)[0] or "application/octet-stream"

        # Always a path, never a buffer. A campaign's rosbag runs to tens of megabytes and
        # beyond, and reading it whole per request costs that much service memory to hand
        # back bytes it never looks at. FileResponse also brings Range and conditional
        # requests, which is what lets a browser seek a .webm rather than download it
        # before playing.
        #
        # `impl.local_file` is asked for outright rather than probed with getattr: every
        # transport implements it (they all subclass LocalTransport), so a presence check
        # could only ever succeed -- and the branch it used to guard, buffering the bytes
        # for "a lane with no path", was therefore unreachable while the *cluster* lane
        # fell into the local resolver and fetched an entire campaign to serve one file.
        return _guard(lambda: FileResponse(impl.local_file(address), media_type=media_type))

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
    def create_campaign(request: CreateCampaignRequest,
                        http_request: Request) -> CampaignRef:
        # The name comes from the authenticated caller, never from the body: a client
        # could otherwise send one name in the header and another here, and the record
        # would answer a question nobody asked. Self-declared either way — that is what
        # a shared secret permits — but at least it is the same claim throughout.
        principal = getattr(http_request.state, "principal", None)
        request = request.model_copy(
            update={"created_by": (principal.display_name if principal else None) or ""})
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
            media_type="text/event-stream", headers=_sse_headers)

    @app.get(Routes.campaign_status("{campaign_id}"), response_model=Status, tags=["campaigns"])
    def get_status(campaign_id: str) -> Status:
        return _guard(lambda: impl.get_status(campaign_id))

    @app.get(Routes.campaign_search_history("{campaign_id}"), response_model=SearchHistory,
             tags=["campaigns"])
    def get_search_history(campaign_id: str) -> SearchHistory:
        return _guard(lambda: impl.get_search_history(campaign_id))

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
            media_type="text/event-stream", headers=_sse_headers)

    @app.get(Routes.job_state("{campaign_id}"), response_model=JobState, tags=["campaigns"])
    def get_job_state(campaign_id: str, job_name: str) -> JobState:
        return _guard(lambda: impl.get_job_state(campaign_id, job_name))

    @app.post(Routes.job_exec("{campaign_id}"), response_model=ExecResult, tags=["campaigns"])
    def exec_in_job(campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> ExecResult:
        return _guard(lambda: impl.exec_in_job(campaign_id, job_name, command, container, source))

    @app.get(Routes.job_log_stream("{campaign_id}"), tags=["campaigns"])
    async def stream_job_log(campaign_id: str, request: Request, job_name: str):
        """Server-sent events: one running job's log, tailed live (``Last-Event-ID``
        resumes). A finished job whose pod was garbage-collected has no live log."""
        return StreamingResponse(
            _sse_log_stream(
                request,
                lambda off: impl.get_job_log(campaign_id, job_name, off),
                _last_event_offset(request)),
            media_type="text/event-stream", headers=_sse_headers)

    @app.post(Routes.campaign_stop("{campaign_id}"), response_model=ActionResult, tags=["campaigns"])
    def stop(campaign_id: str) -> ActionResult:
        return _guard(lambda: impl.stop(campaign_id))

    @app.post(Routes.job_stop("{campaign_id}"), response_model=ActionResult,
              tags=["campaigns"])
    def stop_job(campaign_id: str, job_name: str, reason: "str | None" = None,
                 source: str = "api") -> ActionResult:
        return _guard(lambda: impl.stop_job(campaign_id, job_name, reason, source))

    @app.post(Routes.campaign_retrigger_workspace("{campaign_id}"), response_model=WorkOrder,
              tags=["campaigns"],
              description="Materialise this campaign as a workspace with its config migrated as "
                          "far as it could be and a marker at every decision left. Nothing is "
                          "launched. For a config no migration step can carry forward.")
    def materialize_retrigger_workspace(
        campaign_id: str, workspace_name: str = Body("", embed=True)
    ) -> WorkOrder:
        return _guard(lambda: impl.materialize_retrigger_workspace(campaign_id, workspace_name))

    @app.get(Routes.campaign_retrigger_check("{campaign_id}"), response_model=RetriggerReport,
             tags=["campaigns"],
             description="Whether this campaign can be re-run, reported per axis (config "
                         "version, container protocol, images, plugins, asset providers). "
                         "Changes nothing and starts no container, so it is the cheap thing "
                         "to call before a retrigger.")
    def check_retrigger(campaign_id: str) -> RetriggerReport:
        return _guard(lambda: impl.check_retrigger(campaign_id))

    @app.post(Routes.campaign_retrigger("{campaign_id}"), response_model=CampaignRef,
              tags=["campaigns"],
              description="Launch a new campaign from an existing one's frozen config and "
                          "pinned image. The source campaign is not modified.")
    def retrigger_campaign(campaign_id: str) -> CampaignRef:
        return _guard(lambda: impl.retrigger_campaign(campaign_id))

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
    def stop_exec_container() -> ExecStopResult:
        return _guard(impl.stop_exec_container)

    @app.post(Routes.EXEC_RESOLVE_IMAGE, response_model=ImageResolution, tags=["exec"])
    def resolve_image(request: ExecRequest) -> ImageResolution:
        return _guard(lambda: impl.resolve_image(request))

    @app.get(Routes.campaign_archive("{campaign_id}"), tags=["results"])
    def download_campaign_archive(campaign_id: str):
        """Stream a ``tar.gz`` of the campaign, on either lane.

        Backs ``vast results download`` and the web UI's download button. What comes
        out is the campaign as this service holds it -- postprocessed, if it has been.
        Internal ``_postproc/`` staging is excluded so the archive is the clean
        campaign layout.

        Nothing is buffered and no scratch is used, on either lane: the cluster fetches
        objects from the store and tars them on the fly, the local lane tars its own
        directory into the response. Decisive for ~1TB campaigns.

        A local service used to refuse this with a 409 -- "the results are already on
        this host's filesystem". True of a caller on that host, and false of everyone
        else: a ``vast serve`` reached over the network could not be downloaded from at
        all, and the web UI had to hide its own button on that lane. The lane is not
        what decides whether a caller can read a file.
        """
        from fastapi.responses import StreamingResponse  # pylint: disable=import-outside-toplevel

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
        max_bytes: int | None = Body(None, embed=True),
    ) -> DataQueryResult:
        """Run a read-only ``SELECT``.

        ``max_bytes`` raises the reply's size ceiling for a client that renders the rows
        rather than reading them; omitted, it stays at the context-sized default, so an
        agent cannot spend its window on one ``SELECT *`` by forgetting a parameter.
        """
        return _guard(lambda: impl.query_campaign_data_sql(
            campaign_id, sql, max_rows, extra_campaign_ids, max_bytes))

    @app.get(Routes.campaign_query_csv("{campaign_id}"), tags=["results"])
    def query_campaign_data_csv(campaign_id: str, sql: str,
                                extra_campaign_ids: str = ""):
        """Stream the same read-only ``SELECT`` as CSV, with no row cap.

        The JSON query clamps at 5000 rows and says ``truncated``; this is where the rest
        of the result lives. Streamed, so a result larger than memory is fine at both
        ends, and an MCP tool can hand over this URL instead of spending context on rows.
        """
        from fastapi.responses import StreamingResponse  # pylint: disable=import-outside-toplevel
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
        batch: int | None = None,
    ):
        from fastapi.responses import HTMLResponse  # pylint: disable=import-outside-toplevel
        from nbclient.exceptions import \
            CellExecutionError  # pylint: disable=import-outside-toplevel

        from robovast.results_processing.notebook_render import \
            message_page_html  # pylint: disable=import-outside-toplevel

        def _render():
            try:
                return impl.render_campaign_notebook(
                    campaign_id, workload, level, config_name, run_id, theme, batch)
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
    from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel

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


def _build_mcp_app(impl: RobovastInterface):
    """The MCP server's ASGI app, already anchored at :data:`MCP_PATH` by FastMCP's own
    default (``fastmcp.settings.streamable_http_path == "/mcp"``) — matching where
    :func:`build_app` wires it in.

    *impl* is handed to the tool layer so a mounted MCP calls the implementation
    **directly** instead of going out over loopback HTTP and back into this same
    process — which was a wasted round trip per tool call and, once a token was
    required, a process authenticating to itself.
    """
    from robovast.mcp_server.server import create_server  # pylint: disable=import-outside-toplevel
    from robovast.mcp_server.service_access import \
        use_in_process_service  # pylint: disable=import-outside-toplevel
    use_in_process_service(impl)
    return create_server().http_app()


def _ui_dist() -> Optional[Path]:
    """Locate the built web UI, or ``None`` if there is none to serve.

    Three places, in the order that makes each caller see the freshest one it has:

    1. ``ROBOVAST_UI_DIST`` — an explicit override, and how a container image points at
       assets baked in at a path unrelated to the source tree.
    2. ``frontend/ui/dist`` relative to this file — a source checkout, where the live
       ``npm run build`` output is what a developer means, even if a wheel-shaped copy
       also happens to be lying around.
    3. ``robovast/_ui`` inside the installed package — the wheel. Resolving *up* from
       ``__file__`` cannot find it: installed, this module is
       ``site-packages/robovast/service/app.py``, and ``parents[3]`` is above
       site-packages. That is why a plain ``pip install`` used to ship no UI at all and
       say nothing about it.
    """
    import os  # pylint: disable=import-outside-toplevel

    candidates = []
    env = os.environ.get("ROBOVAST_UI_DIST")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parents[3] / "frontend" / "ui" / "dist")
    candidates.append(Path(__file__).resolve().parent.parent / "_ui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _mount_ui(app) -> None:
    """Serve the built SPA (``frontend/ui/dist``) from the service so the UI starts with it.

    The RobovastInterface routes are registered first and win; this mounts the SPA at
    ``/`` for everything else (``html=True`` serves ``index.html`` at the root). Served
    same-origin with the API, so no CORS.

    A missing build degrades to API-only rather than refusing to start — a service with no
    UI is still a working service. But it **warns**, because the failure is otherwise
    invisible: the UI is the half of the product most people meet first, and "I opened the
    URL and got JSON" is a confusing way to discover it was never built.
    """
    dist = _ui_dist()
    if dist is None:
        logger.warning(
            "web UI build not found — serving API only. Build it with "
            "`cd frontend/ui && npm run build`, or point ROBOVAST_UI_DIST at a built "
            "copy (an image bakes it in and sets that variable).")
        return
    from fastapi.staticfiles import StaticFiles  # pylint: disable=import-outside-toplevel
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    logger.info("serving web UI from %s", dist)


#: Where the origin comes from, for a service that was deployed rather than started by
#: hand: ``vast exec cluster setup`` bakes it from the Ingress, because an in-pod service is
#: given no RBAC to read its own. Named here as well as in the cluster lane that writes it,
#: since this is the side that reads it and the core must not import a lane.
PUBLIC_URL_ENV = "ROBOVAST_PUBLIC_URL"


def bound_origin(host: str, port: int) -> str:
    """The origin this bind can be declared as, or ``""`` for a wildcard.

    A wildcard bind has no single origin: the service is reachable on every address the
    host has, and which one a caller used is not knowable from here. In the deployment
    where that is normal -- a pod, bound ``0.0.0.0`` and reached through an Ingress -- the
    environment carries the answer instead, so declaring nothing here is what lets that
    one through.

    Deliberately stricter than the startup banner's ``display_host``, which guesses
    loopback for a wildcard. That guess is right for the human reading it on this machine
    and wrong for a caller elsewhere, and only one of the two travels.
    """
    if host in ("0.0.0.0", "::"):  # noqa: S104
        return ""
    return f"http://{host}:{port}"


def startup_banner(base_url: str, token: str, *, ephemeral: bool,
                   mount_mcp: bool) -> str:
    """What ``vast serve`` prints for the two clients a person is about to point here.

    Printed rather than logged: these are the lines the person starting the service has
    to act on, and a log level could hide them.

    A **browser** gets a link with the token in it, so "no token was configured" is
    answered by something to click rather than a secret to hunt for -- the shape Jupyter
    has used for years. An **agent** cannot click, so it gets the registration command
    instead, rendered by the same helper ``vast login`` and ``vast exec cluster token``
    use: three places hand out access to this service and the header set must not drift
    between them.

    The ephemeral note comes last because it qualifies both, and because it is the whole
    difference between a registration that survives a restart and one that silently stops
    authenticating the next time the service comes up. A configured token needs no note:
    whoever set it can reuse it.
    """
    lines = []
    if ephemeral:
        lines.append(f"  RoboVAST: {base_url}{Routes.LOGIN}?token={token}")
    if mount_mcp:
        from robovast.client.login import mcp_add_command  # pylint: disable=import-outside-toplevel
        lines.append("  For an agent:\n    "
                     + " \\\n      ".join(mcp_add_command(base_url, token)))
    if not lines:
        return ""
    if ephemeral:
        lines.append(
            f"  (no {auth.TOKEN_ENV_VAR} configured, so this token is temporary and "
            f"changes on restart;\n   set it in .env to keep a browser login and an "
            f"agent registration working across restarts)")
    return "\n" + "\n\n".join(lines) + "\n"


def _from_loopback(request) -> bool:
    """Did this request come from the same machine?

    Conservative on purpose: anything unparseable, absent (a Unix socket has no peer
    address) or forwarded counts as *not* loopback, because the field this gates is only
    useful to a caller that can open the service host's filesystem, and being wrong in
    that direction merely costs a caller two fields it could not have used.
    """
    import ipaddress  # pylint: disable=import-outside-toplevel
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if not host:
        return False
    # Behind a proxy the peer is the proxy, so a forwarded request is never same-host.
    if request.headers.get("x-forwarded-for"):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _proxy_trust(env) -> tuple:
    """Whether to believe ``X-Forwarded-*``, as ``(proxy_headers, allow_ips)``.

    Behind an Ingress, TLS ends at the controller and the pod is spoken to over plain
    HTTP. uvicorn trusts ``X-Forwarded-Proto`` only from 127.0.0.1 by default and the
    controller reaches us from a cluster IP, so ``request.url.scheme`` was ``"http"``
    and the session cookie silently lost its ``Secure`` flag on every published
    deployment. That cookie *is* the shared token, so a single request to the http://
    port would have sent it in clear text -- and the Ingress' 308 to https does not
    prevent it, because the browser attaches the cookie before it sees the redirect.

    Trusted **only in-pod**, where the port is reachable through the Service alone:
    forging the header there needs cluster access, which already grants far more than a
    scheme does. Off cluster, ``vast serve`` keeps uvicorn's default, stays plain http
    for the developer, and the flag is then correctly absent rather than merely missing.

    Deliberately derived rather than configured: an operator who has to remember a flag
    to keep a cookie ``Secure`` will one day not remember it, and nothing would say so.
    """
    behind_ingress = bool(env.get("KUBERNETES_SERVICE_HOST"))
    return behind_ingress, "*" if behind_ingress else "127.0.0.1"


def serve(impl: RobovastInterface, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          log_level: str = "info", mount_mcp: bool = True) -> None:
    """Run the service in the foreground (blocking) via uvicorn.

    Every request needs the shared token; when none is configured one is minted and
    printed as a clickable login URL, so there is no unauthenticated mode to start by
    accident. Binds ``127.0.0.1`` by default all the same — publishing the service is a
    deliberate act (``vast exec cluster setup --ingress-host``, which insists on TLS).

    ``mount_mcp`` (default on) puts the MCP server on this same port, so one URL reaches
    the web UI, the REST API and the tools together.
    """
    import uvicorn  # pylint: disable=import-outside-toplevel

    from robovast.common.shutdown import begin_shutdown  # pylint: disable=import-outside-toplevel

    class _Server(uvicorn.Server):
        """uvicorn server that announces the shutdown before it starts winding down.

        ``handle_exit`` runs in the signal handler — the first moment the process
        knows a Ctrl+C happened, ahead of the graceful-shutdown clock. Raising the
        process-wide flag here is what lets blocking I/O several layers down (an S3
        read retrying over a ``kubectl port-forward``) fail fast instead of repairing
        a connection this process is about to close; see
        :mod:`robovast.common.shutdown`.
        """

        def handle_exit(self, sig, frame):
            begin_shutdown()
            super().handle_exit(sig, frame)

    token, ephemeral = auth.resolve_token(None)
    app = build_app(impl, mount_mcp=mount_mcp, auth_token=token)
    _enable_thread_dump_signal()
    mcp_note = ", MCP at /mcp" if mount_mcp else ""
    logger.info("robovast-service listening on %s:%d (OpenAPI at /docs%s)",
                host, port, mcp_note)

    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # noqa: S104
    # The origin this service will report to clients, published the same way a deployed one
    # receives it -- `setdefault`, so a value baked in at setup always wins and this only
    # answers for a service nobody told. Deliberately not written onto *impl*: `serve` takes
    # a `RobovastInterface`, and the origin is not part of that contract.
    import os  # pylint: disable=import-outside-toplevel
    os.environ.setdefault(PUBLIC_URL_ENV, bound_origin(host, port))
    banner = startup_banner(f"http://{display_host}:{port}", token,
                            ephemeral=ephemeral, mount_mcp=mount_mcp)
    if banner:
        print(banner, flush=True)
    if not ephemeral:
        logger.info("authenticating with the configured %s", auth.TOKEN_ENV_VAR)

    # Where this service's images come from. A persistent service pulling from a dev
    # project for months is easy to forget, so state it once at startup rather than only
    # on demand -- and state it whether or not it was overridden, because "which images
    # is it running?" is the first question when a campaign behaves unexpectedly, and an
    # answer that appears only when someone configured something cannot be relied on.
    #
    # This is the *service default*. A campaign may carry its own project on the request
    # (CreateCampaignRequest.image_project), which is why the line says "default".
    from robovast.common.execution import (  # pylint: disable=import-outside-toplevel
        DEFAULT_IMAGE_PROJECT, default_image_project, default_image_tag)
    project = default_image_project()
    logger.info("RoboVAST image default: %s/*:%s%s", project, default_image_tag(),
                "" if project == DEFAULT_IMAGE_PROJECT else " (ROBOVAST_PROJECT)")

    # Drive uvicorn via an explicit Server so the SSE generators can probe
    # ``should_exit`` (set when a Ctrl+C begins shutdown, before the connection
    # wait) and close their streams instead of hanging it. ``timeout_graceful_
    # shutdown`` is a backstop for any other lingering connection.
    proxy_headers, forwarded_allow_ips = _proxy_trust(os.environ)
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level,
                            log_config=_quiet_access_log_config(),
                            proxy_headers=proxy_headers,
                            forwarded_allow_ips=forwarded_allow_ips,
                            timeout_graceful_shutdown=5)
    server = _Server(config)
    app.state.should_exit = lambda: server.should_exit
    server.run()


def _enable_thread_dump_signal() -> None:
    """Make ``kill -USR1 <pid>`` dump every thread's stack to stderr.

    A service that stops answering is diagnosed from *where its threads are parked*, and
    that evidence only exists while it is still hung — by the time anyone restarts it, it
    is gone. Most of this API is sync ``def`` handlers running in anyio's worker
    threadpool, so a hang shows up as N threads inside one blocking call (an S3 read over
    a stalled ``kubectl port-forward``, say) and the dump names it outright. Without this
    the same investigation has to be re-derived from the code every time.

    ``faulthandler.enable()`` also gives a native traceback on a hard crash (segfault in
    a C extension), which the Python-level handler cannot report.
    """
    import faulthandler  # pylint: disable=import-outside-toplevel
    import os  # pylint: disable=import-outside-toplevel
    import signal  # pylint: disable=import-outside-toplevel

    faulthandler.enable()
    # Not on Windows, and not if something else already claimed SIGUSR1: a diagnostic
    # must never be the reason the service fails to start.
    if not hasattr(signal, "SIGUSR1"):
        return
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
    except (AttributeError, OSError, RuntimeError, ValueError) as e:
        logger.debug("could not register the SIGUSR1 thread dump: %s", e)
        return
    logger.debug("thread dump: kill -USR1 %d", os.getpid())


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
