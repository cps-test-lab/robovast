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
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Optional

from robovast.common import file_address, file_view
from robovast.common.safe_path import safe_join
from robovast.common.store import (read_campaign_created_at,
                                   read_campaign_description)
from robovast.execution.control_server import (ControllerState, Phase, Status,
                                               failure_detail, is_terminal)
from robovast.common.host_display import require_host_display
from robovast.service.interface import (ActionResult, BuildImageRequest,
                                        CampaignRef,
                                        CampaignSummary, CreateCampaignRequest,
                                        CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest,
                                        FileEntry, FileListing, FileMeta,
                                        FileText, ImageBuildRef,
                                        ImageBuildStatus, JobCounts,
                                        JobSummary, ListCampaignsRequest,
                                        ListCampaignsResponse, ListJobsResponse,
                                        ListWorkspacesResponse,
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
                 "description")

    def __init__(self, campaign_id: str, results_dir: str, state: ControllerState,
                 description: str = ""):
        from datetime import datetime, timezone
        self.campaign_id = campaign_id
        self.results_dir = results_dir
        self.state = state
        # Held here as well as in campaign.db: the store row is written by the
        # controller, so between accepting the launch and that write (an image build
        # can make it minutes) this is the only copy — and for a campaign that fails
        # during the build it stays the only one.
        self.description = description
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
    """

    config_path: str


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

    def __init__(self, store=None, workspace_dir=None):
        self._campaigns: dict[str, _LocalCampaign] = {}
        self._lock = threading.Lock()
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
            from robovast.service.workspaces import PINNED_SKIP_DIRS
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
        return WorkspaceInfo.model_validate(self.store.registry.create(request.name))

    def list_workspaces(self) -> ListWorkspacesResponse:
        return ListWorkspacesResponse(workspaces=[
            WorkspaceInfo.model_validate(e) for e in self.store.registry.list()])

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
        root = Path(self._data_dir(owner))
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
        """The file's real path on this host, for a lane that has one.

        Lets the HTTP layer serve it with ``FileResponse`` — streamed, with ``Range`` and
        conditional-request handling — instead of reading it whole into memory. A
        campaign's rosbag is tens of megabytes and up, and ``read_file_bytes`` buffers all
        of it per request just to hand it back.

        The cluster service does **not** override this (its results are object-store
        entries, with no path to hand out), so the HTTP layer treats its absence as "this
        lane cannot stream" rather than as an error. That is a real difference between the
        substrates, not a fallback for one behaviour.
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
        # relaying every byte through the interface. ``app.py`` blanks them again for a
        # non-loopback request — the same-host precondition is the caller's, not ours.
        return VersionInfo(robovast_version=_robovast_version(), backend="docker",
                           backends=["local"],
                           results_root=str(self._campaigns_root()),
                           sources_root=str(self.store.registry.root))

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

        # Before anything is resolved or created: a lane that cannot show a window, or a
        # serve host with no display, must refuse rather than launch a windowless run.
        self._admit_show_gui(request)

        target = self._resolve_project(request.workspace_id, request.config_path)
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

        state = ControllerState()
        entry = _LocalCampaign(campaign_id, results_dir, state,
                               description=request.description)
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

        # If execution.image is a symbolic ``build:<tag>`` ref the campaign needs an image
        # built before it can run. The phase is set here, synchronously, so the campaign is
        # listed as ``building`` from the instant it is accepted; the build itself is
        # *driven by the worker* (below), because this method must return a handle rather
        # than block: awaiting the build here made a 30s-read-timeout client report failure
        # for a campaign that went on to succeed, and left the caller with no id to poll.
        # The phase means the campaign is **waiting for** its image — builds are
        # content-addressed and shared, so it is not necessarily performing one.
        spec, _ = self._build_spec_for(target, campaign_config)
        if spec is not None:
            state.set_phase(Phase.BUILDING,
                            stage=f"waiting for image {spec.tag}")

        def _worker():
            from robovast.execution.backends import CampaignStopped
            backend = None
            try:
                # Before anything that can fail, so every later outcome — a doomed build
                # included — belongs to a campaign that can be found again.
                self._on_campaign_started(campaign_id, entry.created_at)
                # Build (or join a sibling's build of) the experiment image and pin the
                # concrete ref, so the backend uses it (explicit wins in
                # resolve_robovast_image). A failed build is no longer a failed *request*:
                # it raises into the handler below and becomes an inspectable ``failed``
                # campaign, with the reason in its status and the output in its own log.
                build = self._start_build_image(target, campaign_config)
                if build is not None:
                    self._await_build_image(build.build_id, state, campaign_root)
                    options.image = self._resolve_built_image(target, campaign_config)
                state.set_phase(Phase.STARTING)
                with self._campaign_context(campaign_id, target):
                    backend = self._build_backend(state)
                    if is_search:
                        run_search_campaign(
                            target.config_path, campaign_config, results_dir, runs,
                            backend=backend, options=options,
                            campaign_id=campaign_id, state=state,
                            description=request.description)
                    else:
                        run_batch_campaign(
                            target.config_path, campaign_config, results_dir, runs,
                            config_filter=config_filter, backend=backend,
                            options=options, campaign_id=campaign_id, state=state,
                            description=request.description)
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
        return CampaignRef(campaign_id=campaign_id,
                           note=_show_gui_note(request, raw_config))

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

    def _start_build_image(self, project, campaign_config) -> "ImageBuildRef | None":
        """Submit (or join) the build of the project's ``build:`` image; return its ref.

        ``None`` when the project has no ``build:`` section. Returns as soon as the build
        has a *handle* — it does not wait for it; :meth:`_await_build_image` does that, on
        the campaign's own worker thread. Overridden by :class:`ClusterService` for the
        in-cluster BuildKit Job.
        """
        spec, project_dir = self._build_spec_for(project, campaign_config)
        if spec is None:
            return None
        return self._image_builds.start(spec, project_dir)

    def _resolve_built_image(self, project, campaign_config) -> str:
        """The concrete image ref to pin once the build is done (see ``_run_options``)."""
        spec, project_dir = self._build_spec_for(project, campaign_config)
        return self._image_builds.resolve_ref(spec, project_dir)

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
            spec.image = self._exec_image(vast_file)
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

    def _exec_image(self, vast_file: str) -> str:
        """The concrete image to exec in, resolved exactly as a run would resolve it.

        A ``build:<tag>`` must already exist locally: building implicitly would turn a
        seconds-long check into a multi-minute one the caller never asked for. A project
        with **no** ``build:`` section is ordinary, not an error — it falls back to
        ``execution.image`` and then the default, like any run.
        """
        from robovast.common.common import load_config
        from robovast.common.execution import (is_build_image_ref,
                                               resolve_robovast_image)
        campaign_config = load_config(vast_file) or {}
        configured = (campaign_config.get("execution") or {}).get("image")
        if not is_build_image_ref(configured):
            return resolve_robovast_image(config_image=configured, required=True)

        target = WorkspaceTarget(config_path=vast_file)
        spec, project_dir = self._build_spec_for(target, campaign_config)
        if spec is None:
            raise ValueError(
                f"execution.image is {configured!r} but the project has no 'build:' "
                "section to resolve it from")
        ref = self._image_builds.resolve_ref(spec, project_dir)
        if not self._image_builds.image_exists(ref):
            raise ValueError(
                f"image {configured!r} is not built — call build_experiment_image "
                "first. This never builds implicitly, so a quick check cannot silently "
                "become a full image build.")
        return ref

    def stop_exec_container(self, backend: "str | None" = None) -> "ExecStopResult":  # noqa: F821
        del backend            # single-lane service; a multi-backend one overrides
        return self._exec_manager.stop()

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
        result = describe_data_db(self._query_dir(campaign_id))
        return DataDescribe(campaign_id=campaign_id, **result)

    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        extra_campaign_ids=None,
    ) -> "DataQueryResult":
        from robovast.results_processing.data_query import query_data_db
        from robovast.service.interface import DataQueryResult
        extra_dirs = {f"c{i + 1}": self._query_dir(cid)
                      for i, cid in enumerate(extra_campaign_ids or [])}
        result = query_data_db(self._query_dir(campaign_id), sql, max_rows,
                               extra_dirs=extra_dirs)
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
        """Campaign dir holding data.db/campaign.db. Local: on disk (this transport
        overrides via ``_campaign_dir``); the cluster service fetches from the
        object store (``ClusterService`` overrides this)."""
        return self._campaign_dir(campaign_id)

    def _query_dir(self, campaign_id: str):
        """Dir a **query** reads: it needs only ``_execution/data.db`` + ``campaign.db``
        (see ``data_query._open_db``).

        Locally identical to :meth:`_data_dir`. Separate from it because on the cluster the
        two answers differ by orders of magnitude — ``_data_dir`` materializes the whole
        campaign, a query needs two objects — so ``ClusterService`` overrides this one
        alone. Callers needing arbitrary campaign files (notebook render, panel assets,
        endpoint plugins via :meth:`resolve_data_dir`) must keep using ``_data_dir``."""
        return self._data_dir(campaign_id)

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
                "from. A run records one when ROBOSITO_RECORD and ROBOSITO_CAPTURE_EXPORT_DIR are "
                "set, and only on a clean stop.") from err
        except (OSError, ValueError) as err:
            raise SceneUnavailable(f"this run's capture manifest could not be read: {err}") from err

    def _scene_source_dir(self, campaign_id: str) -> str:
        """Where this campaign's files are read from when resolving geometry.

        Its own seam so the cluster lane can materialise just the two small objects it needs instead of
        the whole campaign prefix (the same reason ``_query_dir`` exists beside ``_data_dir``).
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

    def _scene_identity(self, campaign_id, config_name, run_id):
        from robovast.service import scene_cache
        manifest = self._scene_capture(campaign_id, config_name, run_id)
        identity = scene_cache.world_identity(self._scene_source_dir(campaign_id), manifest)
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

        runner_context = self._scene_runner_context(campaign_id, identity)

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
        from robovast.execution.status_recovery import \
            reconstruct_status_from_disk
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
            snap = entry.state.snapshot()
        else:
            snap = reconstruct_status_from_disk(campaign_dir)
        started_at = self._started_at_for(cid)
        counts = self._run_counts(campaign_dir, live=entry is not None)
        return CampaignSummary(
            campaign_id=cid, phase=snap.phase, postprocessed=snap.postprocessed,
            description=self._description_for(cid) or "",
            started_at=started_at,
            num_runs=counts["num_runs"], num_passed=counts["num_passed"],
            num_failed=counts["num_failed"] + counts["num_errors"],
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

    def _status_from_disk(self, campaign_id: str) -> Status:
        from robovast.execution.status_recovery import \
            reconstruct_status_from_disk
        return reconstruct_status_from_disk(self._record_dir(campaign_id))

