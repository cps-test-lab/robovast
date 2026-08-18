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
:class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`
        subclasses this, reusing
its driver-hosting shape and overriding only the launch hooks.

Split out of the former single ``client`` module; ``client`` now re-exports
``LocalTransport`` so existing imports keep working.
"""

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Callable, Optional

from robovast.client import file_address
from robovast.client.safe_path import safe_join
from robovast.common import file_view
from robovast.common.host_display import require_host_display
from robovast.common.store import read_campaign_created_at, read_campaign_description
from robovast.execution.control_server import (ControllerState, Phase, Status, failure_detail,
                                               is_terminal)
from robovast.service.interface import (ActionResult, CampaignRef, CampaignSummary,
                                        CreateCampaignRequest, CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest, FileEntry,
                                        FileListing, FileMeta, FileText, ImageBuildRef, JobCounts,
                                        JobSummary, ListCampaignsRequest, ListCampaignsResponse,
                                        ListJobsResponse, ListWorkspacesResponse, LogChunk,
                                        PreviewConfiguration, PreviewResponse, ResourceUsage,
                                        RobovastInterface, Routes, UploadGrant, ValidationProblem,
                                        ValidationReport, VariationTypeInfo, VariationTypeParam,
                                        VariationTypesResponse, VersionInfo, WorkspaceInfo,
                                        WorldDescription, WriteFileRequest)

logger = logging.getLogger(__name__)


def _as_dir(rel_path: str) -> str:
    """The directory form of a relative path — what a listing echoes back, so that
    concatenating it with an entry yields that entry's address."""
    return f"{rel_path.rstrip('/')}/" if rel_path else ""


def _detail_entry(name: str, path: Path) -> FileEntry:
    """One ``detail=True`` listing entry, from a single ``stat()``.

    ``name`` already carries the directory mark from ``scan_dir``, so the kind is read
    from it rather than paying a second syscall to ask the filesystem again.
    """
    is_dir = name.endswith("/")
    st = path.stat()
    return FileEntry(name=name.rstrip("/"), is_dir=is_dir,
                     bytes=None if is_dir else st.st_size,
                     modified=st.st_mtime,
                     executable=None if is_dir else bool(st.st_mode & 0o111))


def _code_revision() -> str:
    """The revision this process's code was built from, or ``""`` when unavailable.

    Never raises and never substitutes the package version: an empty string is the honest
    answer for a deployment that cannot tell, and a caller checking whether its change is
    loaded needs that distinguishable from a revision that merely differs.
    """
    try:
        from robovast.common.execution import code_revision
        return code_revision()
    except Exception:  # noqa: BLE001 - diagnostics must not break the handshake
        return ""


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


def _panel_remotes(surface: str = "run") -> dict:
    """Package-provided panel type name → MF remote descriptor, for panels of *surface*.

    Types shipping a ``WEB_PANEL`` (e.g. ``robovast_nav``'s ``costmap``); see
    :func:`_plugin_remotes`. One entry-point group and one asset route serve both surfaces --
    which one a panel is for is its class's ``SURFACE`` -- so this filters rather than reading a
    second group.
    """
    from robovast.common.config import (PANEL_TYPES_GROUP,  # pylint: disable=import-outside-toplevel
                                        panel_type_names)
    wanted = panel_type_names(surface)
    remotes = _plugin_remotes(PANEL_TYPES_GROUP, "WEB_PANEL",
                              Routes.panel_types_asset, module_attr="PANEL_MODULE",
                              module_default="./panel")
    return {name: descriptor for name, descriptor in remotes.items() if name in wanted}


def _config_panel_specs(raw_config: dict, remotes: dict, workspace_id: str = "") -> list:
    """The ``visualization.config.panels`` a ``.vast`` declares, flattened for the UI.

    Same single-key shorthand as the run view (``- parameters:`` / ``- scene3d: {...}``), so the
    flattening is the schema's own; the remote descriptor is attached here for a
    package-provided panel exactly as ``list_campaign_panels`` does for the run view.

    Defaults to the two panels that need nothing from the campaign, so a ``.vast`` that declares
    no config view still shows what each configuration contains -- which is what the Config tab
    did before it had panels at all.
    """
    from robovast.common.config import (CUSTOM_PANEL_TYPE,  # pylint: disable=import-outside-toplevel
                                        flatten_panel_shorthand, visualization_block)
    declared = visualization_block(raw_config, "config", "panels")
    if not isinstance(declared, list) or not declared:
        declared = [{"parameters": None}, {"world": None}]
    panels = []
    for entry in declared:
        flat = flatten_panel_shorthand(entry)
        if not isinstance(flat, dict) or not flat.get("type"):
            continue
        panel = dict(flat)
        ptype = panel["type"]
        if ptype == CUSTOM_PANEL_TYPE:
            # A user-authored bundle sits next to the .vast, so it is served as an ordinary
            # workspace file -- no dedicated asset route, because /sources already addresses
            # exactly these bytes.
            rel = panel.get("remote")
            if rel and workspace_id:
                entry = rel if str(rel).endswith(".js") else f"{str(rel).rstrip('/')}/remoteEntry.js"
                panel["remote"] = {
                    "name": f"config_panel_{len(panels)}",
                    "remote_entry_url": f"/{file_address.SOURCES}/{workspace_id}/{entry}",
                    "module": panel.get("module") or "./panel",
                }
        elif ptype in remotes:
            panel["remote"] = remotes[ptype]
        panels.append(panel)
    return panels


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


def _config_view_contribution(config: dict, vast_dir: str) -> dict:
    """What the config view draws for one resolved config: ``{markers, files, errors}``.

    Asked of the variation *classes* here rather than carried out of composition, because
    the answer is a pure function of the resolved configuration
    (:meth:`Variation.config_view_data`). That keeps it off the ``isolate_plugins`` IPC
    path and out of the composition cache, and means a cache hit still gets its markers.
    """
    from robovast.common.config_generation import \
        _get_variation_classes  # pylint: disable=import-outside-toplevel
    from robovast.common.scene_markers import \
        collect_contributions  # pylint: disable=import-outside-toplevel
    block = config.get("_config_block") or {}
    try:
        classes = [cls for cls, _params in _get_variation_classes(block, vast_dir)]
    except Exception as exc:  # noqa: BLE001 - an unresolvable plugin is reported, not raised
        return {"markers": [], "files": {}, "errors": [f"variation types: {exc}"]}
    return collect_contributions(config, classes, vast_dir)


# ---------------------------------------------------------------------------
# Local (in-process) transport
# ---------------------------------------------------------------------------


def _show_gui_note(request, raw_config: dict) -> str:
    """Warn when ``show_gui`` was accepted but the project will still run headless.

    The X socket and ``DISPLAY`` are wired by the lane; what actually opens a window is
    the *scenario*, and for the simulators here that means a parameter this project has
    to flip via ``execution.local.gui.parameter_overrides``. A project without that block
    runs exactly as it would headless, which — without this note — is indistinguishable
    from a display that silently refused the connection.

    Not an error: a scenario is free to open its window unconditionally, and refusing
    would break that case.
    """
    if not getattr(request, "show_gui", False):
        return ""
    local = ((raw_config or {}).get("execution") or {}).get("local") or {}
    gui_block = local.get("gui") if isinstance(local, dict) else None
    if isinstance(gui_block, dict) and gui_block.get("parameter_overrides"):
        return ""
    return ("show_gui was accepted (the host display is wired into the container), but "
            "this project declares no execution.local.gui.parameter_overrides — so its "
            "scenario runs with whatever headless setting it defaults to and no window "
            "may appear. Add the block if its scenario takes a headless parameter.")


class _LocalCampaign:
    """Bookkeeping for one in-process campaign: its live state + worker thread."""

    __slots__ = ("campaign_id", "results_dir", "state", "thread", "error", "created_at",
                 "description", "created_by", "workspace_id")

    def __init__(self, campaign_id: str, results_dir: str, state: ControllerState,
                 description: str = "", workspace_id: str = "",
                 created_by: str = ""):
        from datetime import datetime, timezone
        self.campaign_id = campaign_id
        self.results_dir = results_dir
        self.state = state
        # Which workspace this campaign is *currently* reading its project from, so a
        # push can be refused while it runs. Live-only and deliberately never persisted:
        # ``write_launch_record`` leaves ``workspace_id`` out because a finished campaign
        # is workspace-independent, and that stays true. Empty for a launch with no
        # workspace behind it (a retrigger runs from its own staged copy).
        self.workspace_id = workspace_id
        # Held here as well as in campaign.db: the store row is written by the
        # controller, so between accepting the launch and that write (an image build
        # can make it minutes) this is the only copy — and for a campaign that fails
        # during the build it stays the only one.
        self.description = description
        self.created_by = created_by
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        # Real launch time, recorded the instant the campaign is registered — so a
        # just-launched campaign has a start time before the controller writes the
        # ``campaign`` DB row (seconds later). Same ISO-8601 UTC shape as
        # ``read_campaign_created_at`` reads back from disk, so both format identically.
        self.created_at: str = datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkspaceTarget:
    """What the service resolved a request to: one workspace's ``.vast``.

    Deliberately just the config path. This used to be a ``ProjectConfig`` — the CLI's
    type, where ``.robovast_project`` genuinely binds a config *and* a results dir — but
    the service synthesized one per call with a constant ``results_dir``, so the type
    implied a choice that was never made. Every campaign lands in the shared
    ``_campaigns_root()``; a caller needing that asks for it, rather than reading a copy
    off this object where it could look per-workspace.

    ``ProjectConfig`` stays where it means something: the CLI.

    The two optional fields say how this project differs from a workspace's, and they are here
    rather than as ``_launch_campaign`` parameters because this object already *is* the launch
    path's whole knowledge of the project — every hook it passes through reads
    ``config_path`` and nothing else, so a second channel would be a second thing to thread.
    A retrigger sets both; a workspace launch leaves both ``None`` and behaves exactly as
    before.
    """

    config_path: str
    #: Finish putting the project tree on disk. Called once, at the top of the campaign's
    #: worker thread — not in the request handler — so that a slow or doomed materialization
    #: becomes an inspectable ``failed`` campaign rather than a hung POST, which is the same
    #: reason the image build is awaited there. ``None`` means the tree is already on disk.
    materialize: Optional[Callable[[], None]] = None
    #: Undo :attr:`materialize`. Called from the worker's ``finally``, so **every** way a
    #: campaign can end reaches it — finished, failed, stopped, or a raise inside
    #: ``materialize`` itself. Paired with it rather than left to the caller because a
    #: materialized tree that nothing deletes is a disk leak per launch; the launch path knows
    #: when the tree stops being needed, and the target knows what deleting it means.
    discard: Optional[Callable[[], None]] = None
    #: ``{container name: image ref}`` to run verbatim, skipping the image build entirely.
    #: Data rather than a callback: it is resolved in the request handler, so a campaign whose
    #: image cannot be named fails the *request* with the reason instead of becoming a failed
    #: campaign someone has to go and inspect.
    pinned_images: Optional[dict] = None


class LocalTransport(RobovastInterface):
    """In-process implementation over the local Docker backend.

    A campaign always runs a **workspace's** ``.vast``: ``workspace_id`` is the only
    project binding this service accepts (see :meth:`_resolve_project`), and
    ``config_path``/``vast_path`` selects among several ``.vast`` files in that
    workspace. ``.robovast_project`` is a CLI-side concept and never selects what the
    service runs — its one remaining role is the *results root* (see
    :func:`~robovast.common.results_root.local_results_root`).
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

    #: Cap on cached job-log tails; oldest (LRU) are dropped past this.
    _JOB_LOG_CACHE_MAX = 128

    def __init__(self, store=None, workspace_dir=None):
        self._campaigns: dict[str, _LocalCampaign] = {}
        self._lock = threading.Lock()
        # Incremental job-log tails, so a log panel polling twice a second folds in only
        # each container's delta instead of re-reading whole files. LRU-bounded so a
        # long-lived service does not accumulate buffers. Shared with ClusterService,
        # which caches a PodLogTail in the same map (see _new_job_log_tail).
        self._job_log_tails: "OrderedDict[tuple, object]" = OrderedDict()
        self._job_log_guard = threading.Lock()
        # Container exec gets its own lock: creating its manager reaps a stray container
        # first, and on the cluster that waits for a pod to finish terminating. Doing
        # that under the campaign lock would stall every status read and campaign start
        # for as long as the wait takes.
        self._exec_lock = threading.Lock()
        self._exec_mgr = None
        self._usage_lock = threading.Lock()
        self._usage_cache: "tuple[float, ResourceUsage] | None" = None
        # campaign_id -> recorded start time (see _started_at_for). Only known values
        # are held, and a recorded one never changes, so no invalidation is needed.
        self._started_at_cache: dict[str, str] = {}
        # campaign_id -> recorded description (see _description_for). Same contract as
        # the start-time cache: write-once values only, so no invalidation is needed.
        self._description_cache: dict[str, str] = {}
        self._created_by_cache: dict[str, str] = {}
        # Prime psutil's non-blocking CPU sampler so the first resource_usage()
        # reading reflects real load instead of the 0.0 a cold sampler returns.
        import psutil  # pylint: disable=import-outside-toplevel
        psutil.cpu_percent(interval=None)
        if store is None:
            from robovast.service.workspaces import WorkspaceStore
            store = WorkspaceStore(workspace_dir=workspace_dir)
        self.store = store
        self._sweep_staged_projects()

    def _sweep_staged_projects(self) -> None:
        """Collect staged retrigger trees a killed service left behind.

        Start-up is the only moment this is needed: a campaign's worker releases its own tree
        on every exit, so what survives to here was orphaned by something that ran no
        ``finally``. Best-effort — a service must start even if scratch space cannot be read.
        """
        from robovast.service import retrigger
        try:
            retrigger.sweep_orphans(self.store.registry.root, self._campaigns_root())
        except OSError as e:
            logger.warning("Could not sweep staged retrigger projects: %s", e)

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
                "upload one with 'vast workspace init <dir>'. "
                "('.robovast_project' / 'vast init' binds the "
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

        The precedence lives in :func:`~robovast.common.results_root.local_results_root`,
        shared with the MCP results reader so the two cannot disagree about where a
        campaign is.

        Pure path resolver — the dir is materialized lazily by ``CampaignStore``
        on first run, so simply asking where campaigns live (e.g. from
        :class:`ClusterService`, whose results live in the object store) never
        creates a stray local directory.
        """
        from robovast.common.results_root import local_results_root
        return local_results_root(self.store.registry.root)

    def _project_for_workspace(self, workspace_id: str, vast_path: str = ""):
        """Resolve which ``.vast`` a workspace runs, as a :class:`WorkspaceTarget`.

        A workspace may hold **several** ``.vast`` files. ``vast_path`` (a
        workspace-relative path, confined like every other file op) selects one;
        when omitted, the sole ``.vast`` is used, and if there are several a clear
        error names the candidates so the caller can pass ``vast_path``.

        Results are **not** part of this answer: every campaign lands in the shared
        :meth:`_campaigns_root`, so a caller that needs the results root asks for it
        directly rather than reading it off a per-call object where it was always the
        same constant.
        """
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
            from robovast.client.workspaces import PINNED_SKIP_DIRS
            vasts = [
                v for v in sorted(project_dir.rglob("*.vast"))
                if not any(part.startswith(".") or part in PINNED_SKIP_DIRS
                           for part in v.relative_to(project_dir).parts)]
            if not vasts:
                raise ValueError(
                    f"workspace {workspace_id!r} has no .vast file; "
                    "write one with write_file() first")
            if len(vasts) > 1:
                rel = ", ".join(v.relative_to(project_dir).as_posix() for v in vasts)
                raise ValueError(
                    f"workspace {workspace_id!r} has {len(vasts)} .vast files ({rel}); "
                    "specify which with the path/config_path argument")
            config_path = vasts[0]
        return WorkspaceTarget(config_path=str(config_path))

    # -- workspaces ---------------------------------------------------------

    def create_workspace(self, request: CreateWorkspaceRequest) -> WorkspaceInfo:
        entry = self.store.registry.create(request.name)
        if request.from_campaign:
            try:
                self._seed_from_campaign(entry["workspace_id"], request.from_campaign)
            except BaseException:
                # A half-populated workspace is worse than none: it would sit in the dropdown
                # looking like a project, and the caller was told the create failed.
                self.store.registry.delete(entry["workspace_id"])
                raise
        return WorkspaceInfo.model_validate(entry)

    def _seed_from_campaign(self, workspace_id: str, campaign_id: str) -> None:
        """Fill a new workspace with *campaign_id*'s frozen ``_config/``, reconstructed.

        Not a directory copy: ``_config/`` archives the scenario at its basename while
        ``execution.scenario_file`` may declare a subdirectory path, so a copied tree would fail
        config generation with "scenario file not found". ``retrigger.reconstruct_project`` is
        exactly this rebuild, shared with the retrigger path rather than reimplemented beside it.

        An incomplete snapshot is refused, not silently seeded: a workspace short a file the
        original run used would look like that campaign's project and launch a different one.
        The reason names the files, so the caller can author them and try again.
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.common.results_utils import campaign_vast
        from robovast.service import retrigger

        source_dir = Path(self._retrigger_source_dir(campaign_id))
        try:
            vast_path = campaign_vast(source_dir)
        except ValueError as e:
            raise ValueError(
                f"campaign {campaign_id!r} froze no configuration under _config/, so there is "
                f"nothing to create a workspace from ({e}). A campaign that failed before its "
                f"configuration was frozen has none to copy.") from e

        project_dir = self.store.registry.project_dir(workspace_id)
        retrigger.reconstruct_project(source_dir, project_dir,
                                      validate_config(load_config(str(vast_path))))
        missing = retrigger.missing_run_files(source_dir, project_dir)
        if missing:
            raise ValueError(
                f"campaign {campaign_id!r} froze a configuration missing {len(missing)} file(s) "
                f"its own run used: {', '.join(sorted(missing))}. A workspace seeded from it "
                f"would name that campaign's configuration while running a different one, so "
                f"this refuses instead.")

    def list_workspaces(self) -> ListWorkspacesResponse:
        busy = self._workspaces_in_use()
        return ListWorkspacesResponse(workspaces=[
            WorkspaceInfo.model_validate(
                {**e, "running_campaigns": busy.get(e["workspace_id"], [])})
            for e in self.store.registry.list()])

    def _workspaces_in_use(self) -> dict[str, list[str]]:
        """Live campaigns per workspace — ``{workspace_id: [campaign_id, ...]}``.

        A campaign reads its project out of the workspace for its whole life (a search
        campaign re-composes from it every generation), so overwriting one mid-run
        changes a running experiment underneath itself. Only this process knows the
        pairing, and only while it lasts — which is exactly the question a client has
        to be able to ask before it pushes.
        """
        in_use: dict[str, list[str]] = {}
        with self._lock:
            entries = list(self._campaigns.values())
        for entry in entries:
            if entry.workspace_id and not self._is_done(entry):
                in_use.setdefault(entry.workspace_id, []).append(entry.campaign_id)
        return in_use

    def get_workspace(self, workspace_id: str) -> WorkspaceInfo:
        return WorkspaceInfo.model_validate(self.store.registry.require(workspace_id))

    def delete_workspace(self, workspace_id: str) -> ActionResult:
        self.store.registry.delete(workspace_id)
        return ActionResult(ok=True, message=f"workspace {workspace_id} deleted")

    # -- files (one address space) ------------------------------------------
    # ``/results/<campaign>/<path>`` and ``/sources/<workspace>/<path>``, each confined
    # against **its own** root: a results address must never resolve inside a workspace,
    # or the read-only tree would inherit the writable one's permissions.
    #
    # The strings a caller passes here are the URLs ``app.py`` serves, so this is where
    # the address space is actually resolved for every surface (MCP, CLI, web UI).

    def _address_parts(self, address: str, *, for_write: bool = False):
        """Parse an address into ``(namespace, canonical owner, rel_path)``.

        Separate from :meth:`_address_target` because the write operations hand the
        path straight back to the store, which resolves it again — computing it here
        would take the registry's lock a second time to produce a value nobody reads.

        Raises ``ValueError`` for a malformed or read-only-violating address and
        ``KeyError`` for an unknown workspace — the app maps those to 400 / 404.
        """
        namespace, owner, rel = file_address.parse_address(address)
        if for_write:
            file_address.require_writable(address, namespace)
        if namespace == file_address.SOURCES:
            # Canonicalize once: an address may name a workspace by name, and the
            # address echoed back must be the one a caller can use again.
            owner = self.store.registry.require(owner)["workspace_id"]
        return namespace, owner, rel

    def _address_target(self, address: str, *, for_write: bool = False):
        """Resolve an address to ``(namespace, owner, rel_path, absolute path)``."""
        namespace, owner, rel = self._address_parts(address, for_write=for_write)
        if namespace == file_address.SOURCES:
            # Through the store, so ``/sources`` inherits its confinement rather than
            # re-deriving the root here.
            return namespace, owner, rel, self.store.resolve(owner, rel)
        root = Path(self._whole_campaign_dir(owner))
        if not root.is_dir():
            raise KeyError(f"no campaign {owner!r} in the results tree")
        return namespace, owner, rel, (safe_join(root, rel) if rel else root)

    def list_files(self, address: str, recursive: bool = False, detail: bool = False,
                   offset: int = 0, limit: int = 100) -> FileListing:
        namespace, owner, rel, target = self._address_target(address)
        if target.is_file():
            # Not a 404: the thing exists, the caller asked the wrong question of it.
            raise ValueError(
                f"{address!r} is a file, not a directory — read it instead")
        if not target.is_dir():
            raise KeyError(f"no directory at {address!r}")
        skip = (self.store.skip_entry(owner)
                if namespace == file_address.SOURCES else None)
        return file_view.build_listing(
            FileListing,
            file_address.format_address(namespace, owner, _as_dir(rel)),
            file_view.scan_dir(target, recursive=recursive, skip=skip),
            recursive=recursive, detail=detail, offset=offset, limit=limit,
            detail_fn=_detail_entry)

    @staticmethod
    def _require_file(address: str, target: Path) -> None:
        """Refuse a non-file, saying which kind of 'no' it is.

        A directory is not a missing file — the caller asked the wrong question of
        something that exists, so it is a 400 pointing at the listing, not a 404.
        """
        if target.is_dir():
            raise ValueError(
                f"{address!r} is a directory, not a file — list it instead "
                "(append '/')")
        if not target.is_file():
            raise KeyError(f"no file at {address!r}")

    def read_file(self, address: str, lines: int = 200, offset: int = 0) -> FileText:
        namespace, owner, rel, target = self._address_target(address)
        self._require_file(address, target)
        return FileText(address=file_address.format_address(namespace, owner, rel),
                        **file_view.read_text_page(target, lines, offset))

    def read_file_bytes(self, address: str) -> bytes:
        _, _, _, target = self._address_target(address)
        self._require_file(address, target)
        return target.read_bytes()

    def local_file(self, address: str) -> Path:
        """The file's real path on this host, for the HTTP layer to stream.

        Lets a response be a ``FileResponse`` — streamed, with ``Range`` and
        conditional-request handling — instead of read whole into memory. A campaign's
        rosbag is tens of megabytes and up, and ``read_file_bytes`` buffers all of it per
        request just to hand it back; ``Range`` is also what lets a browser *seek* a
        ``.webm`` rather than download it before playing.

        **Every transport implements this**, which is why callers must not test for its
        presence: ``ClusterService`` subclasses this one, so the attribute is never absent and a ``getattr(impl, "local_file", None) is None``
        check can only ever be False. What differs between the lanes is the *cost* of
        answering, and each says so: here the file is already on disk, and the cluster
        fetches the one object behind the address.
        """
        _, _, _, target = self._address_target(address)
        self._require_file(address, target)
        return target

    @staticmethod
    def _written(owner: str, meta: dict) -> FileMeta:
        """Attach the address to a store's file metadata.

        The **only** place a ``FileMeta`` is built, deliberately: the store below knows
        paths and workspace ids, not addresses, so letting it construct one is how the
        upload path came to return metadata with no address at all — a 400 on every
        non-inline write over HTTP, invisible to the in-process transport that discards
        the result.
        """
        return FileMeta(
            address=file_address.format_address(file_address.SOURCES, owner,
                                                meta["path"]),
            bytes=meta["bytes"], sha256=meta["sha256"],
            executable=meta["executable"])

    def write_file(self, request: WriteFileRequest) -> FileMeta:
        _, owner, rel = self._address_parts(request.address, for_write=True)
        return self._written(owner, self.store.write_file(owner, rel, request.content))

    def edit_file(self, request: EditFileRequest) -> FileMeta:
        _, owner, rel = self._address_parts(request.address, for_write=True)
        return self._written(owner, self.store.edit_file(
            owner, rel, request.old_string, request.new_string))

    def redeem_upload(self, token: str, data: bytes) -> FileMeta:
        """Redeem a one-time upload grant and report the address that was written.

        Not on the interface: a remote client PUTs its bytes at the grant's URL rather
        than calling this, so only the process holding the workspace store can serve it
        (``app.py`` probes for it the way it probes ``resolve_data_dir``).
        """
        meta = self.store.write_upload(token, data)
        return self._written(meta["workspace_id"], meta)

    def delete_file(self, address: str) -> ActionResult:
        _, owner, rel = self._address_parts(address, for_write=True)
        self.store.delete_file(owner, rel)
        return ActionResult(ok=True, message=f"deleted {address}")

    def create_upload(self, request: CreateUploadRequest) -> UploadGrant:
        _, owner, rel = self._address_parts(request.address, for_write=True)
        grant = self.store.create_upload(owner, rel, executable=request.executable)
        return UploadGrant(token=grant["token"], path=grant["path"],
                           expires_in=grant["expires_in"])

    # -- interface ----------------------------------------------------------

    def version(self) -> VersionInfo:
        # The filesystem roots are advertised because this lane *is* local disk, so a
        # caller on the same host can read results with its own tools instead of
        # relaying every byte through the interface. This answers only half the
        # contract -- whether the *service* has openable paths. ``app.py``'s version
        # route answers the other half and blanks them for a non-loopback caller,
        # which a transport cannot see.
        # `can_build_images=True` unconditionally, and deliberately **without probing
        # Docker**. This lane builds with `docker buildx --load` into the local daemon:
        # there is no registry, no Ingress and nothing an operator can misconfigure, so
        # the capability is a property of the lane rather than of this deployment. A dead
        # daemon is *liveness* — `resource_usage`, the run preflight and `vast doctor`
        # each answer that — and `check_docker_access` shells out with a 15 s timeout,
        # which is the last thing this call should ever wait on. `_api_server_url` in the
        # cluster lane's version() refuses to dial for the same reason.
        return VersionInfo(robovast_version=_robovast_version(),
                           code_revision=_code_revision(), backend="docker",
                           can_build_images=True,
                           results_root=str(self._campaigns_root()),
                           sources_root=str(self.store.registry.root))

    def resource_usage(self) -> ResourceUsage:
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
                usage = cached[1]
            else:
                usage = self._compute_resource_usage()
                self._usage_cache = (time.monotonic(), usage)
        # Attached outside the cache: a held exec container comes and goes far faster
        # than the sampling window, and a stale "still holding 6 GB" would be worse than
        # not reporting it at all.
        return usage.model_copy(update={"exec_container": self._exec_container_state()})

    def _exec_container_state(self):
        """The held exec container, or ``None`` — without creating a manager.

        Reading capacity must not start a reaper thread or reap a stray container as a
        side effect; a service that never execs should behave exactly as before.
        """
        if self._exec_mgr is None:
            return None
        try:
            return self._exec_mgr.state()
        except Exception as e:  # noqa: BLE001 - capacity must still be answerable
            logger.debug("could not read exec container state: %s", e)
            return None

    def _compute_resource_usage(self) -> ResourceUsage:
        """Local host capacity + live utilization via ``psutil``.

        ``cpu_percent(interval=None)`` is non-blocking — it averages CPU load since
        the previous call rather than sleeping per request. The first reading after
        the process starts is ``0.0`` (no prior sample); the TTL cache means that is
        replaced by a real value on the next window. Overridden by
        :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`.
        """
        import psutil  # pylint: disable=import-outside-toplevel
        vm = psutil.virtual_memory()
        cores = psutil.cpu_count(logical=True)
        jobs_running, jobs_pending = self._scenario_job_tally()
        return ResourceUsage(
            backend="docker",
            cpu_capacity=float(cores),
            cpu_used=cores * psutil.cpu_percent(interval=None) / 100.0,
            memory_capacity_bytes=vm.total,
            memory_used_bytes=vm.used,
            parallel_runs=False,   # Docker backend is single-flight: runs are sequential
            jobs_running=jobs_running,
            jobs_pending=jobs_pending,
        )

    def _scenario_job_tally(self) -> "tuple[int, int]":
        """``(running, pending)`` scenario runs across this lane's live campaigns.

        Read from the controller snapshot, not from disk: :meth:`list_jobs` discovers
        runs as ``<config>/<run>/`` directories and calls any without a ``test.xml``
        ``running`` while the campaign is live, so a run that died without writing one
        would be reported as still executing for the rest of the campaign. The
        snapshot is what the controller actually believes, and costs no I/O.

        ``running`` is 0 or 1 by construction, not by clamping: this lane is
        single-flight (``parallel_runs=False`` — the Docker backend hardcodes one
        container name), so a batch has at most one run executing, and the phases
        before ``running`` (``initializing``/``building``/``variation``) have none.
        ``pending`` is the rest of the current batch — accepted work that is not
        executing, the same population the cluster lane's ``waiting``+``pending`` Jobs
        are, so a consumer can read the pair without knowing which lane answered.

        Summed over live campaigns even though :meth:`_guard_new_campaign` admits one
        at a time, so this stays correct if that guard ever relaxes.
        """
        with self._lock:
            entries = [e for e in self._campaigns.values() if not self._is_done(e)]
        running = pending = 0
        for entry in entries:
            snap = entry.state.snapshot()
            active = 1 if snap.phase == Phase.RUNNING else 0
            total = snap.runs.total if snap.runs else 0
            done = snap.runs.completed if snap.runs else 0
            running += active
            pending += max(0, total - done - active)
        return running, pending

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

    #: Whether this deployment can put a window on a screen. Only the local Docker lane
    #: can — it is the one whose ``docker`` process sits at the serve host's display.
    #: :class:`ClusterService` flips this; a multi-backend service keeps it, because its
    #: cluster-routed requests reach the cluster lane's own copy of this check.
    _SUPPORTS_SHOW_GUI = True

    def _admit_show_gui(self, request) -> None:
        """Refuse ``show_gui`` unless this deployment can actually show a window.

        Called at request admission, before an image build or a campaign directory
        exists, so a refusal leaves nothing behind. Accepting it and rendering nowhere is
        the failure this prevents: the run looks fine and simply never draws.
        """
        if not getattr(request, "show_gui", False):
            return
        if not self._SUPPORTS_SHOW_GUI:
            raise ValueError(
                "show_gui is only available on a local `vast serve` running the local "
                "Docker backend. This service executes on a cluster, where there is no "
                "display to open a window on — re-run without it, or run the campaign "
                "on a local service.")
        require_host_display(what="show_gui")

    def _run_options(self, request) -> "RunOptions":  # noqa: F821
        from robovast.execution.backends import RunOptions

        # Local backend: upload_to_share just writes a tar.gz to _archives/ (no
        # external provider). Honour the toggle so it works for a local run too.
        # ``show_gui`` -> ``gui`` is the one place the request's outward name meets the
        # run machinery's: the generated run.sh's flag is ``--no-gui`` and cannot be
        # renamed with it, so the boundary is here rather than spread over both.
        return RunOptions(gui=bool(getattr(request, "show_gui", False)),
                          upload_to_share=bool(getattr(request, "upload_to_share", False)),
                          image_project=getattr(request, "image_project", "") or None,
                          image_project_tag=getattr(request, "image_project_tag", "") or None)

    def _campaign_context(self, campaign_id: str, project):
        """Per-campaign setup entered *inside* the worker thread.

        A context manager, so anything thread-scoped (the cluster's aux-pod
        container-runner factory) is established where the composition that reads
        it runs, and torn down when the campaign ends. No-op locally.
        """
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
        # Before anything is resolved or created: a lane that cannot show a window, or a
        # serve host with no display, must refuse rather than launch a windowless run.
        self._admit_show_gui(request)
        target = self._resolve_project(request.workspace_id, request.config_path)
        return self._launch_campaign(request, target)

    def retrigger_campaign(self, campaign_id: str) -> CampaignRef:
        """Launch a new campaign from *campaign_id*'s own records (see the interface).

        A thin orchestrator: :mod:`robovast.service.retrigger` decides everything about the
        source, and :meth:`_launch_campaign` runs it. What belongs here is only the ordering
        that needs the transport — which directory this lane reads the source from, the
        single-flight guard, and making sure a refusal leaves nothing staged behind.
        """
        from robovast.service import retrigger
        from robovast.service.interface import DESCRIPTION_MAX_LEN
        source_dir = self._retrigger_source_dir(campaign_id)
        plan = retrigger.prepare(
            source_dir, campaign_id,
            workspaces_root=self.store.registry.root,
            description_limit=DESCRIPTION_MAX_LEN,
            request_model=CreateCampaignRequest)
        # From here the staged tree exists, so every exit has to release it. The worker's
        # ``finally`` covers the campaign's whole life, but not this stretch: the most likely
        # failure of all -- the single-flight guard refusing because a campaign is already
        # running -- happens before there is a worker to have a ``finally``.
        try:
            self._guard_new_campaign()
            return self._launch_campaign(plan.request, WorkspaceTarget(
                config_path=plan.config_path,
                materialize=plan.materialize,
                discard=plan.discard,
                pinned_images=plan.pinned_images))
        except BaseException:
            plan.discard()
            raise

    def _launch_campaign(self, request: CreateCampaignRequest,
                         target: WorkspaceTarget) -> CampaignRef:
        """Launch *request* against an already-resolved project; return as soon as it is named.

        Split out of :meth:`create_campaign` so a campaign can be launched from a project the
        service resolved some other way — currently a retrigger's staged copy of a previous
        campaign's frozen config (see :meth:`retrigger_campaign`). Everything a non-workspace
        project needs to say travels on *target*, so this signature stays the launch contract
        rather than growing a mode flag per caller.
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.execution.controller import (campaign_id_for, run_batch_campaign,
                                                   run_search_campaign)

        # The raw mapping as well as the validated model: ``execution.local`` is read
        # from the mapping everywhere (``ExecutionConfig`` does not model it), so the
        # show_gui note has to look there too rather than at the model.
        raw_config = load_config(target.config_path)
        campaign_config = validate_config(raw_config)
        # The shared root, asked for directly: it never varied per workspace.
        results_dir = str(self._campaigns_root())
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

        # Named from the instant the campaign is accepted, not from when the controller
        # starts: readers key their log and job reads off this field, and a campaign that
        # is still waiting for its image — or that failed in its build — never reaches a
        # controller at all. Leaving it null until then hid the build's own log behind a
        # status that did not admit which campaign it described.
        state = ControllerState(campaign_id=campaign_id)
        entry = _LocalCampaign(campaign_id, results_dir, state,
                               description=request.description,
                               workspace_id=request.workspace_id,
                               created_by=request.created_by)
        runs = request.runs if request.runs and request.runs > 0 else None
        options = self._run_options(request)
        # Who ends the campaign. The builders' finish tail is outermost only when
        # nothing of the campaign happens after it returns — which is exactly the
        # transport that does *not* postprocess in-process. Derived from that predicate
        # rather than set per transport, so a new transport cannot forget it and leave
        # its campaigns either ending early or never ending at all.
        options.finalize_phase = not self._postprocess_in_process()

        # Register the instant the campaign is accepted — before the (possibly slow)
        # image build — so it is listed with a live phase from t=0 rather than only
        # appearing once the worker creates its directory. The single-flight guard
        # ran first, so registering our own not-yet-started entry here cannot race a
        # second launch past that guard (_is_done treats a non-terminal entry as
        # running regardless of whether its thread exists yet).
        with self._lock:
            self._campaigns[campaign_id] = entry

        # If execution.image is a symbolic ``build:<tag>`` ref the campaign needs an image
        # built before it can run. The phase is set here, synchronously, so the campaign is
        # listed as ``building`` from the instant it is accepted; the build itself is
        # *driven by the worker* (below), because this method must return a handle rather
        # than block: awaiting the build here made a 30s-read-timeout client report failure
        # for a campaign that went on to succeed, and left the caller with no id to poll.
        # The phase means the campaign is **waiting for** its image — builds are
        # content-addressed and shared, so it is not necessarily performing one.
        # A campaign running pinned images builds nothing, so it never waits for one — and its
        # specs cannot be extracted yet anyway, because the project tree it would read arrives
        # with ``target.materialize()`` on the worker below.
        if target.pinned_images is None:
            specs, _ = self._build_specs_for(target, campaign_config)
            if specs:
                state.set_phase(Phase.BUILDING,
                                stage="waiting for image(s) " + ", ".join(sorted(specs)))

        def _worker():
            """Drive the campaign, then release whatever the launch materialized.

            A wrapper rather than a ``finally`` inside :func:`_drive_campaign`, because that
            function's postprocessing tail sits *outside* its own ``try`` — an inner ``finally``
            would fire before postprocessing had run, deleting a project tree still in use.
            Here every exit reaches the release: both ``return``s, any exception, and the
            normal fall-through past the tail.
            """
            try:
                _drive_campaign()
            finally:
                if target.discard is not None:
                    try:
                        target.discard()
                    except OSError as e:
                        # Never turn a finished campaign into a failed one over scratch space;
                        # the init sweep collects whatever is left behind.
                        logger.warning("Could not discard staged project for %s: %s",
                                       campaign_id, e)

        def _drive_campaign():
            from robovast.execution.backends import CampaignStopped
            from robovast.execution.controller import end_campaign
            from robovast.execution.notify import Notifier
            backend = None
            # Built here, not left to the builder, because on this lane the worker is
            # the campaign's outermost scope (see options.finalize_phase): the builder
            # returns while postprocessing is still to come, so the one notification
            # that says "this campaign is over" has to be sent from out here.
            notifier = Notifier.from_env(campaign_id)
            try:
                # Before anything that can fail, so every later outcome — a doomed build
                # included — belongs to a campaign that can be found again.
                self._on_campaign_started(campaign_id, entry.created_at)
                # How the campaign was ASKED FOR, recorded next to it. Here rather than in the
                # request handler for the same reason as the line above: a record written later
                # would be missing from exactly the campaigns someone comes looking at.
                self._record_launch(campaign_id, results_dir, request)
                if target.materialize is not None:
                    state.set_phase(Phase.STARTING, stage="staging the project")
                    target.materialize()
                    # Cleared explicitly: set_phase leaves the stage alone when passed None, so
                    # without this "staging the project" is still the reported stage for the
                    # whole run — and, on a campaign that fails later, names the wrong step.
                    state.set_phase(Phase.STARTING, stage="")
                if target.pinned_images is not None:
                    # Not a build, but still ``_build_specs_for``: it is what installs the
                    # campaign's ``plugins:`` into the project dir, which the cluster lane's
                    # _campaign_context reads before anything else. (``_install_plugins`` in
                    # run_batch_campaign runs later, too late for that.) Cheap and pure — it
                    # never touches the build context, which is absent here by definition.
                    self._build_specs_for(target, campaign_config)
                    options.images = dict(target.pinned_images)
                else:
                    # Build (or join a sibling's build of) the experiment image and pin the
                    # concrete ref, so the backend uses it (explicit wins in
                    # resolve_robovast_image). A failed build is no longer a failed *request*:
                    # it raises into the handler below and becomes an inspectable ``failed``
                    # campaign, with the reason in its status and the output in its own log.
                    # The campaign's image project goes with it: a build's base may be a
                    # `family:` member, and which project that resolves to is per-campaign
                    # (--image-project) rather than ambient.
                    builds = self._start_build_images(
                        target, campaign_config,
                        image_project=options.image_project,
                        image_project_tag=options.image_project_tag)
                    for build in builds:
                        self._await_build_image(build.build_id, state, campaign_root)
                    if builds:
                        options.images = self._resolve_built_images(
                            target, campaign_config,
                            image_project=options.image_project,
                            image_project_tag=options.image_project_tag)
                state.set_phase(Phase.STARTING)
                with self._campaign_context(campaign_id, target):
                    backend = self._build_backend(state)
                    if is_search:
                        run_search_campaign(
                            target.config_path, campaign_config, results_dir, runs,
                            # Passed, not dropped: a search cannot honour a config
                            # filter, and silently ignoring one launched the whole
                            # budget for a caller who asked for a single-config pilot.
                            config_filter=config_filter,
                            backend=backend, options=options,
                            campaign_id=campaign_id, state=state,
                            notifier=notifier, description=request.description,
                            created_by=request.created_by)
                    else:
                        run_batch_campaign(
                            target.config_path, campaign_config, results_dir, runs,
                            config_filter=config_filter, backend=backend,
                            options=options, campaign_id=campaign_id, state=state,
                            notifier=notifier, description=request.description,
                            created_by=request.created_by)
            except CampaignStopped:
                # Clean cooperative stop (Ctrl+C / Stop): the controller already set
                # phase "stopped". Not a failure — no error, no traceback. Persist the
                # outcome so "stopped" survives a service restart.
                logger.info("Campaign %s stopped by request", campaign_id)
                self._record_campaign_stopped(campaign_id, results_dir, state, backend)
                return
            except Exception as e:  # noqa: BLE001 - surfaced via status
                # Not every failed campaign is a bug. A typo'd --config filter, a
                # missing input file, an image build pip could not resolve: the message
                # is self-contained and actionable and the stack names nothing it does
                # not, so such an error opts out of the traceback via
                # ``include_traceback``. Printing one anyway read as a RoboVAST crash
                # and sent the reader to the wrong place. The failure is still an ERROR
                # — only the noise goes. Genuine bugs keep their traceback; same test
                # the controller and the CLI apply, and ``failure_detail`` applies it to
                # the durable record.
                logger.error("Campaign %s failed: %s", campaign_id, e,
                             exc_info=getattr(e, "include_traceback", True))
                entry.error = str(e)
                state.update(error=failure_detail(e))
                state.set_phase(Phase.FAILED, stage=str(e))
                self._record_campaign_failure(
                    campaign_id, results_dir, state, e, backend)
                return
            else:
                # Analysis postprocessing (rosbags → CSV → data.db) — what the eval
                # viewer / `query_campaign_data_sql` read. The batch/search loop leaves
                # it separate, so run it here when the caller asked (the default).
                if request.postprocess and self._postprocess_in_process():
                    self._postprocess(campaign_id, results_dir, state, entry)
            finally:
                # This lane's outermost scope, so the campaign ends here — on every
                # path, including the `return`s above and a campaign that asked for no
                # postprocessing at all. Without this the run leaves the phase at
                # `finishing` and every waiter blocks until its timeout.
                end_campaign(campaign_id, state, notifier)

        thread = threading.Thread(
            target=_worker, name=f"robovast-{campaign_id}", daemon=True)
        entry.thread = thread
        thread.start()
        logger.info("Started campaign %s (search=%s)", campaign_id, is_search)
        return CampaignRef(campaign_id=campaign_id,
                           note=_show_gui_note(request, raw_config))

    # -- image builds -------------------------------------------------------

    @property
    def _images(self):
        """This lane's image store — where its built experiment images live.

        The one member a lane overrides about images, and it is a **factory, not
        behavior**: everything that consumes the store is written once, here, so a lane
        cannot answer an image question wrongly by forgetting to override the method that
        asks it. That is exactly what happened before this seam existed —
        :meth:`_exec_image` asked the local docker daemon on a lane whose images live in a
        registry, inside a pod with no docker at all, and reported every built image as
        unbuilt.
        """
        store = getattr(self, "_image_store", None)
        if store is None:
            from robovast.service.image_store import LocalDockerImageStore
            root = os.environ.get("ROBOVAST_BUILDS_ROOT")
            log_root = Path(root) if root else Path.home() / ".robovast" / "builds"
            store = LocalDockerImageStore(log_root)
            self._image_store = store
        return store

    def _build_specs_for(self, project, campaign_config, image_project=None,
                         image_project_tag=None):
        """Return ({container name: BuildSpec}, project_dir) for a project.

        A campaign may build several images — a system under test, and a scenario or
        simulation container carrying the experiment's own plugins — so this is a map.
        Empty when no container adds packages.
        """

        from robovast.common.config_plugins import ensure_workspace_plugins
        from robovast.service.image_build import extract_build_specs
        project_dir = Path(project.config_path).resolve().parent
        # Which containers build depends on the simulator backend (a stepped simulator
        # folds `simulation` into `scenario`), and the backend can live in the campaign's
        # own `plugins:` -- root-level glue is not in the service image by design. So the
        # campaign's plugins have to be resolvable BEFORE the specs are extracted, which
        # is what the compose path already does (config_generation). Without it a project
        # validated fine and then failed at start_campaign with "Unknown
        # robovast.simulators plugin", which reads as a broken .vast rather than a
        # service that had not installed what the .vast asked for.
        ensure_workspace_plugins(str(project_dir),
                                 getattr(campaign_config, 'plugins', None))
        # base_dir also lets a backend named as a `<file>.py:<Class>` ref next to the
        # .vast resolve here -- the documented escape hatch, which silently did not work
        # on this path because nothing passed the directory it resolves against.
        specs = extract_build_specs(campaign_config, base_dir=str(project_dir),
                                    image_project=image_project,
                                    image_project_tag=image_project_tag)
        if not specs:
            return {}, None
        return specs, project_dir

    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None) -> list:
        """Submit (or join) each container's image build; return their refs.

        Empty when nothing needs building. Returns as soon as each build has a
        *handle* — it does not wait; :meth:`_await_build_image` does that, on the
        campaign's own worker thread. Overridden by :class:`ClusterService` for the
        in-cluster BuildKit Job.
        """
        specs, project_dir = self._build_specs_for(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag)
        return [self._images.start(spec, project_dir)
                for spec in specs.values()]

    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None) -> dict:
        """Concrete image refs to pin once the builds are done, by container name."""
        specs, project_dir = self._build_specs_for(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag)
        return {name: self._images.ref_for(spec, project_dir).ref
                for name, spec in specs.items()}

    #: Poll cadence of :meth:`_await_build_image`. Each tick is one build-status read plus
    #: one build-log read, so it is also how often the campaign's ``build.log`` grows.
    _BUILD_POLL_SECONDS = 2.0

    def _await_build_image(self, build_id: str, state: ControllerState,
                           campaign_root: str) -> None:
        """Wait for *build_id*, teeing its log into the campaign's ``_execution/build.log``.

        One implementation for both lanes: ``get_image_build_status`` and
        ``get_image_build_log`` are interface operations each transport already provides,
        so the local Docker build and the in-cluster BuildKit Job are waited on by the same
        loop rather than by two that drift.

        The log is copied into the campaign because it is the campaign's only durable
        record of the image it ran on: the live source dies with the build (a build Job is
        reaped at ``ttlSecondsAfterFinished``), and a failed build is exactly when someone
        comes looking. The header names the build, so a build **shared** by several
        campaigns reads as shared rather than as this campaign's own work.

        A stop **detaches** — it must never cancel the build. ``build_hash`` is
        content-addressed over the spec and context, so a sibling campaign may be waiting
        on this very build, and the image is a cache entry rather than this campaign's
        property. Hence: raise, touch neither the build Job nor the local build thread.

        Raises:
            CampaignStopped: the campaign was stopped while waiting.
            ImageBuildFailed: the build failed. The message comes from
                ``classify_build_error`` and is the whole diagnosis, so the campaign
                records it without a traceback.
        """
        from robovast.execution.backends import CampaignStopped
        log_path = Path(campaign_root) / "_execution" / "build.log"
        offset = 0
        first = True
        while True:
            status = self.get_image_build_status(build_id)
            if first:
                first = False
                self._append_build_log(
                    log_path,
                    f"waiting for image {status.tag or '?'} (build {build_id})\n")
            offset = self._tee_build_log(build_id, log_path, offset)
            if status.done:
                break
            if state.stop_requested:
                raise CampaignStopped(
                    f"campaign stopped while waiting for image build {build_id}")
            time.sleep(self._BUILD_POLL_SECONDS)
        if status.phase not in ("succeeded", "cached"):
            from robovast.common.errors import ImageBuildFailed
            err = status.error
            detail = f" ({err.message})" if err and err.message else ""
            raise ImageBuildFailed(
                f"experiment image build '{status.tag or build_id}' failed{detail}; "
                f"see the BUILD section of the campaign log "
                f"(get_campaign_log with phase='build')")

    def _tee_build_log(self, build_id: str, log_path: Path, offset: int) -> int:
        """Append the build log's delta from *offset* into *log_path*; return the new
        offset. Best-effort: an unreadable build log must not fail the campaign, which
        would turn a working build into a failed run."""
        try:
            chunk = self.get_image_build_log(build_id, offset)
        except Exception as e:  # noqa: BLE001 - the build itself is what matters
            logger.debug("could not read the build log for %s: %s", build_id, e)
            return offset
        if chunk.text:
            self._append_build_log(log_path, chunk.text)
        return chunk.next_offset

    @staticmethod
    def _append_build_log(log_path: Path, text: str) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            logger.warning("Could not write %s: %s", log_path, e)

    def build_image(self, request) -> "ImageBuildRef":  # noqa: F821
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.service.image_build import primary_build_ref, validate_build_spec
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        specs, project_dir = self._build_specs_for(project, campaign_config)
        if not specs:
            raise ValueError(
                "nothing to build: no container adds system_packages or "
                "python_packages, so every image is used as declared")
        wanted = request.container
        if wanted:
            if wanted not in specs:
                raise ValueError(
                    f"container '{wanted}' builds no image; the ones that do are: "
                    + ", ".join(sorted(specs)))
            specs = {wanted: specs[wanted]}
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise ValueError(f"invalid execution.containers.{name}:\n  - "
                                 + "\n  - ".join(problems))
        refs = {name: self._images.start(spec, project_dir)
                for name, spec in specs.items()}
        return primary_build_ref(refs)

    def get_image_build_status(self, build_id: str):
        return self._images.status(build_id)

    def get_image_build_log(self, build_id: str, offset: int = 0):
        return self._images.log(build_id, offset)

    # -- container exec (diagnostic; produces no campaign) ------------------

    #: Whether a staged entrypoint is rendered for the cluster. Local runs get the
    #: local init/post-run blocks; :class:`ClusterService` flips this.
    _EXEC_CLUSTER_LANE = False

    @property
    def _exec_manager(self):
        """The single-container manager, created on first use.

        Lazily built so a service that never execs starts no reaper thread, and so a
        stray container from a previous process is reaped exactly once, when the
        capability is first used rather than on every import.
        """
        from robovast.service.container_exec import ContainerExecManager

        # The reap stays *inside* this lock so a second caller cannot start a container
        # that the reap is about to remove — but it is deliberately not the campaign lock.
        with self._exec_lock:
            if self._exec_mgr is None:
                self._exec_mgr = ContainerExecManager(self._exec_lane())
                self._reap_stray_exec_container()
            return self._exec_mgr

    def _exec_lane(self):
        from robovast.service.docker_exec_lane import DockerExecLane
        return DockerExecLane()

    def _reap_stray_exec_container(self) -> None:
        """Remove a container left behind by a previous service process.

        A fixed container name makes this a one-liner; without it an orphaned
        diagnostic container would hold memory with nothing able to name it.
        """
        from robovast.service.container_exec import CONTAINER_NAME
        try:
            done = subprocess.run(  # noqa: S603,S607
                ["docker", "rm", "-f", CONTAINER_NAME],
                check=False, capture_output=True, text=True)
            if done.returncode == 0 and done.stdout.strip():
                logger.info("removed stray exec container %s from a previous run",
                            CONTAINER_NAME)
        except OSError as e:
            logger.debug("could not check for a stray %s: %s", CONTAINER_NAME, e)

    def _exec_vast_file(self, request) -> str:
        """The ``.vast`` this request runs, from whichever source it named.

        A campaign's ``_config/`` *is* a project, so the two sources differ only here.
        """
        from robovast.service.container_exec import vast_in_dir
        if request.campaign_id:
            config_dir = self._campaign_dir(request.campaign_id) / "_config"
            if not config_dir.is_dir():
                raise ValueError(
                    f"campaign {request.campaign_id} has no _config/ to run — it is not "
                    "a campaign directory, or was created before its config was staged")
            return vast_in_dir(str(config_dir), request.config_path)
        return self._resolve_project(request.workspace_id, request.config_path).config_path

    def exec_in_container(self, request) -> "ExecResult":  # noqa: F821
        from robovast.common.execution import is_build_image_ref
        from robovast.service.container_exec import result_from, stage, validate
        validate(request)
        # Before staging: a refused request should not have created a temp tree.
        self._admit_show_gui(request)
        vast_file = self._exec_vast_file(request)
        spec, _campaign_data, limit_s, limit_source = stage(
            vast_file, request.config_name, cluster=self._EXEC_CLUSTER_LANE,
            command=request.command, gui=bool(request.show_gui))
        # Ownership of spec's staging tree passes to the manager: a held container mounts
        # it as /config, so it must outlive this call. On the way *in*, though, a failure
        # before that handover is ours to clean up.
        try:
            found = self._resolve_exec_image(vast_file, request.container or None,
                                             campaign_id=request.campaign_id or "")
            spec.image = found.ref
            # What the caller is told the container is. Never `found.ref`: on the cluster
            # lane that is registry-qualified, and this value is reported back.
            spec.image_identity = found.identity
            if is_build_image_ref(spec.image):
                # Defensive: _exec_image resolves build: refs, and handing docker a
                # symbolic one would fail with a confusing pull error instead.
                raise ValueError(f"unresolved image ref {spec.image!r}")
            if request.workspace_id:
                spec.workspace_id = self.store.registry.require(
                    request.workspace_id)["workspace_id"]
                spec.workspace_dir = str(
                    self.store.registry.project_dir(spec.workspace_id))
        except Exception:
            spec.close()
            raise
        # show_gui belongs in the identity, not just in the env: the X11 mount can only be
        # established when the container is created, and a follow-up call only `docker
        # exec`s into it. Without this, asking for a window after a plain call would reuse
        # the mount-less container and silently draw nothing.
        identity = (request.workspace_id, request.campaign_id,
                    request.config_path, request.config_name, spec.image,
                    bool(request.show_gui))
        started = time.monotonic()
        out = self._exec_manager.run(spec, limit_s,
                                    keep_alive=request.keep_alive,
                                    identity=identity)
        return result_from(out, spec=spec, limit_s=limit_s,
                           limit_source=limit_source,
                           duration_s=time.monotonic() - started,
                           container=self._exec_manager.state())

    def _exec_image(self, vast_file: str, container: "str | None" = None,
                    campaign_id: str = "") -> str:
        """The concrete image to exec in, resolved exactly as a run would resolve it.

        *container* is a role or container name (``scenario`` / ``simulation`` / ``sut``
        or an ad-hoc one); the default is the container the scenario runs in, which for
        a campaign with no simulator is the only one — so an unqualified call answers
        the same question it always did.

        A built image must already exist on this lane's store: building implicitly would
        turn a seconds-long check into a multi-minute one the caller never asked for.
        """
        return self._resolve_exec_image(vast_file, container, campaign_id).ref

    def _resolve_exec_image(self, vast_file: str, container: "str | None" = None,
                            campaign_id: str = "") -> "ImageRef":  # noqa: F821
        """The exec image as an :class:`~robovast.service.image_store.ImageRef`.

        Split from :meth:`_exec_image` because two callers want different halves of one
        resolution: a container is started from ``.ref``, while :meth:`resolve_image` hands
        ``.identity`` to a client and must not leak the concrete form. Resolving twice to
        get the two would be two chances to disagree.

        The branch is on the **config source**, not on the lane:

        * a *campaign* has already run, and recorded which image each role ran on, so the
          diagnostic runs those exact bytes — see :func:`campaign_role_image`. Re-deriving
          a content hash from the campaign's frozen ``_config/`` cannot work anyway: that
          snapshot holds the ``.vast``, the scenario and the run files, not the build
          inputs, so every source dir and workspace wheel hashes as a bare requirement and
          the hash differs from the one the build produced.
        * a *workspace* project is asked of the image store, which is the lane's own
          answer to "what is this called here, and is it here".
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.common.containers import plan_containers
        from robovast.common.execution import resolve_robovast_image
        from robovast.service.image_store import ImageRef

        # Validate rather than reading the raw mapping: the build specs come off the
        # *model*, so handing this path a plain dict yields "no build section" for every
        # project that has one.
        campaign_config = validate_config(load_config(vast_file))
        plan = plan_containers(campaign_config.execution.model_dump())
        target = plan.by_name(container) if container else plan.main

        if not target.builds:
            declared = resolve_robovast_image(
                config_image=target.image, fallback=target.is_main)
            # A declared image is already the client-facing name of itself: it carries no
            # build of ours and no registry we chose.
            return ImageRef(ref=declared, identity=declared, build_id="")

        if campaign_id:
            return self._campaign_exec_image(campaign_id, target.name)

        specs, project_dir = self._build_specs_for(
            WorkspaceTarget(config_path=vast_file), campaign_config)
        found = self._images.ref_for(specs[target.name], project_dir)
        if not self._images.present(found):
            self._refuse_unbuilt(target.name, found.build_id)
        return found

    def _campaign_exec_image(self, campaign_id: str, role: str) -> "ImageRef":  # noqa: F821
        """The image *campaign_id* actually ran *role* on.

        Digest-first and role-aware through :func:`campaign_role_image`, which already
        answers this for the scene cache and refuses to substitute the campaign-level image
        for a role that owns a container. A digest is its own identity — it names bytes and
        no registry we picked — so both fields carry it.

        :meth:`_resolve_image_digest` is the existing per-lane hook for the tag-only
        campaigns that predate per-role digests: docker locally, a deliberate refusal on the
        cluster (guessing there would name bytes no node can pull).
        """
        from robovast.common.campaign_data import campaign_role_image
        from robovast.service.image_store import ImageRef
        image = campaign_role_image(Path(self._role_image_source_dir(campaign_id)), role,
                                    resolve_digest=self._resolve_image_digest)
        return ImageRef(ref=image, identity=image, build_id="")

    def _role_image_source_dir(self, campaign_id: str) -> str:
        """Where this campaign's recorded per-role images are read from.

        Its own seam for the reason :meth:`_data_dir` refuses to be one: locally the campaign
        is a directory, on the cluster it is an object-store prefix, and a caller has to say
        which *objects* it needs rather than getting "the whole campaign" and quietly turning
        a lookup into a rosbag download.

        Two are needed, and both matter: ``_execution/execution.yaml`` holds the per-role
        digests, and the frozen ``.vast`` under ``_config/`` is what tells
        :func:`campaign_role_image` whether the role owns a container of its own. Without the
        second it cannot refuse the campaign-level substitution — so exec'ing into ``sut``
        could silently land in the scenario's image.
        """
        return str(self._campaign_dir(campaign_id))

    def _refuse_unbuilt(self, container_name: str, build_id: str) -> "NoReturn":  # noqa: F821
        """Refuse an exec whose image is not on the store, saying which state it is in.

        Shared by every lane and never overridden: ``get_image_build_status`` is an
        interface operation both of them implement (the cluster's even recovers an
        untracked build from its Job), so the classification has one implementation rather
        than two that drift — the same argument ``_await_build_image`` already makes for
        the build wait loop.
        """
        from robovast.common.errors import ImageNotBuilt
        from robovast.service.image_build import not_built_message
        status = None
        if build_id:
            try:
                status = self.get_image_build_status(build_id)
            except KeyError:
                status = None       # nothing was ever started for these inputs
            except Exception as e:  # noqa: BLE001
                # The probe must never replace the refusal it decorates: on the cluster it
                # can touch the API server, and a failure there is not an answer about the
                # image. Degrade to the plainest wording rather than raising something the
                # caller cannot act on.
                logger.debug("could not read build state for %s: %s", build_id, e)
                status = None
        message, next_step = not_built_message(container_name, build_id, status)
        raise ImageNotBuilt(message, next_step=next_step)

    def stop_exec_container(self) -> "ExecStopResult":  # noqa: F821
        # The `del backend` that used to be here outlived the parameter it deleted: the
        # per-request lane selector went when a service became single-lane, and this line
        # made every call raise NameError. Nothing caught it because every test of this
        # verb uses a fake transport, so the real method was never called.
        return self._exec_manager.stop()

    def resolve_image(self, request) -> "ImageResolution":  # noqa: F821
        """Same resolution :meth:`exec_in_container` runs internally, without the run.

        Reuses :meth:`_resolve_exec_image` — the same project load, ``plan_containers`` and
        image-store lookup the exec itself does — so a resolved image never drifts from what
        a real exec would use. No container starts either way.

        Hands back the ``identity``, never the concrete ref: this value crosses the API
        boundary (it keys the per-image catalog cache and is reported to the caller), and on
        the cluster lane the concrete form is registry-qualified.
        """
        from robovast.service.interface import ImageResolution
        vast_file = self._exec_vast_file(request)
        found = self._resolve_exec_image(vast_file, request.container or None,
                                         campaign_id=request.campaign_id or "")
        return ImageResolution(image=found.identity)

    def _postprocess(self, campaign_id, results_dir, state, entry):
        """Run analysis postprocessing for a just-finished local campaign.

        Advances the phase ``... → postprocessing → finished`` and generates the
        campaign's ``data.db``; a failure surfaces via status (phase ``failed``).
        """
        from robovast.client.logging_config import (add_campaign_log_handler,
                                                    remove_campaign_log_handler)
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

    def _record_launch(self, campaign_id, results_dir, request):
        """Persist how this campaign was asked for, to ``_execution/launch.yaml``.

        The counterpart of :meth:`_record_outcome` at the other end of the campaign: what was
        requested, rather than how it ended. ``config_filter`` in particular is recorded
        **nowhere else** — it is consumed inside ``build_campaign_data`` and then gone — so
        without this "was this the full sweep or a one-config pilot?" cannot be answered about
        any campaign in the results root, by a retrigger or by a human.

        Called at the top of the worker so it lands before anything that can fail, for the
        same reason :meth:`_on_campaign_started` is there. Never fatal: a campaign that runs
        correctly must not be failed by an unwritable record.
        """
        from robovast.common.campaign_data import write_launch_record
        try:
            write_launch_record(Path(results_dir) / campaign_id, request)
        except OSError as e:
            logger.warning("Could not write launch.yaml for %s: %s", campaign_id, e)

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
            return self._derive_postprocessed(campaign_id, entry.state.snapshot())
        # Not tracked in this process — reconstruct from disk (past campaign).
        return self._status_from_disk(campaign_id)

    def _derive_postprocessed(self, campaign_id: str, snap: Status) -> Status:
        """Apply the recovery path's ``postprocessed`` rule to a **live** snapshot.

        ``reconstruct_status_from_disk`` states it: *postprocessed is a fact about the
        campaign, not about who last drove it*, and derives it from the built
        ``_execution/data.db``. The live ``ControllerState`` answers a narrower question —
        ``_postprocess`` records ``True`` only when the ``.vast`` declared postprocessing
        **entries**, which is what decides whether the stored archive is the postprocessed
        one. Both are wanted, but only the first is what a reader means by "is there data
        here", so the two have to agree on that.

        They did not, and it was visible: a campaign whose ``.vast`` declares no
        ``results_processing.postprocessing`` still builds ``data.db``, yet reported
        ``postprocessed=False`` for as long as this process still tracked it — hiding the
        web UI's Results and Run views, which read exactly that file — and then started
        reporting ``True`` once a restart dropped the entry and the disk path answered
        instead. Same campaign, same bytes, two answers depending on service uptime.

        Only ever promotes ``False`` → ``True``, and only on the evidence the recovery path
        uses, so the two cannot disagree; what ``_postprocess`` records is untouched, and so
        is the archive decision that reads it. Best-effort on the cluster lane in exactly the
        way the recovery path already is: ``data.db`` is not among ``_RECORD_OBJECTS``, so a
        campaign whose derived data was never fetched here answers the same as before.
        """
        if snap.postprocessed:
            return snap
        try:
            if (Path(self._record_dir(campaign_id)) / "_execution" / "data.db").is_file():
                snap.postprocessed = True
        except OSError:
            pass          # a status read must not fail over an unreachable record dir
        return snap

    def get_campaign_logs(self, campaign_id: str, offset: int = 0):
        """Serve the campaign's unified infrastructure log from the campaigns root.

        Assembles the per-phase files (variation → run → postprocessing) under the
        campaign's ``_execution/`` into one divider-separated stream (see
        :func:`robovast.common.campaign_logs.assemble_log`). Local runs write those
        files in place and they grow there, so the same read serves a live and a
        finished campaign; ``eof`` is set once the campaign is no longer driven here.
        """
        from robovast.common.campaign_logs import assemble_log_from_dir
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
        ``completed``/``failed`` by its ``test.xml`` result, ``killed`` when an operator
        stopped its job, or ``running`` when the campaign is still live and the run has
        not produced one yet (local is sequential, so at most one). Pending
        (not-yet-started) runs have no directory, so they are counted from the
        controller's expected total but not listed.

        The kill has to be consulted because "running" here means *no ``test.xml`` yet* —
        and a killed run's ``test.xml`` is precisely the thing that never arrives. Without
        it the job stayed ``running`` for the rest of the campaign's life, keeping a row in
        the live Jobs list and a Stop button on a job that was already dead.
        """
        from robovast.common.campaign_data import (killed_failure_message, killed_runs,
                                                   read_test_result)
        campaign_dir = self._campaigns_root() / campaign_id
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        live = entry is not None and not self._is_done(entry)
        # One read for the whole listing, and `{}` for every campaign nobody intervened in.
        killed = killed_runs(campaign_dir)

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
                    job_name = f"{config_dir.name}/{run_dir.name}"
                    detail = None
                    try:
                        status = "completed" if read_test_result(run_dir)["success"] \
                            else "failed"
                    except FileNotFoundError:
                        # Same precedence as ``read_run_outcome``: a kill only explains a
                        # run that delivered nothing. One that wrote a ``test.xml`` before
                        # the kill landed keeps the verdict it earned, above.
                        entry_killed = killed.get(job_name)
                        if entry_killed is not None:
                            status = "killed"
                            detail = killed_failure_message(entry_killed)
                        else:
                            status = "running" if live else "failed"
                    jobs.append(JobSummary(
                        job_name=job_name,
                        status=status,
                        detail=detail,
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
            killed=sum(1 for j in jobs if j.status == "killed"),
            total=len(jobs) + pending)
        return ListJobsResponse(jobs=jobs, counts=counts)

    def _new_job_log_tail(self, campaign_id: str, job_name: str):
        """Build this lane's tail for a job. Overridden by the cluster lane."""
        from robovast.service.local_job_log import LocalJobLogTail
        return LocalJobLogTail()

    def _job_log_tail(self, campaign_id: str, job_name: str):
        """The cached log tail for a job, created on first use (LRU-bounded)."""
        key = (campaign_id, job_name)
        with self._job_log_guard:
            tail = self._job_log_tails.get(key)
            if tail is None:
                tail = self._new_job_log_tail(campaign_id, job_name)
                self._job_log_tails[key] = tail
                while len(self._job_log_tails) > self._JOB_LOG_CACHE_MAX:
                    self._job_log_tails.popitem(last=False)
            else:
                self._job_log_tails.move_to_end(key)
            return tail

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a run's live container logs, merged (``job_name`` = ``<config>/<run>``).

        The containers write these files in place as they execute, so the same read
        serves a running and a finished run.

        **All** of the job's containers, not just the main one: the ROS shape runs the
        simulator and the system under test in their own containers, which write
        ``logs/system_<name>.log`` beside the main container's ``logs/system.log``. Only
        the latter used to be read, so the panel showed scenario-execution and neither
        the simulator nor nav2 -- the two whose output explains a failed run. See
        :class:`~robovast.service.local_job_log.LocalJobLogTail` for how concurrent files
        are merged without breaking the byte-offset contract.

        The containers write to the JOB's artifact dir (``_jobs[/<batch>]/job-<j>``),
        not to the config/run dir -- ``<config>/<run>/logs/`` exists but stays empty, so
        reading there returns a silently blank job log even though the same output is
        visible in the campaign log (the local backend also folds container stdout into
        ``controller.log``). :func:`job_artifact_dir` resolves the real dir.

        ``eof`` needs the run finished **and** a settled poll. A sidecar flushes during
        compose's stop grace, i.e. after the main container wrote ``test.xml``, so ending
        the stream on ``test.xml`` alone would close the panel on exactly the shutdown
        output that says whether the simulator saved its recording.
        """
        from robovast.client.safe_path import UnsafePathError
        from robovast.common.campaign_data import read_test_result
        from robovast.common.execution import job_artifact_dir
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
        finished = run_done or not live
        tail = self._job_log_tail(campaign_id, job_name)
        with tail.lock:
            grew = tail.read(job_dir / "logs", flush_partial=finished)
            text, next_offset = tail.merged.slice_from(offset)
        return LogChunk(text=text, next_offset=next_offset,
                        eof=(not live) or (finished and not grew))

    def stop(self, campaign_id: str) -> ActionResult:
        """Request a cooperative stop and kill the compute so the worker unblocks.

        A campaign still in ``building`` is stopped by the flag alone: the teardown below
        removes the *scenario* container and cannot reach a ``docker buildx`` build thread.
        That is deliberate and must stay true — an image build is content-addressed and
        therefore shared, so cancelling it could strand a sibling campaign waiting on the
        same image, and the image is a cache entry rather than this campaign's property.
        ``_await_build_image`` detaches instead (see its ``CampaignStopped`` path).
        """
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(ok=False, message=f"campaign {campaign_id} not tracked here")
        entry.state.request_stop()
        self._kill_scenario_container()
        return ActionResult(ok=True, message="stop requested")

    def stop_job(self, campaign_id: str, job_name: str,
                 reason: "str | None" = None, source: str = "api") -> ActionResult:
        """Kill the running job's scenario container; the run loop moves to the next run.

        Deliberately **not** ``request_stop()``: that flag is what ends the campaign, and
        leaving it clear is the whole difference between this and :meth:`stop`. The
        generated ``run.sh`` runs one ``docker compose up``/``down`` cycle per job, so
        removing the container makes only *that* cycle exit non-zero and the loop proceeds
        (see :class:`~robovast.execution.backends.DockerBackend`).

        The named job must be the one actually in flight. This lane is single-flight
        behind a fixed container name, so the kill lands on whichever job is current
        regardless of what was asked for — accepting a stale name would report success for
        killing a *different* run than the caller named, which is the one outcome worse
        than refusing.
        """
        from robovast.common.campaign_data import record_killed_job
        from robovast.common.execution import job_artifact_dir
        campaign_dir = self._campaigns_root() / campaign_id
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            raise KeyError(f"campaign {campaign_id!r} not tracked here")
        job = self._require_running_job(campaign_id, job_name)
        # Recorded before the kill, not after: the container dies asynchronously and a
        # crash in between would leave a dead run with no explanation for why it stopped.
        try:
            job_dir = os.path.relpath(job_artifact_dir(campaign_dir, job_name),
                                      campaign_dir)
        except (FileNotFoundError, OSError, ValueError):
            # No manifest entry yet (the documented startup race). The run key below is
            # this lane's own job identity, so resolution does not depend on it.
            job_dir = ""
        record_killed_job(campaign_dir, job_dir=job_dir, job_name=job_name,
                          source=source, reason=reason, runs=(job_name,))
        self._kill_scenario_container()
        return ActionResult(
            ok=True,
            message=(f"killed job {job.display_name or job_name}; the campaign continues "
                     f"with its remaining runs and this run is recorded as 'killed'"))

    def _require_running_job(self, campaign_id: str, job_name: str):
        """The named job, or raise — shared by both lanes' :meth:`stop_job` preconditions.

        Resolved through :meth:`list_jobs` rather than a lane-specific probe so the
        precondition is checked against the very status the caller was shown. ``KeyError``
        for a job that does not exist, ``RuntimeError`` naming the phase for one that
        exists but is not running: only a job that is *underway* has something to kill.
        """
        jobs = self.list_jobs(campaign_id).jobs
        job = next((j for j in jobs if j.job_name == job_name), None)
        if job is None:
            known = ", ".join(j.job_name for j in jobs) or "none"
            raise KeyError(f"job {job_name!r} not found in campaign {campaign_id!r} "
                           f"(jobs: {known})")
        if job.status != "running":
            running = [j.job_name for j in jobs if j.status == "running"]
            hint = f"; running now: {', '.join(running)}" if running else ""
            raise RuntimeError(
                f"job {job_name!r} is {job.status}, not running — only a running job can "
                f"be stopped{hint}")
        return job

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

    def _extra_live_ids(self) -> set[str]:
        """Campaigns this service drives that are not in ``self._campaigns``.

        Empty for a single-lane service, where the in-memory registry is the whole
        picture. A multi-lane service overrides it with its sibling lanes' registries:
        without that, :meth:`list_campaigns` sees only *this* lane's live entries, and
        a campaign driven by another lane is unlistable until its results directory
        exists on disk — which is exactly the window in which a caller whose start
        call timed out goes looking for it, finds nothing, and retries into a
        duplicate.
        """
        return set()

    def _durable_campaign_ids(self) -> set[str]:
        """Campaigns whose durable home is **not** this disk.

        Empty here: a local campaign's home *is* the results directory the scan below
        reads, so that scan already sees every one of them. :class:`ClusterService`
        overrides it with the object store's campaign index — without which a finished
        cluster campaign is unlistable as soon as its local scratch is gone, which in-pod
        is every campaign from a previous service life.

        Distinct from :meth:`_extra_live_ids` deliberately: that one is about campaigns
        being *driven* elsewhere right now, this one about campaigns *stored* elsewhere. A
        campaign can be in either, both, or neither.
        """
        return set()

    def _on_campaign_started(self, campaign_id: str, created_at: str) -> None:
        """The campaign's driver is starting: record it wherever it must be discoverable.

        No-op here — see :meth:`_durable_campaign_ids`. :class:`ClusterService` publishes
        the campaign's index marker. This is a hook at the very top of the worker rather
        than a call further down because it has to happen **before anything that can
        fail**: the marker is what makes a campaign findable, so one written after the
        build, the run, or the finalize upload would be missing from precisely the
        campaigns worth looking at.
        """

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        request = request or ListCampaignsRequest()
        results_dir = self._campaigns_root()
        from robovast.common.execution import is_campaign_dir

        # Which campaigns exist = those persisted on disk ∪ those stored in a durable home
        # that is not this disk ∪ those being driven now (registered in-memory, perhaps
        # without a directory yet — a just-launched one is still building/starting). Not
        # three sources of truth: each id is resolved to a summary by the same precedence
        # get_status uses (live snapshot if tracked, else reconstruct from its records).
        disk = {d.name for d in results_dir.iterdir()
                if d.is_dir() and is_campaign_dir(d.name)} if results_dir.is_dir() else set()
        disk |= self._durable_campaign_ids()
        with self._lock:
            mem = set(self._campaigns)
        mem |= self._extra_live_ids()
        # Newest first by recorded start time. Never sort on the id: it is
        # `<name>-<timestamp>` with a user-supplied name (see `campaign_id_for`), so id
        # order is alphabetical by name and only chronological within one name. That
        # matters beyond display, because offset/limit slice *this* order — a
        # name-ordered window would hide the newest campaigns from the caller entirely.
        # A campaign whose start time is unknown (no readable store, no execution
        # record) sorts last; the id only breaks ties, so the order is deterministic
        # even though the input is a set.
        started = {cid: self._started_at_for(cid) for cid in disk | mem}
        all_ids = sorted(started,
                         key=lambda c: (started[c] is not None, started[c] or "", c),
                         reverse=True)
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
            # The store's recorded start time, not now: re-running postprocessing or
            # sharing must not restamp (and so re-order) a finished campaign. Read
            # directly rather than via _started_at_for — we already hold self._lock,
            # which that helper takes.
            entry.created_at = (read_campaign_created_at(self._campaign_dir(campaign_id))
                                or entry.created_at)
            # Likewise the description: a tracked entry answers for the campaign while
            # it is live, so leaving this empty would blank the description out of every
            # listing for the duration of a re-triggered postprocess/share.
            entry.description = (read_campaign_description(self._campaign_dir(campaign_id))
                                 or "")
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
            from robovast.client.logging_config import (add_campaign_log_handler,
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
                # `output_callback` is what puts the step-by-step narrative ("[2/4]
                # Executing: …", "✓ …") into the handler opened above. Without it those lines
                # default to `print` and land on the service's stdout, so a *retriggered*
                # postprocessing wrote a phase file holding only what modules logged
                # themselves -- the campaign log looked empty for the run you just asked for.
                # Same callback the in-campaign path passes (`_postprocess`).
                ok, message = run_postprocessing(
                    results_dir=str(campaign_dir.parent), campaign=request.campaign_id,
                    force=request.force, skip=list(request.skip or []),
                    output_callback=logger.info)
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
            from robovast.client.logging_config import (add_campaign_log_handler,
                                                        remove_campaign_log_handler)
            from robovast.execution.backends import RunOptions
            from robovast.execution.status_recovery import record_step_outcome

            # Its own phase file, so the campaign log shows what an upload did under a SHARE
            # divider. Previously this wrote nowhere the campaign log reads: a share that failed
            # left a one-line `share_error` and no account of how it got there, which is the
            # least inspectable moment of a campaign -- it moves gigabytes to somebody else's
            # storage. Folding it into postprocessing.log instead would make that divider name
            # a step it did not come from.
            handler = None
            try:
                handler = add_campaign_log_handler(
                    str(campaign_dir / "_execution" / "share.log"))
            except Exception:  # pylint: disable=broad-except
                logger.warning("Could not open share.log for %s", request.campaign_id,
                               exc_info=True)
            backend = self._build_backend(ControllerState())
            options = RunOptions(gui=False, upload_to_share=True)
            try:
                logger.info("upload-to-share: %s", campaign_dir.name)
                backend.preflight_upload_to_share()
                backend.share_campaign(str(campaign_dir), options)
                ok, message = True, "upload-to-share complete"
                logger.info("✓ %s", message)
            except Exception as e:  # noqa: BLE001 - surfaced via status + share_error
                ok, message = False, failure_detail(e)
                logger.error("✗ upload-to-share failed: %s", message)
            finally:
                remove_campaign_log_handler(handler)
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
        from robovast.common.common import load_config
        from robovast.common.config_generation import generate_scenario_variations
        project = self._resolve_project(workspace_id, path)
        # A search .vast has no `configuration:` to expand -- its variations live under
        # `search.variations` and are only realized per sampled ParamSet. Composing a
        # sample the way a real batch does is the only preview that means anything;
        # the plain call would report zero configs, indistinguishable from an empty file.
        if (load_config(project.config_path) or {}).get("search"):
            from robovast.search.compose import preview_search_sample
            try:
                sample = preview_search_sample(project.config_path)
            except Exception as e:  # noqa: BLE001 - surface resolution errors as 400
                raise ValueError(str(e)) from e
            configs = sample["configs"]
            runs = sample["runs_per_config"]
        else:
            try:
                campaign_data = generate_scenario_variations(
                    variation_file=project.config_path, output_dir=None)
            except Exception as e:  # noqa: BLE001 - surface resolution errors as 400
                raise ValueError(str(e)) from e
            configs = campaign_data["configs"]
            runs = campaign_data.get("execution", {}).get("runs", 1)
        from robovast.common.common import convert_dataclasses_to_dict
        remotes = _variation_remotes()
        vast_dir = str(Path(project.config_path).parent)
        # Truncate BEFORE building the payload: the contribution of a configuration nobody
        # will see still costs every variation's hook, and a large sweep is exactly where
        # max_configs is passed.
        shown = configs[:max_configs] if max_configs else configs
        items = [PreviewConfiguration(
                    name=c["name"],
                    parameters=convert_dataclasses_to_dict(c.get("config", {})),
                    sim=convert_dataclasses_to_dict(c.get("sim", {})),
                    internals=convert_dataclasses_to_dict(
                        {k: v for k, v in c.items()
                         if k.startswith("_") and k != "_config_block"}),
                    contribution=_config_view_contribution(c, vast_dir),
                    previews=_config_previews(c, remotes))
                 for c in shown]
        truncated = bool(max_configs) and len(configs) > max_configs
        return PreviewResponse(configs=len(configs), runs_per_config=runs,
                               total_trials=len(configs) * runs,
                               configurations=items, truncated=truncated,
                               # The config view is declared in the same file this expanded, and
                               # is wanted at exactly the same moment, so it rides along rather
                               # than costing a second round trip.
                               config_panels=_config_panel_specs(
                                   load_config(project.config_path) or {},
                                   _panel_remotes("config"), workspace_id))

    def describe_world(self, workspace_id: str, path: str = "", targets: str = "",
                       entities: bool = False, backend: str = "") -> WorldDescription:
        del backend  # one lane here; a multi-backend service overrides this to select
        import yaml

        from robovast.common.config_generation import WorldQueryUnavailable, describe_world_payload
        from robovast.common.simulators import backend_name, campaign_sim_block
        project = self._resolve_project(workspace_id, path)
        with open(project.config_path, encoding="utf-8") as handle:
            parameters = yaml.safe_load(handle) or {}
        execution = parameters.get("execution", {}) or {}
        # The campaign DEFAULT block. A campaign that varies its world per configuration has
        # several; the answer names the world it described, so a caller can see which.
        block = campaign_sim_block(execution)
        started = time.monotonic()
        try:
            payload, image = describe_world_payload(
                execution, block, str(Path(project.config_path).parent),
                entities=entities, targets=targets)
        except WorldQueryUnavailable as exc:
            raise ValueError(str(exc)) from None
        return WorldDescription(
            backend=backend_name(execution) or "",
            image=image,
            world=str(payload.get("world") or ""),
            packaged=bool(payload.get("packaged")),
            inputs=[str(p) for p in (payload.get("inputs") or [])],
            plugins=list(payload.get("plugins") or []),
            entities=payload.get("entities"),
            overridable=dict(payload.get("overridable") or {}),
            # Both carry how the answer was arrived at, so dropping them here would hand a caller
            # a null `entities` with nothing to distinguish "compiles none" from "could not ask".
            dropped_transport=[str(p) for p in (payload.get("dropped_transport") or [])],
            errors=dict(payload.get("errors") or {}),
            duration_s=round(time.monotonic() - started, 3),
        )

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
        result = describe_data_db(self._query_dir(campaign_id))
        return DataDescribe(campaign_id=campaign_id, **result)

    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        extra_campaign_ids=None, max_bytes: int | None = None,
    ) -> "DataQueryResult":
        from robovast.results_processing.data_query import query_data_db
        from robovast.service.interface import DataQueryResult
        extra_dirs = {f"c{i + 1}": self._query_dir(cid)
                      for i, cid in enumerate(extra_campaign_ids or [])}
        result = query_data_db(self._query_dir(campaign_id), sql, max_rows,
                               extra_dirs=extra_dirs, max_bytes=max_bytes)
        return DataQueryResult(campaign_id=campaign_id, **result)

    def stream_campaign_query_csv(self, campaign_id: str, sql: str,
                                  extra_campaign_ids=None):
        # Resolved through _query_dir like the JSON path, so the cluster service's
        # object-store fetch happens here too rather than needing its own override.
        from robovast.results_processing.data_query import stream_query_csv
        extra_dirs = {f"c{i + 1}": self._query_dir(cid)
                      for i, cid in enumerate(extra_campaign_ids or [])}
        return stream_query_csv(self._query_dir(campaign_id), sql, extra_dirs=extra_dirs)

    def campaign_data_status(self, campaign_id: str) -> "CampaignDataStatus":
        """Local: a query never transfers anything, so there is nothing to warn about.

        The databases are files on this service's own disk. ``ClusterService`` overrides
        this with the object-store answer."""
        from robovast.service.interface import CampaignDataStatus
        return CampaignDataStatus(
            campaign_id=campaign_id, source="local-disk", fetch_required=False,
            cached=True, transfer="none",
            note="the campaign's databases are on the service's local disk; queries read "
                 "them in place")

    def _data_dir(self, campaign_id: str):
        """Campaign dir holding data.db/campaign.db — **local lane only**.

        Locally this is a directory on disk and everything below can share it. On the
        cluster there is no such thing: the campaign lives in the object store, and any
        answer here would have to materialise it. ``ClusterService`` therefore **refuses**
        this call and each caller states what it needs instead — :meth:`_query_dir` (two
        databases), :meth:`_config_dir` (the frozen ``.vast``), or
        :meth:`_whole_campaign_dir` (everything, said out loud).

        That refusal is the point. While this method silently answered "the whole
        campaign", every inherited method that touched it became a whole-campaign
        download — ``list_campaign_plots`` pulled every rosbag to read one YAML file, per
        campaign, on every Results page load.
        """
        return self._campaign_dir(campaign_id)

    def _whole_campaign_dir(self, campaign_id: str):
        """Campaign dir for a caller that genuinely needs **arbitrary** files from it.

        The honest, explicit form of what ``_data_dir`` used to do implicitly: notebook
        rendering against run outputs, and the ``/results`` file address space. On the
        cluster this is a full ``fetch_campaign``, which is expensive and now says so at
        the call site rather than hiding behind a resolver name.
        """
        return self._data_dir(campaign_id)

    def _config_dir(self, campaign_id: str):
        """Dir holding the campaign's frozen ``_config`` snapshot.

        Separate seam because it is what the *cheap* readers actually want — declared
        plots, panel assets, visualization workloads — and on the cluster it is a handful
        of small objects rather than the campaign.
        """
        return Path(self._data_dir(campaign_id)) / "_config"

    def _query_dir(self, campaign_id: str):
        """Dir a **query** reads: it needs only ``_execution/data.db`` + ``campaign.db``
        (see ``data_query._open_db``).

        Locally identical to :meth:`_data_dir`. Separate from it because on the cluster a
        query needs two objects and the campaign may be terabytes, so ``ClusterService``
        overrides this one alone. Callers needing more say which more: the frozen config
        via :meth:`_config_dir`, or everything via :meth:`_whole_campaign_dir`."""
        return self._data_dir(campaign_id)

    def resolve_data_dir(self, campaign_id: str):
        """Public seam: a campaign's whole data dir, for the endpoint-plugin dispatch
        (see ``endpoint_plugin``), which cannot know which files a plugin will read.

        The one caller entitled to the whole campaign without naming its files — and on
        the cluster that is a full fetch, so it goes through
        :meth:`_whole_campaign_dir` rather than the refused ``_data_dir``."""
        return self._whole_campaign_dir(campaign_id)

    def list_campaign_plots(self, campaign_id: str) -> "CampaignPlotsResponse":
        # Raw-load (not full validation) — reading declared plots must not depend on
        # the rest of the snapshot config being re-validatable.
        from robovast.common.config import visualization_block
        from robovast.common.config_validation import _safe_load
        from robovast.service.interface import CampaignPlotsResponse
        config_dir = Path(self._config_dir(campaign_id))
        vasts = sorted(config_dir.glob("*.vast")) if config_dir.is_dir() else []
        plots = []
        if vasts:
            cfg, _ = _safe_load(str(vasts[0]))
            for p in (visualization_block(cfg, "results", "data_browser", "plots") or []):
                if isinstance(p, dict) and p.get("query"):
                    plots.append({"title": p.get("title", ""), "query": p["query"],
                                  "vega_lite": p.get("vega_lite") or {}})
        return CampaignPlotsResponse(campaign_id=campaign_id, plots=plots)

    def list_campaign_panels(self, campaign_id: str) -> "CampaignPanelsResponse":
        # Raw-load (not full validation) — reading declared panels must not depend on
        # the rest of the snapshot config being re-validatable. Reads the *effective*
        # .vast so in-place run-view visualization edits are reflected.
        from robovast.common.config import CUSTOM_PANEL_TYPE, visualization_block
        from robovast.common.config_validation import _safe_load
        from robovast.common.simulators import merge_default_panels
        from robovast.service.interface import CampaignPanelsResponse
        from robovast.service.postprocessing_edit import campaign_vast
        cfg, _ = _safe_load(str(campaign_vast(Path(self._campaign_dir(campaign_id)))))
        run_view = visualization_block(cfg, "results", "run_view") or {}
        # The simulator backend contributes the panels that replay what it always records
        # (roqsim's `scene3d`), so a campaign never declares one it could not do without.
        # Merged here rather than in the UI, so validation and the view agree.
        raw = merge_default_panels(run_view.get("panels") or [], (cfg or {}).get("execution") or {})
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
            campaign_id=campaign_id, panels=panels, timeline=run_view.get("timeline"))

    def resolve_campaign_panel_asset(self, campaign_id: str, rel_path: str) -> str:
        """Resolve a ``custom`` panel's staged bundle file, confined to the campaign's
        immutable ``_config/`` snapshot. Raises ``ValueError`` (→ 400) on a path escape,
        ``KeyError`` (→ 404) if the file is missing."""
        base = Path(self._config_dir(campaign_id)).resolve()
        target = (base / rel_path).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise ValueError("path escapes the campaign config directory")
        if not target.is_file():
            raise KeyError(f"panel asset not found: {rel_path}")
        return str(target)

    # -- on-demand 3D geometry ---------------------------------------------

    def _scene_capture(self, campaign_id: str, config_name: str, run_id: str) -> dict:
        """The run's parsed ``capture/capture.json``, which names the world it needs.

        Raises ``SceneUnavailable`` when the run has none -- a run recorded without a capture has no
        motion to replay either, so there is nothing for geometry to serve.
        """
        import json

        from robovast.service.scene_cache import SceneUnavailable
        path = (Path(self._scene_source_dir(campaign_id)) / config_name / str(run_id)
                / "capture" / "capture.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as err:
            raise SceneUnavailable(
                "this run has no capture, so there is nothing to replay and no world to build geometry "
                "from. A capture is the simulator backend's to record -- see its documentation for "
                "what enables one -- and is written only on a clean stop.") from err
        except (OSError, ValueError) as err:
            raise SceneUnavailable(f"this run's capture manifest could not be read: {err}") from err

    def _scene_source_dir(self, campaign_id: str) -> str:
        """Where this campaign's files are read from when resolving geometry.

        Its own seam so the cluster lane can materialise just the two small objects it needs instead of
        the whole campaign prefix (the same reason ``_query_dir`` exists beside ``_data_dir``).
        """
        return str(self._campaign_dir(campaign_id))

    def _retrigger_source_dir(self, campaign_id: str) -> str:
        """Where a retrigger reads this campaign's frozen config and records from.

        The same seam as :meth:`_scene_source_dir` and for the same reason: locally the campaign
        is already on disk, while the cluster lane holds only an ephemeral cache and must
        materialise the handful of objects a retrigger reads. With that one override in place,
        the whole feature is plain filesystem code over one directory on both lanes.
        """
        return str(self._campaign_dir(campaign_id))

    def _scene_runner_context(self, campaign_id: str, identity: dict):
        """Context manager yielding the generator's container-runner factory, or None.

        Locally there is nothing to arrange: an absent factory makes the generator fall back to an
        ephemeral ``docker run`` on the campaign's image, which is exactly right. The cluster lane
        overrides this with an aux pod whose lifetime is the build's.
        """
        del campaign_id, identity
        return None

    def _resolve_image_digest(self, ref: str):
        """An image reference resolved to the bytes it currently names, or None.

        The lane-specific half of naming the simulator's image: a campaign that recorded only a
        declared *tag* (one from before per-role digests were written on this lane) can still be
        keyed on bytes, because Docker is right here to ask. The cluster lane deliberately does not
        implement this -- there is no pull-less registry lookup in the tree, and an aux pod's
        imageID arrives only after the pull, far too late to name the output directory -- so there a
        tag-only campaign is refused with a message instead of being guessed at. It costs nothing:
        that lane has recorded per-role digests since per-role digests existed.
        """
        from robovast.common.execution import \
            _get_image_revision  # pylint: disable=import-outside-toplevel
        revision = _get_image_revision(ref)
        return None if revision == "unknown" else revision

    def _scene_identity(self, campaign_id, config_name, run_id):
        from robovast.service import scene_cache
        manifest = self._scene_capture(campaign_id, config_name, run_id)
        identity = scene_cache.world_identity(self._scene_source_dir(campaign_id), manifest,
                                              resolve_digest=self._resolve_image_digest,
                                              config_name=config_name)
        return identity, scene_cache.cache_key(identity)

    def campaign_scene_status(self, campaign_id, config_name, run_id) -> "SceneStatus":
        from robovast.service import scene_cache
        from robovast.service.interface import SceneStatus
        base = SceneStatus(campaign_id=campaign_id, config_name=config_name, run_id=str(run_id))
        try:
            identity, key = self._scene_identity(campaign_id, config_name, run_id)
        except scene_cache.SceneUnavailable as err:
            return base.model_copy(update={"error": str(err), "note": str(err)})
        cached = scene_cache.is_cached(key)
        if cached:
            scene_cache.touch(key)
        running = scene_cache.is_generating(key)
        # A failed attempt is reported, not forgotten: "has not been built yet" is indistinguishable from
        # never having asked, so a viewer would offer Retry forever while the reason sat in the log.
        failure = "" if (cached or running) else scene_cache.last_failure(key)
        note = ""
        if cached:
            note = "geometry is cached; nothing will be built"
        elif running:
            note = "building this world's geometry (it is shared by every run that used it)"
        elif failure:
            note = failure
        else:
            note = "geometry has not been built for this world yet"
        if not identity["overrides_known"]:
            note += ("; this run's capture predates override recording, so geometry is compiled from "
                     "the bare world and may not reflect per-config world overrides")
        return base.model_copy(update={
            "cached": cached,
            "generation_required": not cached,
            "in_progress": running,
            "stage": self._scene_stage(campaign_id, key) if running else "",
            "bytes": scene_cache.entry_bytes(key) if cached else 0,
            "url": (Routes.campaign_scene_asset(campaign_id, f"{key}/scene.json")
                    if cached else ""),
            "world": identity["world"],
            "overrides_known": identity["overrides_known"],
            "error": failure,
            "note": note,
        })

    def _scene_stage(self, campaign_id: str, key: str) -> str:
        """Which step an in-flight build is on. Locally there is no queue and no image pull to watch,
        so the honest answer is the only one it can be."""
        del campaign_id, key
        return "compiling"

    def run_campaign_scene(self, campaign_id, config_name, run_id) -> ActionResult:
        from robovast.service import scene_cache
        try:
            identity, key = self._scene_identity(campaign_id, config_name, run_id)
        except scene_cache.SceneUnavailable as err:
            return ActionResult(ok=False, message=str(err))
        if scene_cache.is_cached(key):
            scene_cache.touch(key)
            return ActionResult(ok=True, message="geometry is already cached")
        if scene_cache.is_generating(key):
            return ActionResult(ok=True, message="this world's geometry is already being built")

        # None on this lane by design -- an absent factory makes the generator fall back
        # to an ephemeral `docker run`. The cluster lane overrides it with an aux pod.
        runner_context = self._scene_runner_context(  # pylint: disable=assignment-from-none
            campaign_id, identity)

        # A retry starts clean, so a stale reason cannot outlive the attempt that is about to replace it.
        scene_cache.clear_failure(key)

        def work():
            # The reason is recorded, not just logged: this runs after the POST has returned, so the
            # status endpoint is the only way it can reach the panel that is polling for it.
            try:
                scene_cache.generate(identity, key, runner_context=runner_context)
            except scene_cache.SceneUnavailable as err:
                logger.warning("scene generation failed for %s: %s", campaign_id, err)
                scene_cache.record_failure(key, str(err))
            except Exception as err:  # pylint: disable=broad-except
                logger.exception("scene generation crashed for %s", campaign_id)
                scene_cache.record_failure(key, f"the scene build crashed: {err}")

        # Deliberately not `_dispatch_background`: that sets a *campaign phase* and refuses while the
        # campaign is busy, so building geometry would show up as the campaign working and would be
        # blocked during a running sweep. This is a service-side cache fill, not a campaign lifecycle
        # step -- the same footing as an image build.
        threading.Thread(target=work, name=f"robovast-scene-{key[:8]}", daemon=True).start()
        return ActionResult(ok=True, message="building this world's geometry; poll the scene status")

    # -- the config view's geometry -----------------------------------------
    #
    # The same cache, the same key function and the same generator as a campaign's, keyed on a
    # world declared in a WORKSPACE instead of one a run recorded. So a project and a campaign
    # built from it share one entry: compile it once in the Config tab and the run view is warm.

    def _workspace_scene_identity(self, workspace_id: str, path: str = ""):
        from robovast.service import scene_cache
        from robovast.common.common import load_config
        project = self._resolve_project(workspace_id, path)
        raw = load_config(project.config_path) or {}
        sim_block = self._campaign_sim_block(raw)
        identity = scene_cache.workspace_world_identity(
            str(Path(project.config_path).parent), raw, sim_block,
            resolve_digest=self._resolve_image_digest)
        return identity, scene_cache.cache_key(identity)

    def _campaign_sim_block(self, raw: dict) -> dict:
        """The campaign-level ``sim`` block as the ``.vast`` declared it, or ``{}``.

        The campaign default only -- per-configuration overrides are deliberately not keyed into
        the geometry (see :func:`scene_cache.workspace_world_identity`). Best-effort: a project
        whose simulator plugin is not installed here still gets its bare world compiled, which is a
        usable picture, rather than no 3D view and an error about a plugin nobody asked about.
        """
        from robovast.common.simulators import (backend_name,  # pylint: disable=import-outside-toplevel
                                                campaign_sim_block)
        execution = raw.get("execution") or {}
        if not backend_name(execution):
            return {}
        try:
            return campaign_sim_block(execution) or {}
        except Exception as err:  # noqa: BLE001 - a bare world is still worth showing
            logger.debug("could not resolve the campaign sim block for the config view: %s", err)
            return {}

    def workspace_scene_status(self, workspace_id: str, path: str = "") -> "SceneStatus":
        from robovast.service import scene_cache
        from robovast.service.interface import SceneStatus
        base = SceneStatus(campaign_id=workspace_id, config_name="", run_id="")
        try:
            identity, key = self._workspace_scene_identity(workspace_id, path)
        except scene_cache.SceneUnavailable as err:
            return base.model_copy(update={"error": str(err), "note": str(err)})
        cached = scene_cache.is_cached(key)
        if cached:
            scene_cache.touch(key)
        running = scene_cache.is_generating(key)
        failure = "" if (cached or running) else scene_cache.last_failure(key)
        return base.model_copy(update={
            "cached": cached,
            "generation_required": not cached,
            "in_progress": running,
            "stage": "compiling" if running else "",
            "bytes": scene_cache.entry_bytes(key) if cached else 0,
            "url": (Routes.workspace_scene_asset(workspace_id, f"{key}/scene.json")
                    if cached else ""),
            "world": identity["world"],
            "overrides_known": True,
            "error": failure,
            "note": failure or ("geometry is cached; nothing will be built" if cached
                                else "geometry has not been built for this world yet"),
        })

    def run_workspace_scene(self, workspace_id: str, path: str = "") -> ActionResult:
        from robovast.service import scene_cache
        try:
            identity, key = self._workspace_scene_identity(workspace_id, path)
        except scene_cache.SceneUnavailable as err:
            return ActionResult(ok=False, message=str(err))
        if scene_cache.is_cached(key):
            scene_cache.touch(key)
            return ActionResult(ok=True, message="geometry is already cached")
        if scene_cache.is_generating(key):
            return ActionResult(ok=True, message="this world's geometry is already being built")
        scene_cache.clear_failure(key)

        def work():
            try:
                scene_cache.generate(identity, key)
            except scene_cache.SceneUnavailable as err:
                logger.warning("scene generation failed for workspace %s: %s", workspace_id, err)
                scene_cache.record_failure(key, str(err))
            except Exception as err:  # pylint: disable=broad-except
                logger.exception("scene generation crashed for workspace %s", workspace_id)
                scene_cache.record_failure(key, f"the scene build crashed: {err}")

        threading.Thread(target=work, name=f"robovast-wscene-{key[:8]}", daemon=True).start()
        return ActionResult(ok=True, message="building this world's geometry; poll the scene status")

    def resolve_workspace_scene_asset(self, workspace_id: str, path: str) -> str:
        """One file of a cached descriptor. The cache is keyed by content and shared, so the
        workspace only scopes the *route*, not the bytes."""
        del workspace_id
        return self.resolve_campaign_scene_asset("", path)

    def _run_state_path(self, campaign_id: str, config_name: str, run_id: str,
                        filename: str) -> Path:
        """Where this run's recording sits, for a lane that can hand out a path.

        Its own seam for the reason :meth:`_scene_source_dir` is: the cluster lane holds no run
        files locally and has to materialise this one object first.
        """
        return (Path(self._scene_source_dir(campaign_id)) / config_name / str(run_id)
                / filename)

    def campaign_screenshot(self, campaign_id, config_name, run_id, *, at=None, view=None,
                            focus=None, camera=None, size="960x720") -> str:
        """Render one moment of a run. Synchronous — see :mod:`robovast.service.screenshot`."""
        from robovast.service import screenshot  # pylint: disable=import-outside-toplevel
        from robovast.service.scene_cache import \
            SceneUnavailable  # pylint: disable=import-outside-toplevel
        try:
            # The same identity geometry is built from: it resolves the campaign's simulator
            # image, refuses a mutable tag, and carries the `_config/` mount a campaign-file
            # world needs. Reused rather than re-derived so the two cannot disagree about which
            # image a campaign's simulator is.
            identity, _key = self._scene_identity(campaign_id, config_name, run_id)
        except SceneUnavailable as err:
            raise screenshot.ScreenshotUnavailable(str(err)) from err
        return str(screenshot.render(
            identity,
            state_path=self._run_state_path(campaign_id, config_name, run_id,
                                            screenshot.state_filename(identity)),
            at=at, view=view or {}, focus=focus or [], camera=camera, size=size,
            runner_context=self._scene_runner_context(campaign_id, identity)))

    def resolve_campaign_scene_asset(self, campaign_id: str, path: str) -> str:
        """Resolve ``<key>/<file>`` within the shared descriptor cache.

        The **key is in the path** rather than re-derived from a run, for two reasons: the descriptor's
        loader fetches ``scene.bin`` and every texture as *relative siblings* of ``scene.json``, so one
        URL prefix has to address the whole entry; and an entry is shared by every campaign that used
        that world, so there is no single run it belongs to. The client never builds this URL -- the
        status response hands it over -- and ``asset_path`` refuses anything escaping the entry.
        """
        del campaign_id  # scopes the route, but a cache entry belongs to a world, not a campaign
        from robovast.service import scene_cache
        key, _, rel = str(path).partition("/")
        if not key or not rel:
            raise KeyError(f"scene asset path must be '<key>/<file>', got {path!r}")
        scene_cache.touch(key)
        return scene_cache.asset_path(key, rel)

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

    # Node levels the web Explorer tree can address (campaign → batch → config → run), the
    # same set the desktop viewer offers. ``batch`` is a *logical* level: it has no directory
    # of its own (see :meth:`_node_data_dir`), so it is identified by the injected ``BATCH``
    # index instead, and it only appears in the tree for a search campaign.
    _VIS_LEVELS = ("run", "config", "batch", "campaign")

    def _visualization_workloads(self, campaign_id: str):
        """Parse ``visualization.results.explorer.notebooks`` from the snapshot ``.vast``.

        Returns ``({workload_name: {level: notebook_path}}, config_dir)`` — notebook
        paths are resolved against the ``_config`` snapshot dir, where the campaign's
        explorer notebooks are copied (see ``common.execution``).
        """
        from robovast.common.config import visualization_block
        from robovast.common.config_validation import _safe_load
        config_dir = Path(self._config_dir(campaign_id))
        vasts = sorted(config_dir.glob("*.vast")) if config_dir.is_dir() else []
        workloads: dict = {}
        if vasts:
            cfg, _ = _safe_load(str(vasts[0]))
            for view in (visualization_block(cfg, "results", "explorer", "notebooks") or []):
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
        from robovast.service.interface import CampaignVisualization, CampaignVisualizationsResponse
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
        config_name: str = "", run_id=None, theme: str = "light", batch=None,
    ) -> str:
        from robovast.results_processing.notebook_render import render_notebook_html
        workloads, _ = self._visualization_workloads(campaign_id)
        notebooks = workloads.get(workload)
        if not notebooks or level not in notebooks:
            raise KeyError(f"No '{level}' notebook for workload '{workload}'.")
        data_dir = self._node_data_dir(campaign_id, level, config_name, run_id)
        # Which batch a ``batch``-level notebook is for. Injected rather than derived from
        # DATA_DIR because a batch has no directory of its own; passed only when known, so a
        # notebook's own ``BATCH = None`` default survives and it can say so.
        inject = {"BATCH": int(batch)} if batch is not None else None
        with self._render_progress(campaign_id, workload) as on_cell:
            return render_notebook_html(notebooks[level], data_dir, theme=theme,
                                        on_cell=on_cell, inject=inject)

    @contextlib.contextmanager
    def _render_progress(self, campaign_id: str, workload: str):
        """Yield an ``on_cell(done, total)`` to report execution progress with, or ``None``.

        A seam, not a feature, at this level: locally the data is already on disk and the
        only cost is the cells themselves, with nowhere to publish counts to. The cluster
        service overrides it, because there this render is the tail of a wait that started
        with a multi-minute transfer and the caller is still watching.
        """
        del campaign_id, workload
        yield None

    def _node_data_dir(self, campaign_id: str, level: str, config_name: str, run_id):
        """The ``DATA_DIR`` for a selected node — the campaign/config/run directory."""
        base = Path(self._whole_campaign_dir(campaign_id))
        # A batch is a grouping recorded in the store, not a directory level: a search
        # campaign's configs sit flat under the campaign root whichever round proposed them.
        # So a batch notebook gets the campaign root and is told *which* batch through the
        # injected ``BATCH`` index -- the same contract the desktop viewer uses.
        if level in ("campaign", "batch"):
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

    def _record_dir(self, cid: str) -> Path:
        """The directory holding *cid*'s **recorded facts** — ``campaign.db`` and
        ``_execution/outcome.json``.

        Its own directory under the results root, locally: the campaign is written there
        and read back from there. The seam exists for :class:`ClusterService`, whose
        durable home is the object store — in-pod there is no local copy at all, so every
        reader below would answer ``unknown``/zero for a campaign the store holds. Four
        readers wanted that directory (:meth:`_summary_for`, :meth:`_started_at_for`,
        :meth:`_description_for`, :meth:`_status_from_disk`) and each resolved it inline,
        so the override had to be written four times or not at all.
        """
        return self._campaign_dir(cid)

    def _summary_for(self, cid: str) -> CampaignSummary:
        from robovast.common.store import read_campaign_mode
        from robovast.execution.status_recovery import reconstruct_status_from_disk
        campaign_dir = self._record_dir(cid)
        with self._lock:
            entry = self._campaigns.get(cid)
        # One precedence rule, shared with get_status: a tracked campaign's live
        # ControllerState wins; otherwise reconstruct the Status from disk (the one
        # documented recovery path — it also derives `postprocessed` from data.db).
        # `started_at` follows the same rule via _started_at_for, which is also what
        # list_campaigns orders by — so the time shown on a row and the time it was
        # sorted by cannot disagree.
        if entry is not None:
            snap = self._derive_postprocessed(cid, entry.state.snapshot())
        else:
            snap = reconstruct_status_from_disk(campaign_dir)
        started_at = self._started_at_for(cid)
        counts = self._run_counts(campaign_dir, live=entry is not None)
        return CampaignSummary(
            campaign_id=cid, phase=snap.phase, postprocessed=snap.postprocessed,
            description=self._description_for(cid) or "",
            created_by=self._created_by_for(cid) or "",
            started_at=started_at,
            # The store is consulted behind the snapshot rather than instead of it: a
            # reconstructed Status can carry no mode at all, because the `outcome.json`
            # early-return path hands back whatever the controller journalled and an older
            # record predates the field. `read_campaign_mode` is the read-only fallback that
            # exists for exactly this, and "" is recorded when neither knows.
            mode=snap.mode or read_campaign_mode(campaign_dir) or "",
            num_runs=counts["num_runs"], num_passed=counts["num_passed"],
            num_failed=counts["num_failed"] + counts["num_errors"],
            num_composition_failed=counts.get("num_composition_failed", 0),
            # From the same snapshot as the phase, so a listing cannot show a campaign as
            # finished-and-fine while its Status says postprocessing failed.
            postprocessing_error=snap.postprocessing_error or "",
            share_error=snap.share_error or "")

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
        if counts is not None and (live or counts["num_runs"] > 0
                                   or counts.get("num_composition_failed", 0) > 0):
            # A campaign whose every draw failed to compose has zero runs and yet is
            # fully accounted for: without this the store's real answer is discarded
            # for a disk walk that can only find the runs that do not exist.
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
        """Legacy fallback: derive counts by walking each run's ``test.xml``.

        ``num_composition_failed`` is 0 here by necessity, not by finding none: a
        draw that never composed left no directory for a disk walk to see. Only the
        store knows about those, which is why this is the last resort.
        """
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
            "num_composition_failed": 0,
        }

    def _started_at_for(self, cid: str) -> Optional[str]:
        """Start time of *cid* as an ISO-8601 UTC string, or None if unknown.

        Same precedence as :meth:`_summary_for`: a campaign this service is driving
        reports its in-memory launch time, so it is ordered correctly from t=0 — before
        the controller has written the ``campaign`` row seconds later. Otherwise the
        durable record in ``campaign.db`` is read.

        Memoised because listing has to know every candidate's start time to order them,
        and the SSE stream re-lists once a second. A recorded start time never changes
        (``CampaignStore.create_campaign`` stamps it once, and the post-hoc indexer
        preserves it across rebuilds), so a cached value cannot go stale. ``None`` is
        deliberately *not* cached: a campaign whose store does not exist yet must be
        re-read on the next poll.
        """
        with self._lock:
            entry = self._campaigns.get(cid)
        if entry is not None:
            return entry.created_at
        cached = self._started_at_cache.get(cid)
        if cached is not None:
            return cached
        started = read_campaign_created_at(self._record_dir(cid))
        if started is not None:
            self._started_at_cache[cid] = started
        return started

    def _description_for(self, cid: str) -> Optional[str]:
        """The campaign's description, or None when it was launched without one.

        Same precedence and caching rationale as :meth:`_started_at_for`: the live
        entry answers for a campaign this process launched (its store row may not
        exist yet), the durable ``campaign.db`` answers for every other one, and the
        value is memoised because the SSE stream re-lists once a second. A description
        is written once with the campaign row and never edited, so a cached value
        cannot go stale; ``None`` is not cached, since a campaign whose store is not
        written yet must be re-read on the next poll.
        """
        with self._lock:
            entry = self._campaigns.get(cid)
        if entry is not None:
            return entry.description or None
        cached = self._description_cache.get(cid)
        if cached is not None:
            return cached
        description = read_campaign_description(self._record_dir(cid))
        if description is not None:
            self._description_cache[cid] = description
        return description

    def _created_by_for(self, cid: str) -> Optional[str]:
        """Who says they launched *cid*, or None when nobody gave a name.

        Sibling of :meth:`_description_for`, with the same precedence and the same
        reason for caching: the live entry answers for a campaign this process
        launched, the durable ``campaign.db`` for every other one, and the SSE stream
        re-lists once a second. Written once with the campaign row and never edited, so
        a cached value cannot go stale.
        """
        from robovast.common.store import read_campaign_created_by
        with self._lock:
            entry = self._campaigns.get(cid)
        if entry is not None:
            return entry.created_by or None
        cached = self._created_by_cache.get(cid)
        if cached is not None:
            return cached
        created_by = read_campaign_created_by(self._record_dir(cid))
        if created_by is not None:
            self._created_by_cache[cid] = created_by
        return created_by

    def _status_from_disk(self, campaign_id: str) -> Status:
        from robovast.execution.status_recovery import reconstruct_status_from_disk
        return reconstruct_status_from_disk(self._record_dir(campaign_id))
