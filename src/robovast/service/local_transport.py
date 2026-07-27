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

"""``LocalTransport`` — the in-process implementation over the local Docker backend.

Executes local Docker campaigns by driving
:func:`robovast.execution.controller.run_batch_campaign` in a background thread,
serving live status from the same
:class:`~robovast.execution.control_server.ControllerState` the cluster controller
uses. This backs ``vast exec local run`` (mode 1); campaigns die with the process.
:class:`~robovast.service.cluster_service.ClusterService` subclasses this, reusing
its driver-hosting shape and overriding only the launch hooks.

Split out of the former single ``client`` module; ``client`` now re-exports
``LocalTransport`` so existing imports keep working.
"""

import logging
import os
import subprocess
import threading
import time
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Optional

from robovast.execution.control_server import (ControllerState, Phase, Status,
                                               failure_detail, is_terminal)
from robovast.service.interface import (ActionResult, BuildImageRequest,
                                        CampaignRef,
                                        CampaignSummary, CreateCampaignRequest,
                                        CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest,
                                        FileContent, FileMeta, ImageBuildRef,
                                        ImageBuildStatus, JobCounts,
                                        JobSummary, ListCampaignsRequest,
                                        ListCampaignsResponse, ListJobsResponse,
                                        ListFilesResponse, ListWorkspacesResponse,
                                        LogChunk,
                                        PreviewConfiguration, PreviewResponse,
                                        ResourceUsage,
                                        RobovastInterface, Routes, UploadGrant,
                                        ValidationProblem, ValidationReport,
                                        VariationTypeInfo,
                                        VariationTypeParam, VariationTypesResponse,
                                        VersionInfo, WorkspaceInfo,
                                        WriteFileRequest)

logger = logging.getLogger(__name__)


def _robovast_version() -> str:
    """The version of the code *this process is running*.

    ``get_app_version`` prefers the git revision (with ``+dirty`` for an unclean tree)
    and falls back to package metadata. That preference is the point: a service is
    long-lived and loads its code once, so a client needs to tell "the fix I just made
    is loaded" from "this process predates it". The packaged version alone cannot —
    it stays ``2.0.0`` across every edit.
    """
    from robovast.common.execution import get_app_version
    try:
        return get_app_version()
    except Exception:  # noqa: BLE001 - version reporting must never break the handshake
        try:
            return _pkg_version("robovast")
        except PackageNotFoundError:  # editable/source without metadata
            return "0.0.0+unknown"


def _plugin_remotes(group: str, asset_attr: str, url_builder,
                    module_attr: str = "", module_default: str = "./preview") -> dict:
    """Map entry-point name → a Module-Federation remote descriptor, for the plugins in
    *group* that ship a web asset (declare *asset_attr*). ``module`` is read from the
    class's *module_attr* when given, else *module_default*. Best-effort; a plugin that
    fails to import is skipped. Shared by variation-type previews and run-view panels."""
    from importlib.metadata import entry_points
    remotes = {}
    for ep in entry_points(group=group):
        try:
            cls = ep.load()
            asset = getattr(cls, asset_attr, None)
        except Exception as e:  # noqa: BLE001 - skip a broken plugin
            logger.debug("%s plugin %s failed to load for web asset: %s", group, ep.name, e)
            continue
        if asset:
            module = getattr(cls, module_attr, module_default) if module_attr else module_default
            # The Module-Federation container name defaults to the entry-point name (one
            # container per type). A plugin that ships several panels from one shared bundle
            # sets REMOTE_NAME to a common container name (e.g. "robovast_nav") on each class;
            # the asset URL still uses ep.name, so every type resolves to the same bundle.
            remotes[ep.name] = {
                "name": getattr(cls, "REMOTE_NAME", ep.name),
                "remote_entry_url": url_builder(ep.name, "remoteEntry.js"),
                "module": module,
            }
    return remotes


def _variation_remotes() -> dict:
    """Variation-type name → MF remote descriptor for types shipping a ``WEB_PREVIEW``
    (built-in types return nothing; they render host-native). See :func:`_plugin_remotes`."""
    return _plugin_remotes("robovast.variation_types", "WEB_PREVIEW",
                           Routes.variation_asset, module_default="./preview")


def _panel_remotes() -> dict:
    """Package-provided panel type name → MF remote descriptor for types shipping a
    ``WEB_PANEL`` (e.g. ``robovast_nav``'s ``costmap``). See :func:`_plugin_remotes`."""
    from robovast.common.config import PANEL_TYPES_GROUP  # pylint: disable=import-outside-toplevel
    return _plugin_remotes(PANEL_TYPES_GROUP, "WEB_PANEL",
                           Routes.panel_types_asset, module_attr="PANEL_MODULE",
                           module_default="./panel")


def _config_previews(config: dict, remotes: dict) -> list:
    """Per-variation preview descriptors for one resolved config.

    Reads the declared variations off the config's ``_config_block`` (the ``.vast``
    ``configuration`` entry). Each descriptor is ``{variation_type, params, remote}``
    where ``remote`` is a Module-Federation descriptor for an external plugin's web
    component, or ``None`` for a built-in (rendered host-native)."""
    block = config.get("_config_block") or {}
    out = []
    for entry in block.get("variations", []) or []:
        if not isinstance(entry, dict):
            continue
        for type_name, params in entry.items():
            out.append({
                "variation_type": type_name,
                "params": params if isinstance(params, dict) else {},
                "remote": remotes.get(type_name),
            })
    return out


# ---------------------------------------------------------------------------
# Local (in-process) transport
# ---------------------------------------------------------------------------


def _read_log_slice(path: Path, offset: int, eof: bool) -> LogChunk:
    """Read a single log file from byte *offset* onward into a :class:`LogChunk`.

    The same offset-poll contract as the campaign log: ``next_offset`` is the new
    end of file, ``eof`` signals no more bytes will be written. A not-yet-created
    log (the run just started) reads as an empty, non-terminal chunk.
    """
    if not path.is_file():
        return LogChunk(text="", next_offset=offset, eof=eof)
    data = path.read_bytes()
    return LogChunk(text=data[offset:].decode("utf-8", errors="replace"),
                    next_offset=len(data), eof=eof)

class _LocalCampaign:
    """Bookkeeping for one in-process campaign: its live state + worker thread."""

    __slots__ = ("campaign_id", "results_dir", "state", "thread", "error", "created_at")

    def __init__(self, campaign_id: str, results_dir: str, state: ControllerState):
        from datetime import datetime, timezone
        self.campaign_id = campaign_id
        self.results_dir = results_dir
        self.state = state
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        # Real launch time, recorded the instant the campaign is registered — so a
        # just-launched campaign has a start time before the controller writes the
        # ``campaign`` DB row (seconds later). Same ISO-8601 UTC shape as
        # ``_campaign_started_at`` reads back from disk, so both format identically.
        self.created_at: str = datetime.now(timezone.utc).isoformat()


