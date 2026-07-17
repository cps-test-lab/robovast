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

"""``RobovastClient`` — one interface, two transports.

The client is how the ``vast`` CLI, the MCP server, and (later) a web UI reach
RoboVAST operations without caring where they run:

* :class:`LocalTransport` — in-process, direct library calls. Executes local
  Docker campaigns by driving :func:`robovast.execution.controller.run_batch_campaign`
  in a background thread, serving live status from the same
  :class:`~robovast.execution.control_server.ControllerState` the cluster
  controller uses. This backs ``vast exec local run`` (mode 1). Campaigns die
  with the process — for persistence, run a service and use :class:`HTTPTransport`.
* :class:`HTTPTransport` — talks to a running ``robovast-service`` (local
  ``vast serve``, a remote VM, or an in-cluster deployment) over the HTTP
  contract in :class:`robovast.service.interface.Routes`.

Both implement :class:`~robovast.service.interface.RobovastInterface`, so a
caller holding a ``RobovastClient`` is transport-agnostic.
"""

import logging
import os
import subprocess
import threading
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Optional

from robovast.execution.control_server import (ControllerState, Status,
                                               failure_detail)
from robovast.service.interface import (ActionResult, CampaignRef,
                                        CampaignSummary, CreateCampaignRequest,
                                        CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest,
                                        FileContent, FileMeta,
                                        ListCampaignsRequest,
                                        ListCampaignsResponse,
                                        ListFilesResponse, ListWorkspacesResponse,
                                        PreviewConfiguration, PreviewResponse,
                                        RobovastInterface, Routes, UploadGrant,
                                        ValidationProblem, ValidationReport,
                                        VariationTypeInfo,
                                        VariationTypeParam, VariationTypesResponse,
                                        VersionInfo, WorkspaceInfo,
                                        WriteFileRequest)

logger = logging.getLogger(__name__)


def _robovast_version() -> str:
    try:
        return _pkg_version("robovast")
    except PackageNotFoundError:  # editable/source without metadata
        return "0.0.0+unknown"


def _variation_remotes() -> dict:
    """Map variation-type name → a Module-Federation remote descriptor, for the
    types that ship a web preview asset (declare ``WEB_PREVIEW``). Built-in types
    return nothing (they render host-native in the editor). Best-effort; a plugin
    that fails to import is simply skipped."""
    from importlib.metadata import entry_points
    remotes = {}
    for ep in entry_points(group="robovast.variation_types"):
        try:
            asset = getattr(ep.load(), "WEB_PREVIEW", None)
        except Exception as e:  # noqa: BLE001 - skip a broken plugin
            logger.debug("variation %s failed to load for web preview: %s", ep.name, e)
            continue
        if asset:
            remotes[ep.name] = {
                "name": ep.name,
                "remote_entry_url": Routes.variation_asset(ep.name, "remoteEntry.js"),
                "module": "./preview",
            }
    return remotes


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


class _LocalCampaign:
    """Bookkeeping for one in-process campaign: its live state + worker thread."""

    __slots__ = ("campaign_id", "results_dir", "state", "thread", "error")

    def __init__(self, campaign_id: str, results_dir: str, state: ControllerState):
        self.campaign_id = campaign_id
        self.results_dir = results_dir
        self.state = state
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None


class LocalTransport(RobovastInterface):
    """In-process implementation over the local Docker backend.

    ``workspace_id`` resolution is deferred to Phase A; for now an empty
    ``workspace_id`` resolves to the initialized CWD project (back-compat with
    ``vast exec local run``). A non-empty id is looked up once workspaces exist.
    """

    #: Local Docker is single-flight — the backend hardcodes container name
    #: ``robovast``, so two concurrent local campaigns would collide.
    _CONTAINER_NAME = "robovast"

    #: How long to wait, on shutdown, for a stopped campaign's worker thread to
    #: run its container teardown before we exit anyway. A hair over the backend's
    #: SIGTERM grace (``_STOP_GRACE_SECONDS`` = 15s) so the trap can complete.
    _SHUTDOWN_JOIN_SECONDS = 20

    def __init__(self, store=None, workspace_dirs=None):
        self._campaigns: dict[str, _LocalCampaign] = {}
        self._lock = threading.Lock()
        if store is None:
            from robovast.service.workspaces import WorkspaceStore
            store = WorkspaceStore(workspace_dirs=workspace_dirs)
        self.store = store

    # -- project resolution -------------------------------------------------

    def _resolve_project(self, workspace_id: str, vast_path: str = ""):
        """Resolve the project to act on: a workspace, or the CWD project.

        A non-empty ``workspace_id`` uses the workspace's authored project — the
        server-side inputs, so no client filesystem is involved. An empty id
        falls back to the initialized CWD project (``vast exec local run``
        back-compat). ``vast_path`` selects which ``.vast`` in a multi-``.vast``
        workspace (workspace-relative).
        """
        from robovast.common.cli.project_config import ProjectConfig
        if workspace_id:
            return self._project_for_workspace(workspace_id, vast_path)
        project = ProjectConfig.load()
        if project is None or not project.config_path or not project.results_dir:
            raise ValueError(
                "Project not initialized. Run 'vast init <config-file>' first.")
        return project

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
        return VersionInfo(robovast_version=_robovast_version(), backend="docker")

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
        return RunOptions(gui=False)

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
        campaign_id = campaign_id_for(campaign_config)
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

        state = ControllerState()
        entry = _LocalCampaign(campaign_id, results_dir, state)
        runs = request.runs if request.runs and request.runs > 0 else None
        options = self._run_options(request)

        def _worker():
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
            except Exception as e:  # noqa: BLE001 - surfaced via status
                logger.exception("Campaign %s failed", campaign_id)
                entry.error = str(e)
                state.update(error=failure_detail(e))
                state.set_phase("failed", stage=str(e))
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
        with self._lock:
            self._campaigns[campaign_id] = entry
        thread.start()
        logger.info("Started campaign %s (search=%s)", campaign_id, is_search)
        return CampaignRef(campaign_id=campaign_id)

    def _postprocess(self, campaign_id, results_dir, state, entry):
        """Run analysis postprocessing for a just-finished local campaign.

        Advances the phase ``... → postprocessing → finished`` and generates the
        campaign's ``data.db``; a failure surfaces via status (phase ``failed``).
        """
        from robovast.results_processing.postprocessing import run_postprocessing
        try:
            state.set_phase("postprocessing")
            ok, message = run_postprocessing(
                results_dir=results_dir, campaign=campaign_id,
                output_callback=logger.info)
            if ok:
                state.set_phase("finished")
            else:
                entry.error = message
                state.update(error=message)
                state.set_phase("failed", stage=f"postprocessing failed: {message}")
                self._record_outcome(campaign_id, results_dir, state)
        except Exception as e:  # noqa: BLE001 - surfaced via status
            logger.exception("Postprocessing for %s failed", campaign_id)
            entry.error = str(e)
            state.update(error=failure_detail(e))
            state.set_phase("failed", stage=f"postprocessing: {e}")
            self._record_outcome(campaign_id, results_dir, state)

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

    def get_status(self, campaign_id: str) -> Status:
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is not None:
            return entry.state.snapshot()
        # Not tracked in this process — reconstruct from disk (past campaign).
        return self._status_from_disk(campaign_id)

    _CONTROLLER_LOG = ("_execution", "controller.log")

    @staticmethod
    def _read_log_slice(path: "Path | str", offset: int, eof: bool):
        """Read a ``controller.log`` from *offset*; return a :class:`LogChunk`.

        Bytes are read (not text) so a mid-file offset can never split a character,
        then decoded leniently. A missing file is an empty chunk — normal for a
        campaign that has not written its first line yet.
        """
        from robovast.service.interface import LogChunk
        try:
            with open(path, "rb") as f:
                f.seek(max(0, offset))
                data = f.read()
        except FileNotFoundError:
            return LogChunk(text="", next_offset=offset, eof=eof)
        return LogChunk(text=data.decode("utf-8", "replace"),
                        next_offset=(max(0, offset) + len(data)), eof=eof)

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        """Serve the live ``controller.log`` from the shared campaigns root.

        Local runs write the file in place and it grows there, so the same read
        serves a live and a finished campaign; ``eof`` is set once the campaign is
        no longer being driven here.
        """
        path = self._campaigns_root() / campaign_id / Path(*self._CONTROLLER_LOG)
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        eof = entry is None or self._is_done(entry)
        return self._read_log_slice(path, offset, eof)

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
        # Single-flight: one scenario container backs whichever campaign is running.
        self._kill_scenario_container()
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
        if not results_dir.is_dir():
            return ListCampaignsResponse(campaigns=[], total=0)
        from robovast.common.execution import is_campaign_dir
        dirs = sorted(
            (d for d in results_dir.iterdir()
             if d.is_dir() and is_campaign_dir(d.name)),
            key=lambda d: d.name, reverse=True)
        total = len(dirs)
        window = dirs[request.offset:request.offset + request.limit]
        summaries = [self._summary_for(d) for d in window]
        return ListCampaignsResponse(campaigns=summaries, total=total)

    def upload_to_share(self, campaign_id: str,
                        overrides: Optional[dict] = None) -> ActionResult:
        return ActionResult(
            ok=False,
            message="upload_to_share is not supported by the local backend "
                    "(no external share); use 'vast results publish' instead.")

    # -- postprocessing -----------------------------------------------------

    def get_postprocessing(self, campaign_id: str):
        from robovast.service.interface import PostprocessingInfo
        from robovast.service.postprocessing_edit import get_postprocessing
        info = get_postprocessing(self._campaign_dir(campaign_id))
        return PostprocessingInfo(campaign_id=campaign_id, source=info["source"],
                                  entries=info["entries"], revisions=info["revisions"])

    def update_postprocessing(self, request):
        from robovast.service.interface import PostprocessingRevision
        from robovast.service.postprocessing_edit import update_postprocessing
        res = update_postprocessing(self._campaign_dir(request.campaign_id),
                                    request.entries)
        return PostprocessingRevision(campaign_id=request.campaign_id,
                                      revision=res["revision"], entries=res["entries"])

    def run_postprocessing(self, request) -> ActionResult:
        from robovast.results_processing.postprocessing import run_postprocessing
        from robovast.service.postprocessing_edit import effective_vast
        campaign_dir = self._campaign_dir(request.campaign_id)
        override = effective_vast(campaign_dir)
        # `campaign` scopes the work to this campaign (without it the run sweeps
        # every campaign under the results root); `vast_file` only chooses the
        # config, so the postprocessing override still applies.
        ok, message = run_postprocessing(
            results_dir=str(campaign_dir.parent), campaign=request.campaign_id,
            vast_file=str(override), force=request.force,
            skip=list(request.skip or []))
        return ActionResult(ok=ok, message=message)

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

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _is_done(entry: _LocalCampaign) -> bool:
        return entry.thread is None or not entry.thread.is_alive()

    def _summary_for(self, campaign_dir: Path) -> CampaignSummary:
        from robovast.common.campaign_data import get_vast_configuration_info
        cid = campaign_dir.name
        with self._lock:
            entry = self._campaigns.get(cid)
        phase = entry.state.snapshot().phase if entry else "finished"
        try:
            info = get_vast_configuration_info(campaign_dir)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            info = {}
        return CampaignSummary(
            campaign_id=cid, phase=phase,
            started_at=self._campaign_started_at(campaign_dir),
            num_runs=info.get("num_runs", 0), num_passed=info.get("num_passed", 0),
            num_failed=info.get("num_failed", 0) + info.get("num_errors", 0))

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
        from robovast.common.campaign_data import (get_vast_configuration_info,
                                                   read_execution_outcome)
        campaign_dir = self._campaigns_root() / campaign_id
        if not campaign_dir.is_dir():
            return Status(phase="unknown", campaign_id=campaign_id)
        # A failed campaign left its terminal outcome (phase + error) here — prefer
        # that over reconstructing an optimistic "finished".
        outcome = read_execution_outcome(campaign_dir)
        if outcome is not None:
            return outcome
        try:
            info = get_vast_configuration_info(campaign_dir)
            total = info.get("num_runs", 0)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            total = 0
        return Status(phase="finished", campaign_id=campaign_id,
                      runs={"completed": total, "total": total})