class LocalTransport(RobovastInterface):
    """In-process implementation over the local Docker backend.

    A campaign always runs a **workspace's** ``.vast``: ``workspace_id`` is the only
    project binding this service accepts (see :meth:`_resolve_project`), and
    ``config_path``/``vast_path`` selects among several ``.vast`` files in that
    workspace. ``.robovast_project`` is a CLI-side concept and never selects what the
    service runs.
    """

    #: Local Docker is single-flight — the backend hardcodes container name
    #: ``robovast``, so two concurrent local campaigns would collide.
    _CONTAINER_NAME = "robovast"

    #: How long to wait, on shutdown, for a stopped campaign's worker thread to
    #: run its container teardown before we exit anyway. A hair over the backend's
    #: SIGTERM grace (``_STOP_GRACE_SECONDS`` = 15s) so the trap can complete.
    _SHUTDOWN_JOIN_SECONDS = 20

    #: How long a :meth:`resource_usage` reading is reused. The UI chip and the MCP
    #: tool poll this, so the real sampling (a psutil call locally, node/pod lists on
    #: the cluster) is memoised for this window — N concurrent clients cost one
    #: sampling per window, not N.
    _USAGE_CACHE_TTL = 10.0

    def __init__(self, store=None, workspace_dir=None):
        self._campaigns: dict[str, _LocalCampaign] = {}
        self._lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._usage_cache: "tuple[float, ResourceUsage] | None" = None
        # Prime psutil's non-blocking CPU sampler so the first resource_usage()
        # reading reflects real load instead of the 0.0 a cold sampler returns.
        import psutil  # pylint: disable=import-outside-toplevel
        psutil.cpu_percent(interval=None)
        if store is None:
            from robovast.service.workspaces import WorkspaceStore
            store = WorkspaceStore(workspace_dir=workspace_dir)
        self.store = store

    # -- project resolution -------------------------------------------------

    def _resolve_project(self, workspace_id: str, vast_path: str = ""):
        """Resolve what to act on — always a workspace's ``.vast``.

        ``workspace_id`` is the service's **only** project binding, so this is
        single-mode by design. ``vast_path`` selects which ``.vast`` in a
        multi-``.vast`` workspace (workspace-relative).

        There used to be a fallback here: an empty ``workspace_id`` resolved the
        ``.robovast_project`` in the *service's* CWD. That branch ignored ``vast_path``
        entirely, so a caller naming one ``.vast`` silently got whichever one had been
        initialized -- a campaign that ran the wrong simulator and looked successful.
        Its stated justification ("``vast exec local run`` back-compat") was false:
        that command drives the controller in-process and never reaches this method.
        """
        if not workspace_id:
            raise ValueError(
                "workspace_id is required: the service runs a workspace's project. "
                "List them with 'vast workspace list' / list_workspaces(); pin a "
                "directory in place with 'vast serve --workspace-dir <dir>', or "
                "upload one with 'vast workspace init <dir>' (create_workspace + "
                "update_workspace). ('.robovast_project' / 'vast init' binds the "
                "CLI's project, not the service's -- it never selected what the "
                "service runs.)")
        return self._project_for_workspace(workspace_id, vast_path)

    def _campaigns_root(self) -> Path:
        """The single results root every local campaign shares.

        Campaigns are self-contained and **workspace-independent**, so every
        local Docker campaign — launched from a workspace *or* the CWD project —
        lands here and is listed / reconstructed / queried from here. Writing them
        under ``<workspace>/results`` instead would both hide them from the
        service's readers and let ``delete_workspace`` take the campaigns with it.

        Prefer an initialized CWD project's ``results_dir`` (back-compat with
        ``vast exec local run``); otherwise a service-owned dir beside the
        workspaces so a headless ``vast serve`` still has one stable location.

        Pure path resolver — the dir is materialized lazily by ``CampaignStore``
        on first run, so simply asking where campaigns live (e.g. from
        :class:`ClusterService`, whose results live in the object store) never
        creates a stray local directory.
        """
        from robovast.common.cli.project_config import ProjectConfig
        project = ProjectConfig.load()
        if project is not None and project.results_dir:
            return Path(project.results_dir)
        return self.store.registry.root.parent / "results"

    def _project_for_workspace(self, workspace_id: str, vast_path: str = ""):
        """Build a ProjectConfig rooted at the workspace's ``project/`` dir.

        A workspace may hold **several** ``.vast`` files. ``vast_path`` (a
        workspace-relative path, confined like every other file op) selects one;
        when omitted, the sole ``.vast`` is used, and if there are several a clear
        error names the candidates so the caller can pass ``vast_path``. Results go
        to the shared :meth:`_campaigns_root` (never ``<workspace>/results``), so
        campaigns stay independent of the workspace that authored them.
        """
        from robovast.common.cli.project_config import ProjectConfig
        workspace_id = self.store.registry.require(workspace_id)["workspace_id"]
        project_dir = self.store.registry.project_dir(workspace_id)
        if vast_path:
            # Confine to the workspace (reject ..\/absolute), exactly like the file ops.
            config_path = self.store._safe_join(workspace_id, vast_path)
            if not config_path.is_file():
                raise ValueError(f"no such .vast in workspace {workspace_id!r}: {vast_path!r}")
        else:
            # A pinned dir is a live project tree that may hold campaign-output
            # snapshots (results/**/_config/*.vast); skip those (and hidden dirs)
            # so a project with one authored .vast still resolves cleanly. Normal
            # workspaces never contain results/, so this is a no-op for them.
            from robovast.service.workspaces import PINNED_SKIP_DIRS
            vasts = [
                v for v in sorted(project_dir.rglob("*.vast"))
                if not any(part.startswith(".") or part in PINNED_SKIP_DIRS
                           for part in v.relative_to(project_dir).parts)]
            if not vasts:
                raise ValueError(
                    f"workspace {workspace_id!r} has no .vast file; "
                    "write one with write_project_file() first")
            if len(vasts) > 1:
                rel = ", ".join(v.relative_to(project_dir).as_posix() for v in vasts)
                raise ValueError(
                    f"workspace {workspace_id!r} has {len(vasts)} .vast files ({rel}); "
                    "specify which with the path/config_path argument")
            config_path = vasts[0]
        # Results land in the shared campaigns root (materialized lazily by the
        # store on first run), never under the workspace.
        return ProjectConfig(config_path=str(config_path),
                             results_dir=str(self._campaigns_root()))

    # -- workspaces ---------------------------------------------------------

    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceInfo:
        return WorkspaceInfo.model_validate(self.store.registry.create(request.name))

    def list_workspaces(self) -> ListWorkspacesResponse:
        return ListWorkspacesResponse(workspaces=[
            WorkspaceInfo.model_validate(e) for e in self.store.registry.list()])

    def get_workspace(self, workspace_id: str) -> WorkspaceInfo:
        return WorkspaceInfo.model_validate(self.store.registry.require(workspace_id))

    def delete_workspace(self, workspace_id: str) -> ActionResult:
        self.store.registry.delete(workspace_id)
        return ActionResult(ok=True, message=f"workspace {workspace_id} deleted")

    def write_project_file(self, request: WriteFileRequest) -> FileMeta:
        return FileMeta.model_validate(self.store.write_file(
            request.workspace_id, request.path, request.content))

    def edit_project_file(self, request: EditFileRequest) -> FileMeta:
        return FileMeta.model_validate(self.store.edit_file(
            request.workspace_id, request.path, request.old_string, request.new_string))

    def read_project_file(self, workspace_id: str, path: str) -> FileContent:
        return FileContent(path=path,
                           content=self.store.read_file(workspace_id, path))

    def list_project_files(self, workspace_id: str) -> ListFilesResponse:
        return ListFilesResponse(files=[
            FileMeta.model_validate(f) for f in self.store.list_files(workspace_id)])

    def delete_project_file(self, workspace_id: str, path: str) -> ActionResult:
        self.store.delete_file(workspace_id, path)
        return ActionResult(ok=True, message=f"deleted {path}")

    def create_upload(self, request: CreateUploadRequest) -> UploadGrant:
        grant = self.store.create_upload(
            request.workspace_id, request.path, executable=request.executable)
        return UploadGrant(token=grant["token"], path=grant["path"],
                           expires_in=grant["expires_in"])

    # -- interface ----------------------------------------------------------

    def version(self) -> VersionInfo:
        return VersionInfo(robovast_version=_robovast_version(), backend="docker",
                           backends=["local"])

    def resource_usage(self, backend: Optional[str] = None) -> ResourceUsage:
        """Backend capacity/usage, cached for ``_USAGE_CACHE_TTL`` seconds.

        The cache (and its lock) live here so both the local and cluster services
        share one memoisation path; subclasses supply the actual reading by
        overriding :meth:`_compute_resource_usage`. Computing under the lock means
        concurrent polls collapse to a single sampling per window. ``backend`` is a
        no-op here (this is a single-lane service); a multi-backend service uses it
        to pick the lane.
        """
        with self._usage_lock:
            cached = self._usage_cache
            if cached is not None and time.monotonic() - cached[0] < self._USAGE_CACHE_TTL:
                return cached[1]
            usage = self._compute_resource_usage()
            self._usage_cache = (time.monotonic(), usage)
            return usage

    def _compute_resource_usage(self) -> ResourceUsage:
        """Local host capacity + live utilisation via ``psutil``.

        ``cpu_percent(interval=None)`` is non-blocking — it averages CPU load since
        the previous call rather than sleeping per request. The first reading after
        the process starts is ``0.0`` (no prior sample); the TTL cache means that is
        replaced by a real value on the next window. Overridden by
        :class:`~robovast.service.cluster_service.ClusterService`.
        """
        import psutil  # pylint: disable=import-outside-toplevel
        vm = psutil.virtual_memory()
        cores = psutil.cpu_count(logical=True)
        return ResourceUsage(
            backend="docker",
            cpu_capacity=float(cores),
            cpu_used=cores * psutil.cpu_percent(interval=None) / 100.0,
            memory_capacity_bytes=vm.total,
            memory_used_bytes=vm.used,
            parallel_runs=False,   # Docker backend is single-flight: runs are sequential
        )

    # -- launch hooks (overridden by ClusterService) -------------------------
    #
    # create_campaign below is shared by BOTH deployments: local Docker and the
    # in-cluster service. They differ only in these hooks — the driver loop, its
    # worker thread, the status/outcome bookkeeping and the postprocess tail are
    # identical, which is the whole point of running the controller in-process on
    # both sides.

    def _guard_new_campaign(self) -> None:
        """Reject a launch this deployment cannot run concurrently.

        Local Docker is single-flight (the backend hardcodes the ``robovast``
        container name), so two concurrent local campaigns would collide. The
        cluster service overrides this to a no-op: its campaigns are I/O-bound
        drivers whose compute lives in Kubernetes Jobs, so they run in parallel.
        """
        with self._lock:
            if any(not self._is_done(c) for c in self._campaigns.values()):
                raise RuntimeError(
                    "A local campaign is already running (local Docker is "
                    "single-flight). Stop it before starting another.")

    def _build_backend(self, state):
        """The :class:`ExecutionBackend` this deployment runs campaigns on."""
        from robovast.execution.backends import DockerBackend
        return DockerBackend(state=state)

    def _run_options(self, request) -> "RunOptions":  # noqa: F821
        from robovast.execution.backends import RunOptions
        # Local backend: upload_to_share just writes a tar.gz to _archives/ (no
        # external provider). Honour the toggle so it works for a local run too.
        return RunOptions(gui=False,
                          upload_to_share=bool(getattr(request, "upload_to_share", False)))

    def _campaign_context(self, campaign_id: str, project):
        """Per-campaign setup entered *inside* the worker thread.

        A context manager, so anything thread-scoped (the cluster's aux-pod
        container-runner factory) is established where the composition that reads
        it runs, and torn down when the campaign ends. No-op locally.
        """
        import contextlib
        return contextlib.nullcontext()

    def _postprocess_in_process(self) -> bool:
        """True when the worker runs analysis postprocessing after the loop.

        Local does (in-process). The cluster service instead chains it *inside* the
        builder (``RunOptions.postprocess``) so ``data.db`` rides the campaign's
        existing upload rather than needing one of its own.
        """
        return True

    def _record_campaign_failure(self, campaign_id, results_dir, state, exc, backend):
        """Durably record a failed campaign. Local writes ``_execution/outcome.json``."""
        self._record_outcome(campaign_id, results_dir, state)

    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.execution.controller import (campaign_id_for,
                                                   run_batch_campaign,
                                                   run_search_campaign)

        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        results_dir = str(project.results_dir)
        campaign_id = campaign_id_for(campaign_config, request.campaign_name or None)
        is_search = campaign_config.search is not None
        config_filter = request.config_filter or None

        # NOTE: the config_filter is deliberately **not** validated here. Doing so
        # meant expanding the whole campaign synchronously on the caller's thread,
        # which (a) broke this method's documented "returns immediately" contract —
        # the POST hung for the entire expansion, holding an anyio threadpool slot —
        # (b) expanded twice (once here, once in the worker), and (c) could not work
        # for campaigns needing an auxiliary container, whose runner only exists
        # inside the worker's _campaign_context. Expansion now happens exactly once,
        # in the worker. A bad filter surfaces there as phase=failed with the same
        # "Available configs:" message in Status.error (+ outcome.json) — which the
        # in-process driver makes visible immediately; the old submit-time check
        # existed only because a doomed *controller pod* would have hidden it in
        # kubectl logs, and there is no such pod any more.

        self._guard_new_campaign()

        # Fail loudly rather than silently adopt an existing campaign's directory.
        # Ids are timestamp-unique (see campaign_id_for), so this only fires on a
        # genuine collision (e.g. a hand-copied dir) — never in normal operation.
        campaign_root = os.path.join(results_dir, campaign_id)
        if os.path.exists(campaign_root):
            raise RuntimeError(
                f"campaign {campaign_id} already exists at {campaign_root}")

        state = ControllerState()
        entry = _LocalCampaign(campaign_id, results_dir, state)
        runs = request.runs if request.runs and request.runs > 0 else None
        options = self._run_options(request)

        # Register the instant the campaign is accepted — before the (possibly slow)
        # image build — so it is listed with a live phase from t=0 rather than only
        # appearing once the worker creates its directory. The single-flight guard
        # ran first, so registering our own not-yet-started entry here cannot race a
        # second launch past that guard (_is_done treats a non-terminal entry as
        # running regardless of whether its thread exists yet).
        with self._lock:
            self._campaigns[campaign_id] = entry

        # Implicit preflight: if execution.image is a symbolic ``build:<tag>`` ref,
        # ensure the image exists (idempotent build) and pin it as the explicit
        # image so the backend uses it (explicit wins in resolve_robovast_image).
        # Blocks until the image is ready — the campaign shows phase ``building``
        # meanwhile. A doomed build is recorded as ``failed`` (so it stays visible
        # in the list and does not wedge the single-flight guard) and re-raised so
        # the caller sees it before any run starts.
        spec, _ = self._build_spec_for(project, campaign_config)
        if spec is not None:
            state.set_phase(Phase.BUILDING)
        try:
            built = self._ensure_build_image(project, campaign_config)
        except Exception as e:  # noqa: BLE001 - recorded via status, then re-raised
            logger.exception("Campaign %s image build failed", campaign_id)
            entry.error = str(e)
            state.update(error=failure_detail(e))
            state.set_phase(Phase.FAILED, stage=str(e))
            raise
        if built:
            options.image = built
        state.set_phase(Phase.STARTING)

        def _worker():
            from robovast.execution.backends import (
                CampaignConfigError, CampaignStopped)
            backend = None
            try:
                with self._campaign_context(campaign_id, project):
                    backend = self._build_backend(state)
                    if is_search:
                        run_search_campaign(
                            project.config_path, campaign_config, results_dir, runs,
                            backend=backend, options=options,
                            campaign_id=campaign_id, state=state)
                    else:
                        run_batch_campaign(
                            project.config_path, campaign_config, results_dir, runs,
                            config_filter=config_filter, backend=backend,
                            options=options, campaign_id=campaign_id, state=state)
            except CampaignStopped:
                # Clean cooperative stop (Ctrl+C / Stop): the controller already set
                # phase "stopped". Not a failure — no error, no traceback. Persist the
                # outcome so "stopped" survives a service restart.
                logger.info("Campaign %s stopped by request", campaign_id)
                self._record_campaign_stopped(campaign_id, results_dir, state, backend)
                return
            except CampaignConfigError as e:
                # Bad user input (e.g. a typo'd --config filter), not a bug. The
                # message is self-contained and actionable, so surface it as
                # phase=failed *without* a stack trace, which would only be noise.
                logger.warning("Campaign %s: %s", campaign_id, e)
                entry.error = str(e)
                state.update(error=str(e))
                state.set_phase(Phase.FAILED, stage=str(e))
                self._record_campaign_failure(
                    campaign_id, results_dir, state, e, backend)
                return
            except Exception as e:  # noqa: BLE001 - surfaced via status
                logger.exception("Campaign %s failed", campaign_id)
                entry.error = str(e)
                state.update(error=failure_detail(e))
                state.set_phase(Phase.FAILED, stage=str(e))
                self._record_campaign_failure(
                    campaign_id, results_dir, state, e, backend)
                return
            # Analysis postprocessing (rosbags → CSV → data.db) — what the eval
            # viewer / `query_campaign_data_sql` read. The batch/search loop leaves
            # it separate, so run it here when the caller asked (the default).
            if request.postprocess and self._postprocess_in_process():
                self._postprocess(campaign_id, results_dir, state, entry)

        thread = threading.Thread(
            target=_worker, name=f"robovast-{campaign_id}", daemon=True)
        entry.thread = thread
        thread.start()
        logger.info("Started campaign %s (search=%s)", campaign_id, is_search)
        return CampaignRef(campaign_id=campaign_id)

    # -- image builds -------------------------------------------------------

    @property
    def _image_builds(self):
        """Lazily-created local image-build manager (docker buildx --load)."""
        mgr = getattr(self, "_image_build_mgr", None)
        if mgr is None:
            from pathlib import Path as _Path

            from robovast.service.image_build import LocalImageBuildManager
            root = os.environ.get("ROBOVAST_BUILDS_ROOT")
            log_root = _Path(root) if root else _Path.home() / ".robovast" / "builds"
            mgr = LocalImageBuildManager(log_root)
            self._image_build_mgr = mgr
        return mgr

    def _build_spec_for(self, project, campaign_config):
        """Return (BuildSpec, project_dir) for a project, or (None, None)."""
        from pathlib import Path as _Path

        from robovast.service.image_build import extract_build_spec
        spec = extract_build_spec(campaign_config)
        if spec is None:
            return None, None
        project_dir = _Path(project.config_path).resolve().parent
        return spec, project_dir

    def _ensure_build_image(self, project, campaign_config) -> "str | None":
        """Build (or reuse) the project's ``build:`` image; return the concrete ref.

        Returns ``None`` when the project has no ``build:`` section. Blocks until
        the build finishes; raises ``RuntimeError`` on failure (with the structured
        error message) so the campaign preflight fails loudly.
        """
        spec, project_dir = self._build_spec_for(project, campaign_config)
        if spec is None:
            return None
        mgr = self._image_builds
        ref = mgr.start(spec, project_dir)
        status = self._await_build(ref.build_id)
        if status.phase not in ("succeeded", "cached"):
            err = status.error
            detail = f" ({err.message})" if err and err.message else ""
            raise RuntimeError(
                f"experiment image build '{spec.tag}' failed{detail}; "
                f"see the build log (build_id={ref.build_id})")
        return mgr.resolve_ref(spec, project_dir)

    def _await_build(self, build_id: str):
        mgr = self._image_builds
        while True:
            status = mgr.status(build_id)
            if status.done:
                return status
            time.sleep(1.0)

    def build_image(self, request) -> "ImageBuildRef":  # noqa: F821
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.service.image_build import validate_build_spec
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        spec, project_dir = self._build_spec_for(project, campaign_config)
        if spec is None:
            raise ValueError(
                "project has no 'build:' section — nothing to build (set a build: "
                "section and execution.image: build:<tag>)")
        problems = validate_build_spec(spec, project_dir)
        if problems:
            raise ValueError("invalid build: section:\n  - " + "\n  - ".join(problems))
        return self._image_builds.start(spec, project_dir)

    def get_image_build_status(self, build_id: str):
        return self._image_builds.status(build_id)

    def get_image_build_log(self, build_id: str, offset: int = 0):
        return self._image_builds.log(build_id, offset)

    def _postprocess(self, campaign_id, results_dir, state, entry):
        """Run analysis postprocessing for a just-finished local campaign.

        Advances the phase ``... → postprocessing → finished`` and generates the
        campaign's ``data.db``; a failure surfaces via status (phase ``failed``).
        """
        from robovast.common.logging_config import (
            add_campaign_log_handler, remove_campaign_log_handler)
        from robovast.results_processing.postprocessing import run_postprocessing
        # Capture the postprocessing narrative into its own phase file, which the
        # unified campaign log serves under the POSTPROCESSING divider. Thread-
        # isolated (same worker thread), so concurrent campaigns stay separate.
        log_path = Path(results_dir) / campaign_id / "_execution" / "postprocessing.log"
        handler = None
        try:
            handler = add_campaign_log_handler(str(log_path))
        except Exception:  # noqa: BLE001 - logging must never abort postprocessing
            logger.warning("Could not open postprocessing.log for %s", campaign_id,
                           exc_info=True)
        try:
            state.set_phase(Phase.POSTPROCESSING)
            ok, message = run_postprocessing(
                results_dir=results_dir, campaign=campaign_id,
                output_callback=logger.info)
            if ok:
                from robovast.results_processing.postprocessing import \
                    campaign_defines_postprocessing
                if campaign_defines_postprocessing(
                        str(Path(results_dir) / campaign_id)):
                    state.update(postprocessed=True)
                state.update(postprocessing_error=None)
                state.set_phase(Phase.FINISHED)
            else:
                # The runs finished — a postprocessing failure keeps phase=finished
                # (not a run failure) and records the reason on its own field, so it is
                # re-triggerable and distinct from a failed run. Mirrors the cluster
                # auto-chain in controller._chain_postprocessing.
                state.update(postprocessing_error=message, postprocessed=False)
                state.set_phase(Phase.FINISHED, stage=f"postprocessing failed: {message}")
        except Exception as e:  # noqa: BLE001 - surfaced via status
            logger.exception("Postprocessing for %s failed", campaign_id)
            state.update(postprocessing_error=failure_detail(e), postprocessed=False)
            state.set_phase(Phase.FINISHED, stage=f"postprocessing failed: {e}")
        finally:
            # Re-write the durable outcome to reflect the final postprocessing state
            # (_finish_campaign wrote one before this ran, when postprocessing was still
            # pending). Success or failure, one record now carries the accurate
            # postprocessed / postprocessing_error / share_error snapshot.
            self._record_outcome(campaign_id, results_dir, state)
            remove_campaign_log_handler(handler)

    def _record_outcome(self, campaign_id, results_dir, state):
        """Persist the failed campaign's terminal outcome to _execution/outcome.json.

        So a past/reaped local campaign still surfaces its reason via
        :meth:`_status_from_disk` — the same durable record the cluster controller
        writes, at the same campaign-relative path.
        """
        from robovast.common.campaign_data import write_execution_outcome
        try:
            write_execution_outcome(Path(results_dir) / campaign_id, state.snapshot())
        except OSError as e:
            logger.warning("Could not write outcome.json for %s: %s", campaign_id, e)

    def _record_campaign_stopped(self, campaign_id, results_dir, state, backend) -> None:
        """Persist a cooperatively-stopped campaign's terminal ``Status``.

        So the ``stopped`` phase survives a restart — otherwise a stopped campaign
        reconstructs from disk as an ambiguous ``finished``/``unknown``. The local home
        is the filesystem, so writing ``outcome.json`` is enough (``ClusterService``
        overrides this to also publish it to the object store).
        """
        self._record_outcome(campaign_id, results_dir, state)

    def get_status(self, campaign_id: str) -> Status:
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is not None:
            return entry.state.snapshot()
        # Not tracked in this process — reconstruct from disk (past campaign).
        return self._status_from_disk(campaign_id)

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        """Serve the campaign's unified infrastructure log from the campaigns root.

        Assembles the per-phase files (variation → run → postprocessing) under the
        campaign's ``_execution/`` into one divider-separated stream (see
        :func:`robovast.common.campaign_logs.assemble_log`). Local runs write those
        files in place and they grow there, so the same read serves a live and a
        finished campaign; ``eof`` is set once the campaign is no longer driven here.
        """
        from robovast.common.campaign_logs import assemble_log_from_dir
        from robovast.service.interface import LogChunk
        campaign_dir = self._campaigns_root() / campaign_id
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        eof = entry is None or self._is_done(entry)
        text, next_offset, eof = assemble_log_from_dir(campaign_dir, offset, eof)
        return LogChunk(text=text, next_offset=next_offset, eof=eof)

    #: Directories under a campaign that are not per-configuration results.
    _RESERVED_DIRS = frozenset({"_config", "_execution", "_transient"})

    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        """List the campaign's runs (local Docker fans a batch out into runs).

        Runs are discovered on disk as ``<config>/<run-number>`` directories (the
        same layout :func:`get_vast_configuration_info` reads); a run is
        ``completed``/``failed`` by its ``test.xml`` result, or ``running`` when the
        campaign is still live and the run has not produced one yet (local is
        sequential, so at most one). Pending (not-yet-started) runs have no directory,
        so they are counted from the controller's expected total but not listed.
        """
        from robovast.common.campaign_data import read_test_result
        campaign_dir = self._campaigns_root() / campaign_id
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        live = entry is not None and not self._is_done(entry)

        jobs: list[JobSummary] = []
        if campaign_dir.is_dir():
            config_dirs = sorted(
                d for d in campaign_dir.iterdir()
                if d.is_dir() and d.name not in self._RESERVED_DIRS
                and not d.name.startswith("."))
            for config_dir in config_dirs:
                run_dirs = sorted(
                    (d for d in config_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                    key=lambda d: int(d.name))
                for run_dir in run_dirs:
                    try:
                        status = "completed" if read_test_result(run_dir)["success"] \
                            else "failed"
                    except FileNotFoundError:
                        status = "running" if live else "failed"
                    jobs.append(JobSummary(
                        job_name=f"{config_dir.name}/{run_dir.name}",
                        status=status,
                        display_name=f"{config_dir.name} · run {run_dir.name}"))

        expected_total = 0
        if entry is not None:
            snap = entry.state.snapshot()
            expected_total = snap.runs.total if snap.runs else 0
        pending = max(0, expected_total - len(jobs)) if live else 0
        counts = JobCounts(
            running=sum(1 for j in jobs if j.status == "running"),
            pending=pending,
            completed=sum(1 for j in jobs if j.status == "completed"),
            failed=sum(1 for j in jobs if j.status == "failed"),
            total=len(jobs) + pending)
        return ListJobsResponse(jobs=jobs, counts=counts)

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a run's live ``job/logs/system.log`` (``job_name`` = ``<config>/<run>``).

        The run writes this file in place as it executes, so the same read serves a
        running and a finished run; ``eof`` is set once the run has a result or the
        campaign is no longer driven here.

        The container writes this to the JOB's artifact dir (``_jobs[/<batch>]/job-<j>``),
        not to the config/run dir -- ``<config>/<run>/logs/`` exists but stays empty, so
        reading there returns a silently blank job log even though the same output is
        visible in the campaign log (the local backend also folds container stdout into
        ``controller.log``). :func:`job_artifact_dir` resolves the real dir.
        """
        from robovast.common.campaign_data import read_test_result
        from robovast.common.execution import job_artifact_dir
        from robovast.common.safe_path import UnsafePathError, safe_join
        campaign_dir = self._campaigns_root() / campaign_id
        # job_name comes from a client, so confine it to the campaign (shared check).
        try:
            run_dir = safe_join(campaign_dir, job_name)
        except UnsafePathError as e:
            raise KeyError(str(e)) from e
        if not run_dir.is_dir():
            raise KeyError(f"job {job_name!r} not found in campaign {campaign_id!r}")
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        live = entry is not None and not self._is_done(entry)
        try:
            read_test_result(run_dir)
            run_done = True
        except FileNotFoundError:
            run_done = False
        try:
            job_dir = Path(job_artifact_dir(campaign_dir, job_name))
        except FileNotFoundError:
            # Before the first job starts there is no manifest yet; that is the
            # documented startup race, not a broken layout.
            return LogChunk(text="", next_offset=offset, eof=run_done or not live)
        return _read_log_slice(job_dir / "logs" / "system.log", offset,
                               eof=run_done or not live)

    def stop(self, campaign_id: str) -> ActionResult:
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(ok=False, message=f"campaign {campaign_id} not tracked here")
        entry.state.request_stop()
        self._kill_scenario_container()
        return ActionResult(ok=True, message="stop requested")

    def _kill_scenario_container(self) -> None:
        """Force-remove the single-flight scenario container so the worker unblocks.

        The backend's run script (in its own session) is blocked on ``docker
        compose``; removing the container makes it return promptly, then its
        ``stop_requested`` poll tears the rest down. Best-effort — a missing
        container is fine.
        """
        try:
            subprocess.run(["docker", "rm", "-f", self._CONTAINER_NAME],  # noqa: S603,S607
                           check=False, capture_output=True)
        except OSError as e:
            logger.warning("docker rm -f %s failed: %s", self._CONTAINER_NAME, e)

    def _terminate_running_campaigns(self, running) -> None:
        """Terminate the compute backing *running* campaigns so their workers unblock.

        The cooperative flag alone is not enough — a worker blocked in ``run_batch``
        must have its compute killed to return. Local override: one scenario
        container backs whichever campaign is running (single-flight), so a single
        force-remove covers them all. :class:`ClusterService` overrides this to delete
        each campaign's Kubernetes Jobs.
        """
        self._kill_scenario_container()

    def shutdown(self) -> None:
        """Stop any in-flight campaign so Ctrl+C on ``vast serve`` tears it down.

        Campaigns run on daemon worker threads, so a bare process exit would kill
        the worker mid-run and orphan its ``docker compose`` containers. On
        shutdown we request a cooperative stop of every still-running campaign
        (same path as :meth:`stop`) and briefly join the workers so their
        container-teardown traps complete before the process exits.
        """
        with self._lock:
            running = [e for e in self._campaigns.values() if not self._is_done(e)]
        if not running:
            return
        logger.info("Shutting down — stopping %d running campaign(s)", len(running))
        for entry in running:
            entry.state.request_stop()
        self._terminate_running_campaigns(running)
        for entry in running:
            if entry.thread is not None:
                entry.thread.join(timeout=self._SHUTDOWN_JOIN_SECONDS)
                if entry.thread.is_alive():
                    logger.warning(
                        "Campaign %s did not stop within %ds; exiting anyway",
                        entry.campaign_id, self._SHUTDOWN_JOIN_SECONDS)

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        request = request or ListCampaignsRequest()
        results_dir = self._campaigns_root()
        from robovast.common.execution import is_campaign_dir
        # Which campaigns exist = those persisted on disk ∪ those being driven now
        # (registered in-memory, perhaps without a directory yet — a just-launched
        # one is still building/starting). Not two sources of truth: each id is
        # resolved to a summary by the same precedence get_status uses (live
        # snapshot if tracked, else reconstruct from disk).
        disk = {d.name for d in results_dir.iterdir()
                if d.is_dir() and is_campaign_dir(d.name)} if results_dir.is_dir() else set()
        with self._lock:
            mem = set(self._campaigns)
        all_ids = sorted(disk | mem, reverse=True)  # newest first (id ends in timestamp)
        total = len(all_ids)
        window = all_ids[request.offset:request.offset + request.limit]
        summaries = [self._summary_for(cid) for cid in window]
        return ListCampaignsResponse(campaigns=summaries, total=total)

    def cleanup_campaign_data(self, request) -> ActionResult:
        return ActionResult(
            ok=False,
            message="cleanup-data is not supported by the local backend (no object "
                    "store); it applies to a cluster service.")

    def _ensure_deletable(self, campaign_id: str) -> None:
        """Validate that *campaign_id* is safe to delete, or raise.

        Two guards shared by the local and cluster transports before anything is
        removed:

        * The id must match the campaign naming pattern — this blocks a traversal
          value like ``..`` from ever reaching the ``rmtree`` / bucket delete and
          taking out the results root or an unrelated bucket (``ValueError`` → 400).
        * No live in-memory driver entry may exist — the authoritative "still
          running here" signal. Stop the campaign first (``RuntimeError`` → 409).
        """
        from robovast.common.execution import is_campaign_dir
        if not campaign_id or not is_campaign_dir(campaign_id):
            raise ValueError(
                f"Refusing to delete {campaign_id!r}: not a valid campaign id.")
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is not None and not self._is_done(entry):
            raise RuntimeError(
                f"Campaign {campaign_id!r} is still running; stop it before deleting.")

    def delete_campaign(self, campaign_id: str) -> ActionResult:
        """Delete the campaign's directory under the results root (see interface)."""
        import shutil
        self._ensure_deletable(campaign_id)
        campaign_dir = self._campaign_dir(campaign_id)
        existed = campaign_dir.is_dir()
        shutil.rmtree(campaign_dir, ignore_errors=True)
        with self._lock:
            self._campaigns.pop(campaign_id, None)
        return ActionResult(
            ok=True,
            message=(f"Deleted campaign {campaign_id!r}." if existed
                     else f"Campaign {campaign_id!r} had no local data; nothing to delete."))

    # -- postprocessing -----------------------------------------------------

    def _publish_config_edit(self, campaign_id: str) -> None:
        """Hook: persist an in-place edit of ``_config/<name>.vast`` beyond local disk.

        No-op locally — the local ``_config/`` is the durable copy. ``ClusterService``
        overrides this to upload the edited config to the object store, so a re-run's
        ``fetch_campaign(force=True)`` sees the edit instead of clobbering it.
        """

    def get_postprocessing(self, campaign_id: str):
        from robovast.service.interface import PostprocessingInfo
        from robovast.service.postprocessing_edit import get_postprocessing
        info = get_postprocessing(self._campaign_dir(campaign_id))
        return PostprocessingInfo(campaign_id=campaign_id, entries=info["entries"])

    def update_postprocessing(self, request):
        from robovast.service.interface import PostprocessingRevision
        from robovast.service.postprocessing_edit import update_postprocessing
        res = update_postprocessing(self._campaign_dir(request.campaign_id),
                                    request.entries)
        self._publish_config_edit(request.campaign_id)
        return PostprocessingRevision(campaign_id=request.campaign_id,
                                      entries=res["entries"])

    def get_postprocessing_source(self, campaign_id: str):
        from robovast.service.interface import PostprocessingSource
        from robovast.service.postprocessing_edit import get_postprocessing_source
        info = get_postprocessing_source(self._campaign_dir(campaign_id))
        return PostprocessingSource(campaign_id=campaign_id, content=info["content"])

    def update_postprocessing_source(self, request):
        from robovast.service.interface import PostprocessingSource
        from robovast.service.postprocessing_edit import update_postprocessing_source
        update_postprocessing_source(self._campaign_dir(request.campaign_id),
                                     request.content)
        self._publish_config_edit(request.campaign_id)
        return PostprocessingSource(campaign_id=request.campaign_id,
                                    content=request.content)

    def _dispatch_background(self, campaign_id: str, *, phase: str, work) -> ActionResult:
        """Run a post-run operation (postprocessing / share) as a tracked background
        campaign and return immediately, so the campaign view shows it live.

        Registers a fresh tracked entry set to *phase* — refusing if the campaign already
        has a live operation (the busy guard) — then runs ``work(state)`` on a daemon
        thread. ``work`` performs the operation, streams its own log, sets the final phase
        and records the durable outcome; this helper only owns the tracked-entry lifecycle
        and a crash safety-net. The entry's ``created_at`` is the campaign's real start
        time so a re-run does not make its listed ``started_at`` jump to now.
        """
        with self._lock:
            existing = self._campaigns.get(campaign_id)
            if existing is not None and not self._is_done(existing):
                return ActionResult(
                    ok=False,
                    message=f"campaign {campaign_id!r} is busy; an operation is "
                            "already running — wait for it to finish")
            state = ControllerState()
            state.update(campaign_id=campaign_id)
            state.set_phase(phase)
            entry = _LocalCampaign(campaign_id, str(self._campaigns_root()), state)
            entry.created_at = (self._campaign_started_at(self._campaign_dir(campaign_id))
                                or entry.created_at)
            self._campaigns[campaign_id] = entry

        def _worker():
            try:
                work(state)
            except Exception as e:  # noqa: BLE001 - surfaced via status; never crash the thread
                logger.exception("Background %s for %s failed", phase, campaign_id)
                state.update(error=failure_detail(e))
                state.set_phase(Phase.FINISHED)

        entry.thread = threading.Thread(
            target=_worker, name=f"robovast-{phase}-{campaign_id}", daemon=True)
        entry.thread.start()
        return ActionResult(
            ok=True, message=f"{phase} started; monitor it in the campaign view")

    def run_postprocessing(self, request) -> ActionResult:
        campaign_dir = self._campaign_dir(request.campaign_id)

        def work(state):
            from robovast.common.logging_config import (add_campaign_log_handler,
                                                        remove_campaign_log_handler)
            from robovast.execution.status_recovery import record_step_outcome
            from robovast.results_processing.postprocessing import run_postprocessing
            handler = None
            try:
                handler = add_campaign_log_handler(
                    str(campaign_dir / "_execution" / "postprocessing.log"))
            except Exception:  # pylint: disable=broad-except
                logger.warning("Could not open postprocessing.log for %s",
                               request.campaign_id, exc_info=True)
            try:
                # `campaign` scopes the work to this campaign; with no `vast_file` the run
                # reads the campaign's own `_config/<name>.vast` (edited in place).
                ok, message = run_postprocessing(
                    results_dir=str(campaign_dir.parent), campaign=request.campaign_id,
                    force=request.force, skip=list(request.skip or []))
            finally:
                remove_campaign_log_handler(handler)
            status = record_step_outcome(campaign_dir, postprocessing=(ok, message))
            state.update(postprocessed=status.postprocessed,
                         postprocessing_error=status.postprocessing_error)
            state.set_phase(Phase.FINISHED)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.POSTPROCESSING, work=work)

    def run_share(self, request) -> ActionResult:
        """(Re)trigger upload-to-share for one finished campaign, from disk.

        Dispatched as a tracked background op (works after a `vast serve` restart, no
        live entry needed). Local ``share_campaign`` writes the tar.gz to the archive
        dir; the durable ``share_error`` is cleared on success / set on failure.
        """
        campaign_dir = self._campaign_dir(request.campaign_id)

        def work(state):
            from robovast.execution.backends import RunOptions
            from robovast.execution.status_recovery import record_step_outcome
            backend = self._build_backend(ControllerState())
            options = RunOptions(gui=False, upload_to_share=True)
            try:
                backend.preflight_upload_to_share()
                backend.share_campaign(str(campaign_dir), options)
                ok, message = True, "upload-to-share complete"
            except Exception as e:  # noqa: BLE001 - surfaced via status + share_error
                ok, message = False, failure_detail(e)
            status = record_step_outcome(campaign_dir, share=(ok, message))
            state.update(share_error=status.share_error)
            state.set_phase(Phase.FINISHED)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.SHARING, work=work)

    # -- validation / preview / authoring help (config editor) --------------

    def validate_project(self, workspace_id: str, path: str = "") -> ValidationReport:
        from robovast.common.config_validation import validate_project_file
        try:
            project = self._resolve_project(workspace_id, path)
            result = validate_project_file(project.config_path)
        except Exception as e:  # noqa: BLE001 - editor sends in-progress YAML; never 500
            return ValidationReport(valid=False, problems=[
                ValidationProblem(stage="error", message=str(e))])
        return ValidationReport.model_validate(result)

    def preview_configurations(
        self, workspace_id: str, max_configs: int = 0, path: str = ""
    ) -> PreviewResponse:
        from robovast.common.config_generation import generate_scenario_variations
        project = self._resolve_project(workspace_id, path)
        try:
            campaign_data, _ = generate_scenario_variations(
                variation_file=project.config_path, output_dir=None)
        except Exception as e:  # noqa: BLE001 - surface resolution errors as 400
            raise ValueError(str(e)) from e
        configs = campaign_data["configs"]
        runs = campaign_data.get("execution", {}).get("runs", 1)
        remotes = _variation_remotes()
        items = [PreviewConfiguration(
                    name=c["name"], parameters=c.get("config", {}),
                    previews=_config_previews(c, remotes))
                 for c in configs]
        truncated = bool(max_configs) and len(items) > max_configs
        if truncated:
            items = items[:max_configs]
        return PreviewResponse(configs=len(configs), runs_per_config=runs,
                               total_trials=len(configs) * runs,
                               configurations=items, truncated=truncated)

    def get_config_schema(self) -> dict:
        from robovast.common.config import ConfigV1
        return ConfigV1.model_json_schema()

    def list_variation_types(self) -> VariationTypesResponse:
        from importlib.metadata import entry_points
        from robovast.common.plugin_schema import schema_from_object
        types = []
        for ep in entry_points(group="robovast.variation_types"):
            summary, params = "", []
            try:
                obj = ep.load()
                doc = (getattr(obj, "__doc__", "") or "").strip()
                summary = doc.splitlines()[0] if doc else ""
                for p in schema_from_object(obj) or []:
                    params.append(VariationTypeParam(
                        name=str(p.get("name", "")), type=str(p.get("type", "")),
                        required=bool(p.get("required", False)),
                        default=p.get("default"), description=p.get("description")))
            except Exception as e:  # noqa: BLE001 - skip a broken plugin, keep the rest
                logger.debug("variation type %s failed to introspect: %s", ep.name, e)
            types.append(VariationTypeInfo(name=ep.name, summary=summary, params=params))
        return VariationTypesResponse(types=sorted(types, key=lambda t: t.name))

    def _campaign_dir(self, campaign_id: str):
        # Campaigns all live under the shared results root (see _campaigns_root);
        # an absolute id is honored as-is for analysis of an arbitrary folder.
        if os.path.isabs(campaign_id):
            return Path(campaign_id)
        return self._campaigns_root() / campaign_id

    # -- results data query (eval viewer) -----------------------------------

    def describe_campaign_data(self, campaign_id: str) -> "DataDescribe":
        from robovast.results_processing.data_query import describe_data_db
        from robovast.service.interface import DataDescribe
        result = describe_data_db(self._data_dir(campaign_id))
        return DataDescribe(campaign_id=campaign_id, **result)

    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        extra_campaign_ids=None,
    ) -> "DataQueryResult":
        from robovast.results_processing.data_query import query_data_db
        from robovast.service.interface import DataQueryResult
        extra_dirs = {f"c{i + 1}": self._data_dir(cid)
                      for i, cid in enumerate(extra_campaign_ids or [])}
        result = query_data_db(self._data_dir(campaign_id), sql, max_rows,
                               extra_dirs=extra_dirs)
        return DataQueryResult(campaign_id=campaign_id, **result)

    def _data_dir(self, campaign_id: str):
        """Campaign dir holding data.db/campaign.db. Local: on disk (this transport
        overrides via ``_campaign_dir``); the cluster service fetches from the
        object store (``ClusterService`` overrides this)."""
        return self._campaign_dir(campaign_id)

    def resolve_data_dir(self, campaign_id: str):
        """Public seam: resolve a campaign's data dir (local disk or, on the cluster,
        an object-store fetch — ``ClusterService`` overrides ``_data_dir``). Used by the
        service's package-provided endpoint dispatch (see ``endpoint_plugin``) so plugins
        get local/cluster transparency without touching the private resolver."""
        return self._data_dir(campaign_id)

    def list_campaign_plots(self, campaign_id: str) -> "CampaignPlotsResponse":
        # Raw-load (not full validation) — reading declared plots must not depend on
        # the rest of the snapshot config being re-validatable.
        from robovast.common.config_validation import _safe_load
        from robovast.service.interface import CampaignPlotsResponse
        config_dir = Path(self._data_dir(campaign_id)) / "_config"
        vasts = sorted(config_dir.glob("*.vast")) if config_dir.is_dir() else []
        plots = []
        if vasts:
            cfg, _ = _safe_load(str(vasts[0]))
            for p in (((cfg or {}).get("evaluation") or {}).get("plots") or []):
                if isinstance(p, dict) and p.get("query"):
                    plots.append({"title": p.get("title", ""), "query": p["query"],
                                  "vega_lite": p.get("vega_lite") or {}})
        return CampaignPlotsResponse(campaign_id=campaign_id, plots=plots)

    def list_campaign_panels(self, campaign_id: str) -> "CampaignPanelsResponse":
        # Raw-load (not full validation) — reading declared panels must not depend on
        # the rest of the snapshot config being re-validatable. Reads the *effective*
        # .vast so in-place run-view visualization edits are reflected.
        from robovast.common.config_validation import _safe_load
        from robovast.service.interface import CampaignPanelsResponse
        from robovast.service.postprocessing_edit import campaign_vast
        from robovast.common.config import CUSTOM_PANEL_TYPE
        cfg, _ = _safe_load(str(campaign_vast(Path(self._campaign_dir(campaign_id)))))
        viz = (cfg or {}).get("visualization") or {}
        raw = viz.get("panels") or []
        # Each panel is a single-key mapping ``{<type>: <props-or-null>}`` (``playback:``
        # for a bare panel); flatten to the ``{type, ...fields}`` the web UI consumes.
        # A bare ``- playback`` (no colon) parses to the plain string ``"playback"``.
        # Attach a Module-Federation ``remote`` descriptor to panels rendered as remotes:
        # package panels (entry-point types shipping WEB_PANEL) and user ``custom`` panels.
        pkg_remotes = _panel_remotes()
        panels = []
        for i, entry in enumerate(raw):
            if isinstance(entry, str):
                ptype, props = entry, None
            else:
                (ptype, props), = entry.items()
            props = props or {}
            panel = {"type": ptype, **props}
            if ptype == CUSTOM_PANEL_TYPE:
                remote = props.get("remote")
                if remote:
                    rel = remote if remote.endswith(".js") \
                        else f"{remote.rstrip('/')}/remoteEntry.js"
                    panel["remote"] = {
                        "name": f"panel_{i}",
                        "remote_entry_url": Routes.campaign_panel_asset(campaign_id, rel),
                        "module": props.get("module") or "./panel",
                    }
            elif ptype in pkg_remotes:
                panel["remote"] = pkg_remotes[ptype]
            panels.append(panel)
        return CampaignPanelsResponse(
            campaign_id=campaign_id, panels=panels, timeline=viz.get("timeline"))

    def resolve_campaign_panel_asset(self, campaign_id: str, rel_path: str) -> str:
        """Resolve a ``custom`` panel's staged bundle file, confined to the campaign's
        immutable ``_config/`` snapshot. Raises ``ValueError`` (→ 400) on a path escape,
        ``KeyError`` (→ 404) if the file is missing."""
        base = (Path(self._data_dir(campaign_id)) / "_config").resolve()
        target = (base / rel_path).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise ValueError("path escapes the campaign config directory")
        if not target.is_file():
            raise KeyError(f"panel asset not found: {rel_path}")
        return str(target)

    def get_panels_source(self, campaign_id: str) -> "PanelsSource":
        from robovast.service.interface import PanelsSource
        from robovast.service.postprocessing_edit import get_visualization
        info = get_visualization(self._campaign_dir(campaign_id))
        return PanelsSource(campaign_id=campaign_id, content=info["content"])

    def update_panels_source(self, request) -> "PanelsSource":
        from robovast.service.interface import PanelsSource
        from robovast.service.postprocessing_edit import update_visualization
        update_visualization(self._campaign_dir(request.campaign_id), request.content)
        self._publish_config_edit(request.campaign_id)
        return PanelsSource(campaign_id=request.campaign_id, content=request.content)

    def get_run_file(
        self, campaign_id: str, config_name: str, run_id: int, path: str,
    ) -> bytes:
        # Confine the lookup to the run directory: the path comes from the URL, so a
        # ``..``/absolute path must not read outside the run's artifacts. Shared check
        # (also rejects a ``~`` prefix and symlink escapes, which the old
        # ``startswith`` test on the resolved path did not).
        from robovast.common.safe_path import safe_join
        run_dir = Path(self._data_dir(campaign_id)) / config_name / str(run_id)
        target = safe_join(run_dir, path)
        if not target.is_file():
            raise KeyError(
                f"no file {path!r} in run {config_name}/{run_id} of campaign {campaign_id}")
        return target.read_bytes()

    # Node levels the web Explorer tree can address (campaign → config → run). The
    # desktop's ``batch`` level is omitted: the web tree has no batch node.
    _VIS_LEVELS = ("run", "config", "campaign")

    def _visualization_workloads(self, campaign_id: str):
        """Parse ``evaluation.visualization`` from the snapshot ``.vast``.

        Returns ``({workload_name: {level: notebook_path}}, config_dir)`` — notebook
        paths are resolved against the ``_config`` snapshot dir, where the campaign's
        visualization notebooks are copied (see ``common.execution``).
        """
        from robovast.common.config_validation import _safe_load
        config_dir = Path(self._data_dir(campaign_id)) / "_config"
        vasts = sorted(config_dir.glob("*.vast")) if config_dir.is_dir() else []
        workloads: dict = {}
        if vasts:
            cfg, _ = _safe_load(str(vasts[0]))
            for view in (((cfg or {}).get("evaluation") or {}).get("visualization") or []):
                if not isinstance(view, dict):
                    continue
                for name, levels in view.items():
                    if not isinstance(levels, dict):
                        continue
                    notebooks = {
                        lvl: str(config_dir / levels[lvl])
                        for lvl in self._VIS_LEVELS
                        if isinstance(levels.get(lvl), str) and levels[lvl]
                    }
                    if notebooks:
                        workloads[name] = notebooks
        return workloads, config_dir

    def list_campaign_visualizations(
        self, campaign_id: str
    ) -> "CampaignVisualizationsResponse":
        from robovast.service.interface import (CampaignVisualization,
                                                CampaignVisualizationsResponse)
        workloads, _ = self._visualization_workloads(campaign_id)
        return CampaignVisualizationsResponse(
            campaign_id=campaign_id,
            workloads=[
                CampaignVisualization(
                    name=name,
                    levels=[lvl for lvl in self._VIS_LEVELS if lvl in notebooks])
                for name, notebooks in workloads.items()
            ])

    def render_campaign_notebook(
        self, campaign_id: str, workload: str, level: str,
        config_name: str = "", run_id=None, theme: str = "light",
    ) -> str:
        from robovast.results_processing.notebook_render import render_notebook_html
        workloads, _ = self._visualization_workloads(campaign_id)
        notebooks = workloads.get(workload)
        if not notebooks or level not in notebooks:
            raise KeyError(f"No '{level}' notebook for workload '{workload}'.")
        data_dir = self._node_data_dir(campaign_id, level, config_name, run_id)
        return render_notebook_html(notebooks[level], data_dir, theme=theme)

    def _node_data_dir(self, campaign_id: str, level: str, config_name: str, run_id):
        """The ``DATA_DIR`` for a selected node — the campaign/config/run directory."""
        base = Path(self._data_dir(campaign_id))
        if level == "campaign":
            return str(base)
        if level == "config":
            return str(base / config_name)
        if level == "run":
            return str(base / config_name / str(run_id))
        raise ValueError(f"Unknown visualization level: {level}")

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _is_done(entry: _LocalCampaign) -> bool:
        """Whether a campaign is over — no more work will happen on it.

        An entry is registered *before* its worker thread exists (create_campaign
        registers eagerly so the campaign lists from t=0, and shows phase
        ``building``/``starting`` during the image-build preflight). So "thread is
        None" no longer means "done" — a not-yet-started campaign is still live.
        Done means either a terminal phase, or a thread that existed and has ended
        (covering a crashed worker that never recorded a terminal phase)."""
        return is_terminal(entry.state.snapshot().phase) or (
            entry.thread is not None and not entry.thread.is_alive())

    def _summary_for(self, cid: str) -> CampaignSummary:
        from robovast.execution.status_recovery import \
            reconstruct_status_from_disk
        campaign_dir = self._campaigns_root() / cid
        with self._lock:
            entry = self._campaigns.get(cid)
        # One precedence rule, shared with get_status: a tracked campaign's live
        # ControllerState wins; otherwise reconstruct the Status from disk (the one
        # documented recovery path — it also derives `postprocessed` from data.db).
        # `started_at` follows the same rule: the entry's launch time when tracked,
        # else the campaign.db creation time; a recovered campaign with no readable
        # store has a genuinely unknown start time (None), not a swallowed error.
        if entry is not None:
            snap = entry.state.snapshot()
            started_at = entry.created_at
        else:
            snap = reconstruct_status_from_disk(campaign_dir)
            started_at = self._campaign_started_at(campaign_dir)
        counts = self._run_counts(campaign_dir, live=entry is not None)
        return CampaignSummary(
            campaign_id=cid, phase=snap.phase, postprocessed=snap.postprocessed,
            started_at=started_at,
            num_runs=counts["num_runs"], num_passed=counts["num_passed"],
            num_failed=counts["num_failed"] + counts["num_errors"])

    def _run_counts(self, campaign_dir: Path, *, live: bool) -> dict:
        """Pass/fail tallies for the summary, from ``campaign.db`` when possible.

        The fast path is one indexed ``GROUP BY`` over ``campaign.db``'s ``run``
        table (:func:`read_run_counts`) — no ``test.xml`` walk. A store predating
        that table (schema v1) returns nothing; for a *finished* campaign we then
        backfill the run rows from disk once (so the next call is fast) and, if the
        run table is still empty, fall back to the authoritative
        :func:`get_vast_configuration_info` disk walk so counts are never
        under-reported. A live campaign is left to the controller to fill in (no
        write-on-read to avoid store lock contention).
        """
        from robovast.common.store import read_run_counts

        counts = read_run_counts(campaign_dir)
        if counts is not None and (live or counts["num_runs"] > 0):
            return counts
        if not live:
            import sqlite3
            try:
                from robovast.common.campaign_index import backfill_run_rows
                if backfill_run_rows(campaign_dir):
                    counts = read_run_counts(campaign_dir)
            except (OSError, ValueError, TypeError, sqlite3.Error) as e:
                logger.debug("run-row backfill failed for %s: %s", campaign_dir, e)
        if counts is not None and counts["num_runs"] > 0:
            return counts
        return self._walk_counts(campaign_dir)

    @staticmethod
    def _walk_counts(campaign_dir: Path) -> dict:
        """Legacy fallback: derive counts by walking each run's ``test.xml``."""
        from robovast.common.campaign_data import get_vast_configuration_info
        try:
            info = get_vast_configuration_info(campaign_dir)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            info = {}
        return {
            "num_runs": info.get("num_runs", 0),
            "num_passed": info.get("num_passed", 0),
            "num_failed": info.get("num_failed", 0),
            "num_errors": info.get("num_errors", 0),
        }

    @staticmethod
    def _campaign_started_at(campaign_dir: Path) -> Optional[str]:
        """Real start time of the campaign as an ISO-8601 UTC string.

        Reads ``campaign.created_at`` — the timestamp the controller records at
        campaign creation (see :meth:`CampaignStore.create_campaign`) — from the
        campaign's own ``campaign.db``. Opened **read-only** so listing never
        migrates or locks a store a running campaign is still writing. Returns
        ``None`` when the store is absent or unreadable.
        """
        import sqlite3
        from datetime import datetime, timezone
        from robovast.common.store import STORE_FILENAME
        db = campaign_dir / STORE_FILENAME
        if not db.is_file():
            return None
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT created_at FROM campaign ORDER BY created_at LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row or row[0] is None:
            return None
        return datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat()

    def _status_from_disk(self, campaign_id: str) -> Status:
        from robovast.execution.status_recovery import \
            reconstruct_status_from_disk
        return reconstruct_status_from_disk(self._campaigns_root() / campaign_id)