# ---------------------------------------------------------------------------
# HTTP transport (to a robovast-service: local vast serve, VM, or cluster)
# ---------------------------------------------------------------------------


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

    def stop(self, campaign_id: str) -> ActionResult:
        return ActionResult.model_validate(self._post(Routes.campaign_stop(campaign_id)))

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        request = request or ListCampaignsRequest()
        return ListCampaignsResponse.model_validate(
            self._get(Routes.CAMPAIGNS, limit=request.limit, offset=request.offset))

    def upload_to_share(self, campaign_id: str,
                        overrides: Optional[dict] = None) -> ActionResult:
        return ActionResult.model_validate(
            self._post(Routes.campaign_upload_to_share(campaign_id),
                       {"overrides": overrides or {}}))

    def get_postprocessing(self, campaign_id: str):
        from robovast.service.interface import PostprocessingInfo
        return PostprocessingInfo.model_validate(
            self._get(Routes.campaign_postprocessing(campaign_id)))

    def update_postprocessing(self, request):
        from robovast.service.interface import PostprocessingRevision
        return PostprocessingRevision.model_validate(self._post(
            Routes.campaign_postprocessing(request.campaign_id),
            json=request.model_dump()))

    def run_postprocessing(self, request) -> ActionResult:
        return ActionResult.model_validate(self._post(
            Routes.campaign_postprocessing_run(request.campaign_id),
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


# ---------------------------------------------------------------------------
# Facade / factory
# ---------------------------------------------------------------------------


def RobovastClient(service_url: str = "", timeout: float = 30.0) -> RobovastInterface:  # noqa: N802
    """Return a transport-agnostic client.

    * ``service_url`` set → :class:`HTTPTransport` to that ``robovast-service``.
    * empty (default) → :class:`LocalTransport` (in-process local Docker).

    Selection can also come from ``ROBOVAST_SERVICE_URL`` when ``service_url`` is
    empty, so a deployment can point every client at a service without code change.
    """
    url = service_url or os.environ.get("ROBOVAST_SERVICE_URL", "")
    if url:
        return HTTPTransport(url, timeout=timeout)
    return LocalTransport()
