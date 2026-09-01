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
uses. This is the lane behind ``vast serve --backend local``; campaigns die with the
service process.
:class:`~robovast.execution.cluster_execution.cluster_service.ClusterService` subclasses
this, reusing its driver-hosting shape and overriding only the launch hooks.

Split out of the former single ``client`` module; ``client`` now re-exports
``LocalTransport`` so existing imports keep working.
"""

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Callable, Optional

from robovast.client import file_address
from robovast.client.safe_path import safe_join
from robovast.common import file_view
from robovast.common.config import (EXPLORER_SCOPES, SCENARIO_CONTAINER,
                                    SIMULATION_CONTAINER)
from robovast.common.host_display import require_host_display
from robovast.common.campaign_data import read_campaign_finished_at
from robovast.common.store import read_campaign_created_at, read_campaign_description
from robovast.execution.control_server import (ControllerState, Phase, Status, failure_detail,
                                               is_terminal)
from robovast.service.interface import (ActionResult, CampaignOrigin, CampaignRef,
                                        UpgradeInfo,
                                        CampaignSummary, OriginKind, ShareListing,
                                        CreateCampaignRequest, CreateUploadRequest,
                                        CreateWorkspaceRequest, DiskSpace, EditFileRequest, FileEntry,
                                        FileListing, FileMeta, FileText, ImageBuildRef,
                                        ImportCampaignRequest, JobCounts, JobKind,
                                        JobSummary, ListCampaignsRequest, ListCampaignsResponse,
                                        ListJobsResponse, ListWorkspacesResponse, LogChunk,
                                        PreviewConfiguration, PreviewResponse, ResourceUsage,
                                        MigrationMarker, RetriggerAxis, RetriggerReport,
                                        RobovastInterface, Routes, SearchHistory, WorkOrder,
                                        UploadGrant, ValidationProblem,
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


def _build_date() -> str:
    """When the image this process runs was built, or ``""`` when unavailable.

    Never raises and never substitutes: a source checkout has no build to date, and a
    manufactured one — the file's mtime, today — would be read as the age of the deployment
    and believed. Same contract as :func:`_code_revision`, for the same reason.
    """
    try:
        from robovast.common.execution import build_date
        return build_date()
    except Exception:  # noqa: BLE001 - diagnostics must not break the handshake
        return ""


def _package_version() -> str:
    """The packaged semver of the running code, or ``""`` when there is no metadata.

    Deliberately *not* ``get_app_version``, which prefers a revision and so answers a
    different question. This is the release an operator can look up in a changelog, and
    it needs a reader of its own: a deployed image always has a baked revision, so
    :func:`_robovast_version` short-circuits to that and the semver never surfaces there.

    ``""`` is the honest answer for a source tree with no metadata, in the same way ``""``
    is for an undeterminable revision — never a substituted revision, which would look
    like a release that does not exist.
    """
    try:
        return _pkg_version("robovast")
    except PackageNotFoundError:  # editable/source without metadata
        return ""


def _robovast_version() -> str:
    """The version of the code *this process is running*.

    ``get_app_version`` prefers the git revision (with ``+dirty`` for an unclean tree)
    and falls back to package metadata. That preference is the point: a service is
    long-lived and loads its code once, so a client needs to tell "the fix I just made
    is loaded" from "this process predates it". The packaged version alone cannot —
    it stays ``2.0.0`` across every edit.

    The consequence is that this is a revision on any real deployment, which is why the
    semver has a field of its own (:func:`_package_version`) rather than being read off
    this one.
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


def _preview_tag(workspace_id: str, path: str) -> str:
    """A stable, name-safe tag for the project a preview is composing.

    Stable so repeated previews of the same file address the same held aux container
    instead of starting a fresh one each time; hashed because a workspace id and a config
    path together respect neither the length limit nor the character set a container or pod
    name does.
    """
    digest = hashlib.sha1(f"{workspace_id}:{path}".encode("utf-8")).hexdigest()[:12]
    return f"preview-{digest}"


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


def _no_timeout_note(raw_config: dict) -> str:
    """Say, at launch, that this campaign cannot be judged stalled — before it is.

    ``execution.timeout`` is what makes ``stalled`` a verdict rather than ``null``, and
    ``null`` is the one answer nobody acts on: a campaign that declared no budget gets no
    stall verdict, and so ``vast campaign wait`` can never end on one. Told here because the fix is
    a line in the ``.vast`` and this is the last moment before the compute is spent; four
    minutes into a wedged sweep it is only an explanation.

    Advisory, not a validation error, and for the reason :func:`_show_gui_note` is: a
    campaign with no declared per-run budget is a legitimate thing to run.
    """
    from robovast.common.config import declared_per_run_seconds

    execution = (raw_config or {}).get("execution") or {}
    if not isinstance(execution, dict) or declared_per_run_seconds(execution):
        return ""
    return ("this project declares no execution.timeout, so no stall verdict is possible "
            "for it — `vast campaign wait` cannot end on one, and get_campaign_status reports "
            "stalled: null, which is not 'healthy'. Declare it to get a verdict. (A "
            "simulator that reports on itself is unaffected: its own findings still end "
            "the wait.)")


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
                 "description", "created_by", "workspace_id", "origin")

    def __init__(self, campaign_id: str, results_dir: str, state: ControllerState,
                 description: str = "", workspace_id: str = "",
                 created_by: str = "", origin=None):
        self.campaign_id = campaign_id
        self.results_dir = results_dir
        self.state = state
        # Which workspace this campaign is *currently* reading its project from, so a
        # push can be refused while it runs. Live-only and deliberately never persisted:
        # ``write_launch_record`` leaves ``workspace_id`` out because a finished campaign
        # is workspace-independent, and that stays true. Empty for a launch with no
        # workspace behind it (a retrigger runs from its own staged copy).
        self.workspace_id = workspace_id
        # Kept beside ``workspace_id`` rather than folded into it, because the two answer
        # different questions and only happen to agree for a plain workspace launch. That
        # field is a *liveness* reading -- "is a campaign reading this workspace right now?"
        # -- and is correctly empty for a retrigger, which runs from its own staged copy.
        # This is the *record* of where the configuration came from, and a retrigger has one
        # (the lineage it was re-run from). Merging them would put the record/link conflation
        # this whole field is careful about inside a single attribute.
        self.origin = origin
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

    Deliberately just the config path, not the CLI's ``ProjectConfig``. The service would
    synthesize one per call with a constant ``results_dir``, so the type would imply a
    choice that is never made. Every campaign lands in the shared
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
    #: Where this project's configuration came from, recorded on the campaign and never read
    #: back to run anything (see ``interface.CampaignOrigin``). Here for the reason the
    #: docstring gives above: this object already *is* the launch path's knowledge of the
    #: project, so the record travels with it rather than through a second channel. ``None``
    #: when the launch path cannot say.
    origin: Optional[CampaignOrigin] = None
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
    #: Adopt this campaign id rather than minting a fresh one. Set only when re-entering a
    #: campaign that already exists and is still owed work -- one whose driver a service
    #: restart took away (see ``cluster_execution.campaign_resume``).
    #:
    #: This is the whole difference between a launch and a re-launch, which is why it is one
    #: field here rather than a mode flag on :meth:`LocalTransport._launch_campaign`: that
    #: method's contract is that everything a non-workspace project needs to say travels on
    #: this object. Setting it also waives the "already exists" guard below, because for a
    #: re-entry the directory being there is the point rather than a collision.
    campaign_id: Optional[str] = None


#: How long a job-state read may take. Short on purpose: this runs on the status path, so a wedged
#: container must cost a bounded wait and then say so, never hold a caller. The command it runs is a
#: tail of two records, which answers in well under a second when anything is answering at all.
_JOB_STATE_LIMIT_S = 20

#: How long one health read stands in for the next. The waiter's own poll interval, so a status
#: read never triggers a second exec for a job already asked this interval -- N watchers cost one
#: check, and nobody watching costs none at all. Latency to notice is bounded by this, which is
#: irrelevant against runs measured in minutes.
_HEALTH_TTL_S = 10.0

#: How long a caller's own command may run in a live job. Longer than a state read -- a caller may
#: legitimately watch a topic for a few seconds -- but still a cap: this holds a request open, and a
#: command that needs longer wants ``exec_in_container``, where nothing is waiting on it.
_PROBE_LIMIT_S = 60

#: How long an uploaded-but-never-imported archive is kept before it is swept. Long enough that
#: it cannot collide with an upload still in flight or a user deciding whether to force a
#: replace, short enough that abandoned multi-gigabyte archives do not accumulate.
STAGED_ARCHIVE_MAX_AGE_S = 24 * 60 * 60

#: How long a campaign-archive upload grant stays redeemable, matching the workspace store's
#: ``UPLOAD_TTL_SECONDS``. It bounds the wait *before* the PUT begins, not the transfer: the
#: grant is consumed when the request arrives, so a multi-hour upload of a large campaign is
#: not racing this.
ARCHIVE_UPLOAD_TTL_SECONDS = 600


def _archive_has_metrics(archive_path) -> bool:
    """Whether the archive already carries ``_execution/data.db``, from the tar index.

    Postprocessing writes that file and nothing else does, so its presence is the whole
    raw-versus-postprocessed question -- answerable from the member list, without
    extracting anything, before the import even starts.
    """
    import tarfile  # pylint: disable=import-outside-toplevel
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            return any(name.endswith("/_execution/data.db") for name in tar.getnames())
    except (tarfile.TarError, OSError):
        # Unreadable is not "postprocessed"; the extraction that follows will say so
        # properly, and until then the safe answer is the one that runs postprocessing.
        return False


def _throttled_transfer_log(log, every: float = 0.10):
    """A ``(received, total)`` provider callback that logs a line per *every* of the whole.

    The providers call back per chunk, which in a campaign log is thousands of lines saying
    nothing. A share download is the least inspectable minutes an import has -- gigabytes
    from somebody else's storage -- so it gets an account of itself, just not that one.
    """
    seen = {"mark": 0.0}

    def _cb(received, total):
        if total <= 0:
            return
        fraction = received / total
        if fraction < seen["mark"] and received < total:
            return
        seen["mark"] = fraction + every
        log(f"  {fraction * 100:5.1f}%  {received}/{total} bytes")

    return _cb


class LocalTransport(RobovastInterface):
    """In-process implementation over the local Docker backend.

    A campaign always runs a **workspace's** ``.vast``: ``workspace_id`` is the only
    project binding this service accepts (see :meth:`_resolve_project`), and
    ``config_path``/``vast_path`` selects among several ``.vast`` files in that
    workspace. Nothing ambient selects what the service runs; the results root is
    named by ``vast serve --results-dir`` (see
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

    def __init__(self, store=None, workspace_dir=None, results_dir=None):
        #: Where local campaigns land, when the caller pinned one (``vast serve
        #: --results-dir``). ``None`` leaves it to the service-owned default; see
        #: :meth:`_campaigns_root`.
        self._results_dir = results_dir
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
        # campaign_id -> what its running jobs' simulators last said about themselves, and
        # when. Held in memory and never written: a diagnostic is not a result, and a campaign
        # nobody polls is never asked. Refreshed off the request thread (see _attach_health),
        # so a wedged container cannot hold a status read even for the exec's own timeout.
        self._health: dict[str, dict] = {}
        self._health_guard = threading.Lock()
        # campaign_id -> recorded start time (see _started_at_for). Only known values
        # are held, and a recorded one never changes, so no invalidation is needed.
        self._started_at_cache: dict[str, str] = {}
        #: campaign_id -> recorded finish time (see _finished_at_for). Unlike the caches
        #: beside it this one CAN go stale: a re-triggered postprocessing or a re-run
        #: export ends the campaign again and moves its finish time, so
        #: `_dispatch_background` drops the entry when it registers such an operation.
        self._finished_at_cache: dict[str, str] = {}
        # campaign_id -> recorded description (see _description_for). Same contract as
        # the start-time cache: write-once values only, so no invalidation is needed.
        self._description_cache: dict[str, str] = {}
        self._created_by_cache: dict[str, str] = {}
        self._origin_cache: dict[str, CampaignOrigin] = {}
        # campaign_id -> (rest key, answer) for the two EXPENSIVE reads, which -- unlike the
        # write-once facts above -- can change, so each entry carries the key it was computed
        # from and is discarded the moment that key moves. See _rest_key for what the key is
        # and why it is a file stat rather than an invalidation call.
        self._summary_cache: dict[str, tuple] = {}
        self._disk_status_cache: dict[str, tuple] = {}
        # token -> (expiry, staged archive path) for campaign-archive uploads. In memory by
        # design; see the "taking a campaign in" section.
        self._archive_grants: dict[str, tuple[float, Path]] = {}
        self._archive_grants_lock = threading.Lock()
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

        There is no fallback here. Resolving an empty ``workspace_id`` against the
        ``.robovast_project`` in the *service's* CWD would ignore ``vast_path`` entirely,
        so a caller naming one ``.vast`` silently gets whichever one was initialized -- a
        campaign that runs the wrong simulator and looks successful. Nor would an
        in-process runner need it: such a caller drives the controller directly and never
        reaches this method.
        """
        if not workspace_id:
            raise ValueError(
                "workspace_id is required: the service runs a workspace's project. "
                "List them with 'vast workspace list' / list_workspaces(); pin a "
                "directory in place with 'vast serve --workspace-dir <dir>', or "
                "upload one with 'vast workspace init <dir>'.")
        return self._project_for_workspace(workspace_id, vast_path)

    def _campaigns_root(self) -> Path:
        """The single results root every local campaign shares.

        Campaigns are self-contained and **workspace-independent**, so every
        local Docker campaign — launched from a workspace *or* the CWD project —
        lands here and is listed / reconstructed / queried from here. Writing them
        under ``<workspace>/results`` instead would both hide them from the
        service's readers and let ``delete_workspace`` take the campaigns with it.

        ``vast serve --results-dir`` wins when it was given: the caller naming a directory
        on the serve host is the most specific answer there is, and the only one, since no
        project file binds a results dir. Otherwise the precedence lives in
        :func:`~robovast.common.results_root.local_results_root`, shared with the MCP results
        reader so the two cannot disagree about where a campaign is.

        Pure path resolver — the dir is materialized lazily by ``CampaignStore`` on first
        run, so simply asking where campaigns live never creates a stray local directory.

        :class:`ClusterService` uses this too. Its campaigns' *durable* home is the object
        store, but the ones it is driving have a working root here all the same, and it is
        not a cache: a batch downloads its results into it, extraction reads it through a
        path, and postprocessing derives ``data.db`` from it.
        """
        from robovast.common.results_root import local_results_root
        if self._results_dir:
            return Path(self._results_dir)
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
        entry = self.store.registry.require(workspace_id)
        workspace_id = entry["workspace_id"]
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
        # The origin is recorded from what was just resolved, not from the request: the
        # request may name no .vast at all (the sole-file case above), and the name is the
        # registry's rather than whatever alias the caller passed. Relative to the project
        # root because the campaign's own _config/ keeps only the basename, so a project
        # holding several .vast files in subdirectories would otherwise be ambiguous.
        origin = CampaignOrigin(
            kind=OriginKind.WORKSPACE,
            workspace_id=workspace_id,
            workspace_name=entry.get("name") or "",
            config_path=Path(config_path).relative_to(project_dir).as_posix())
        return WorkspaceTarget(config_path=str(config_path), origin=origin)

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
        # `upgrade=True`: this reads an ARCHIVED config, which may predate the current version.
        # The strict policy refused, so seeding a workspace from any older campaign failed -- the
        # same mistake the retrigger path had. The archived file is not rewritten; the workspace
        # gets the upgraded shape, which is what someone editing it should see.
        retrigger.reconstruct_project(source_dir, project_dir,
                                      validate_config(load_config(str(vast_path), upgrade=True)))
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
        paths and workspace ids, not addresses, so a ``FileMeta`` built there carries no
        address — a 400 on every non-inline write over HTTP, and invisible to the
        in-process transport, which discards the result.
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

    # -- taking a campaign in -----------------------------------------------
    #
    # Archive grants are held in memory rather than in the workspace store's token table,
    # for two reasons: this channel must work on a service with no workspaces configured (the
    # store is what raises 501 there), and a grant outliving the process would buy nothing --
    # a restart mid-upload has already failed the upload.

    #: Where uploaded archives are staged before import. Under the results root so a
    #: multi-gigabyte tarball lands on the same volume it will be extracted into, rather
    #: than on whatever backs the system temp dir.
    _STAGING_DIRNAME = "_imports"

    def _staging_dir(self) -> Path:
        return self._campaigns_root() / self._STAGING_DIRNAME

    def campaign_tar_stream(self, campaign_id: str):
        """Tar this host's campaign directory straight into the response.

        The local counterpart of the cluster's object-store tar: same exclusions, same
        streaming, so a caller cannot tell which lane answered. ``_postproc/`` is left
        out with ``.cache`` -- it is postprocessing's staging, not part of the campaign.
        """
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        campaign_dir = self._campaign_dir(campaign_id)
        if not campaign_dir.is_dir():
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        return campaign_archive.iter_campaign_tar(
            str(campaign_dir),
            exclude=campaign_archive.DEFAULT_EXCLUDE | {"_postproc"})

    def create_archive_upload(self) -> UploadGrant:
        token = secrets.token_urlsafe(32)
        staged = self._staging_dir() / f"{token}.tar.gz"
        with self._archive_grants_lock:
            self._prune_archive_grants()
            self._archive_grants[token] = (time.time() + ARCHIVE_UPLOAD_TTL_SECONDS, staged)
        self._sweep_staged_archives()
        return UploadGrant(token=token, path=str(staged),
                           expires_in=ARCHIVE_UPLOAD_TTL_SECONDS)

    def _sweep_staged_archives(self) -> None:
        """Delete staged archives old enough that nothing can still be waiting on them.

        Every other path already cleans up after itself: an import deletes the copy it
        consumed, and a failed extraction deletes it too. What is left is the upload that was
        never imported -- a refused pre-flight, or a browser that went away between the PUT and
        the POST -- and those bytes are a campaign archive, so leaving them is leaving
        gigabytes per attempt on the results volume.

        Not deleted on refusal, deliberately: the answer to the commonest refusal (a campaign of
        that id is already here) is to import the *same* staged archive again with ``force``,
        which is exactly what the web UI's "Replace existing" does. Cleaning up on refusal would
        make that retry re-upload the whole thing.

        Age rather than liveness, for the same reason: a file being written has no grant left
        (the token is consumed when the PUT begins), so "unreferenced" cannot distinguish an
        upload in flight from an abandoned one. The window is generous because the thing it must
        never do is delete an upload that is still arriving.
        """
        cutoff = time.time() - STAGED_ARCHIVE_MAX_AGE_S
        staging = self._staging_dir()
        if not staging.is_dir():
            return
        for path in staging.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    logger.info("Removed abandoned staged archive %s", path.name)
            except OSError as e:
                # Housekeeping: a file that cannot be removed must not fail the upload the
                # caller actually asked for.
                logger.warning("Could not remove staged archive %s: %s", path, e)

    def _prune_archive_grants(self) -> None:
        """Drop expired grants. Called under the lock, on each new grant."""
        now = time.time()
        for token in [t for t, (expiry, _) in self._archive_grants.items() if expiry < now]:
            self._archive_grants.pop(token, None)

    def redeem_archive_upload(self, token: str) -> Path:
        """Consume *token* and return the path its bytes belong at.

        One-time: the grant is removed here, so a replayed PUT is a plain 404 rather than a
        second write to a path somebody else may already be importing.
        """
        with self._archive_grants_lock:
            self._prune_archive_grants()
            grant = self._archive_grants.pop(token, None)
        if grant is None:
            raise KeyError("no such upload grant (unknown, already used, or expired)")
        _, staged = grant
        staged.parent.mkdir(parents=True, exist_ok=True)
        return staged

    def list_share_archives(self) -> ShareListing:
        """Ask the share what it holds, with this service's own credentials.

        Nothing is cached and nothing is cross-referenced against what this service has:
        an archive whose campaign was cleaned up here is not an anomaly to be filtered out,
        it is the main reason import exists. Clients decide what to do with the overlap.

        Newest campaign first -- see the sort below for why that is read off the id.
        """
        from robovast.common.execution import \
            get_campaign_timestamp  # pylint: disable=import-outside-toplevel
        from robovast.execution.share_providers import \
            share_type_configured  # pylint: disable=import-outside-toplevel
        from robovast.execution.share_providers.naming import \
            parse_archive_name  # pylint: disable=import-outside-toplevel
        from robovast.service.interface import \
            ShareArchive  # pylint: disable=import-outside-toplevel

        if not share_type_configured():
            return ShareListing(configured=False)
        provider = self._share_provider()
        archives = []
        for object_name, size in provider.list_campaign_archives_with_size():
            parsed = parse_archive_name(os.path.basename(object_name))
            if parsed is None:
                continue
            campaign_id, variant = parsed
            archives.append(ShareArchive(
                campaign_id=campaign_id, variant=variant, object_name=object_name,
                size=size, url=provider.archive_url(object_name)))
        # Newest first, keyed on the timestamp inside the campaign id rather than on any
        # modification time: no provider reports one -- every
        # `list_campaign_archives_with_size` yields `(name, size)` -- and what a reader of
        # this listing wants is when the campaign ran, not when somebody last touched its
        # object. `parse_archive_name` has already accepted each id as a campaign id, so
        # the parser's fall-back to the whole name is unreachable here.
        #
        # Three keys because a campaign's two variants share the first two: without
        # `object_name` their order is whatever the provider happened to list, and two
        # calls could disagree about which of `raw`/`postprocessed` comes first.
        archives.sort(
            key=lambda a: (get_campaign_timestamp(a.campaign_id), a.campaign_id,
                           a.object_name),
            reverse=True)
        return ShareListing(configured=True, share_type=provider.SHARE_TYPE,
                            archives=archives)

    def import_campaign(self, request: ImportCampaignRequest) -> CampaignRef:
        """Take a campaign in, as a tracked background operation.

        Long by construction -- a share download, a multi-gigabyte extraction, and then
        postprocessing when what arrived was raw -- so it returns a handle rather than
        blocking a request until it is over. Everything that is knowable up front is
        settled here, synchronously, so the caller learns about a bad archive or a name
        collision as an error and not as a background failure five minutes later.
        """
        campaign_id, fetch, raw = self._resolve_import_source(request)
        note = ("this archive has no metric tables, so postprocessing runs once it lands -- "
                "the import is not over when the extraction is") if raw else ""

        # Pre-flight, the same shape as preflight_upload_to_share: the authoritative claim
        # (which deletes, under force) happens in the worker once the busy guard has
        # passed, so nothing here can destroy a campaign that is still being worked on.
        if self._campaign_is_here(campaign_id) and not request.force:
            raise RuntimeError(
                f"{campaign_id} is already here. Refusing to overwrite a campaign that is "
                f"already present -- its records are evidence. Import it again with force "
                f"to replace it.")

        def work(state):
            self._run_import(state, campaign_id, fetch, request)

        result = self._dispatch_background(campaign_id, phase=Phase.IMPORTING, work=work)
        if not result.ok:
            # The busy guard. A conflict, so it is raised rather than reported: this op
            # answers with a ref, and there is no ref for a campaign that was not started.
            raise RuntimeError(result.message)
        return CampaignRef(campaign_id=campaign_id, note=note)

    def _resolve_import_source(self, request: ImportCampaignRequest):
        """``(campaign_id, fetch, raw)`` for an import request; raise if it names nothing.

        *fetch* is a callable run inside the worker that puts the archive on this host and
        returns ``(path, owned)`` -- ``owned`` meaning this service staged the copy and may
        delete it afterwards. A path the caller named is never deleted: removing somebody's
        own file as a side effect of importing it is not something they can undo.

        Two things are known before any bytes move, on both sources: the campaign id -- from
        the archive's member list, or from the object's name -- which is what lets the
        campaign appear in the view at ``importing`` while it is still arriving; and whether
        it is *raw*, which is what lets the caller be told up front that postprocessing
        follows. Neither costs a read of the contents.
        """
        from robovast.execution.share_providers.naming import \
            RAW  # pylint: disable=import-outside-toplevel
        from robovast.service.ingest import \
            read_campaign_id  # pylint: disable=import-outside-toplevel

        if bool(request.archive_path) == bool(request.share_archive):
            raise ValueError(
                "an import names exactly one source: archive_path (a file on the service "
                "host) or share_archive (an archive on the configured share)")

        if request.archive_path:
            archive = Path(request.archive_path)
            if not archive.is_file():
                raise KeyError(f"no archive at {request.archive_path} on the service host")
            staged_root = self._staging_dir().resolve()
            try:
                owned = archive.resolve().parent == staged_root
            except OSError:
                owned = False
            return (read_campaign_id(archive), lambda _log: (archive, owned),
                    not _archive_has_metrics(archive))

        object_name, campaign_id, variant = self._find_share_archive(request.share_archive)

        def _fetch(log):
            provider = self._share_provider()
            dest = self._staging_dir() / f"{campaign_id}.tar.gz"
            dest.parent.mkdir(parents=True, exist_ok=True)
            log(f"downloading {object_name} from the {provider.SHARE_TYPE} share ...")
            provider.download_archive(object_name, str(dest),
                                      _throttled_transfer_log(log))
            log(f"downloaded {dest.stat().st_size} bytes")
            return dest, True

        return campaign_id, _fetch, variant == RAW

    def _share_provider(self):
        """The configured share provider, or a refusal naming what is missing."""
        from robovast.execution.share_providers import \
            load_provider_from_env  # pylint: disable=import-outside-toplevel
        provider = load_provider_from_env()
        if provider is None:
            raise RuntimeError(
                "this service has no share configured (ROBOVAST_SHARE_TYPE unset), so it "
                "cannot fetch an archive from one")
        return provider

    def _find_share_archive(self, wanted: str):
        """``(object_name, campaign_id, variant)`` for *wanted* on the share.

        *wanted* may be a campaign id or a full archive name; both resolve through the
        share's own listing, so a typo is an error naming what is actually there rather
        than a download of nothing.
        """
        from robovast.execution.share_providers.naming import \
            parse_archive_name  # pylint: disable=import-outside-toplevel

        provider = self._share_provider()
        available = []
        for object_name, _size in provider.list_campaign_archives_with_size():
            parsed = parse_archive_name(os.path.basename(object_name))
            if parsed is None:
                continue
            campaign_id, variant = parsed
            available.append(campaign_id)
            if wanted in (campaign_id, os.path.basename(object_name)):
                return object_name, campaign_id, variant
        raise KeyError(
            f"no archive for {wanted!r} on the {provider.SHARE_TYPE} share. "
            f"It holds: {', '.join(sorted(available)[:10]) or '(nothing)'}")

    def _run_import(self, state, campaign_id: str, fetch, request) -> None:
        """The import itself: claim, fetch, extract, register, and postprocess if raw.

        **A failed import is left in place, as a failed campaign.** Deleting its own
        directory -- on the reasoning that a tree which merely looks like a campaign would
        be listed by every client from then on -- buys nothing and costs the diagnosis: the
        campaign is listed anyway, since registering the tracked entry is what makes it
        visible during the import and that entry outlives the failure. Deleting the tree
        leaves a campaign listed as ``failed`` with *no* ``import.log``, no
        ``import.json``, and nothing to read. Worst of both.

        So the evidence stays where the evidence goes: in the campaign, next to the log
        that explains it. It behaves like any other failed campaign, including being
        removed by ``vast campaign delete``, and the archive is untouched, so a retry with
        force costs only the transfer.

        "In the campaign" has to mean *where clients read the campaign*. Publishing runs
        only on the success path, so on a lane whose durable home is an object store the log
        and the report would stay on a pod's scratch while ``list_files`` answered from the
        store -- the same undiagnosable failed campaign, reached by a different route.
        :meth:`_publish_failed_import` closes it: the account goes up, the scratch goes
        away, and the campaign reads the same on both lanes.
        """
        from robovast.client.logging_config import (  # pylint: disable=import-outside-toplevel
            add_campaign_log_handler, remove_campaign_log_handler)
        from robovast.service.ingest import (  # pylint: disable=import-outside-toplevel
            blocking_summary, claim_campaign_dir, extract_archive, ingest_campaign,
            read_campaign_id)
        from robovast.service.interface import \
            IngestReport  # pylint: disable=import-outside-toplevel

        if request.force:
            self._release_durable_campaign(campaign_id)
        target = claim_campaign_dir(self._campaigns_root(), campaign_id,
                                    force=request.force)
        handler = None
        try:
            handler = add_campaign_log_handler(str(target / "_execution" / "import.log"))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not open import.log for %s", campaign_id, exc_info=True)

        try:
            archive, owned = fetch(logger.info)
            # The archive must be the campaign it was asked for. On the share path the id
            # comes from the *object's name* while extraction lands the tree under whatever
            # name the tar carries, and nothing downstream compares them: a mismatch
            # extracts as some other campaign, ingests the empty directory claimed here,
            # and reports `config, layout` -- the archive's own symptom -- under an id that
            # is not the one that failed. Reading the id costs the tar's index, which the
            # upload path already pays (`_resolve_import_source`); this closes the other.
            inner = read_campaign_id(archive)
            if inner != campaign_id:
                raise RuntimeError(
                    f"the archive fetched for {campaign_id} holds campaign {inner!r}. "
                    f"Refusing to extract it: it would land as {inner!r} while "
                    f"{campaign_id!r} was ingested as an empty directory, and neither name "
                    f"would then mean what it says.")
            logger.info("extracting %s into %s ...", Path(archive).name,
                        self._campaigns_root())
            extract_archive(archive, self._campaigns_root(), remove_archive=owned)
            report = ingest_campaign(target, rebuild_store=request.rebuild_store)
            for name, stage in report["stages"].items():
                logger.info("  %-15s %-10s %s", name, stage["verdict"], stage["detail"])
            # Through the wire model rather than json.dumps of a dict: one definition of
            # what a stage report is, so what a client reads out of the file and what the
            # interface documents cannot drift apart.
            (target / "_execution" / "import.json").write_text(
                IngestReport.model_validate(report).model_dump_json(indent=2),
                encoding="utf-8")
            if not report["ok"]:
                raise RuntimeError(
                    f"{campaign_id} could not be ingested. {blocking_summary(report)}")
            logger.info("\u2713 imported %s", campaign_id)
        except Exception as e:  # noqa: BLE001 - recorded on the campaign, which is kept
            detail = failure_detail(e)
            logger.error("\u2717 import of %s failed: %s", campaign_id, e)
            logger.error("The campaign is kept as failed so this log survives; remove it "
                         "with 'vast campaign delete %s', or retry with force.", campaign_id)
            remove_campaign_log_handler(handler)
            handler = None
            # Durable, so the failure still reads as a failure after a service restart --
            # the tracked entry that carries it now lives only in this process.
            self._record_failed_import(target, detail)
            # ...and durable *where clients read*, which on a lane whose home is an object
            # store is not this disk. Publishing happens only on success, so without this
            # a failed import leaves import.log and import.json on a pod's scratch, where
            # `list_files` -- pointed at the store -- answers "no directory" for the very
            # campaign whose card is showing the failure: the "worst of both" this method's
            # docstring names, on the cluster lane only.
            self._publish_failed_import(campaign_id, target)
            state.update(error=detail)
            state.set_phase(Phase.FAILED)
            return
        finally:
            remove_campaign_log_handler(handler)

        self._postprocess_after_import(state, campaign_id, target)

    @staticmethod
    def _record_failed_import(target: Path, detail: str) -> None:
        """Write the terminal outcome of a failed import into the campaign it failed on.

        Best-effort: the import already failed, and failing to *record* that must not
        replace the reason with a second, less useful one. The log beside it is the
        account either way.
        """
        try:
            from robovast.execution.status_recovery import \
                write_execution_outcome  # pylint: disable=import-outside-toplevel
            write_execution_outcome(target, Status(phase=Phase.FAILED, error=detail))
        except Exception:  # pylint: disable=broad-except
            logger.warning("Could not record the failed import outcome for %s",
                           target.name, exc_info=True)

    def _postprocess_campaign(self, campaign_id: str, campaign_dir: Path, *,
                              force: bool = False, skip=(), state=None) -> tuple:
        """Run the campaign's own postprocessing pipeline; return ``(ok, message)``.

        One call for both callers -- the ``run_postprocessing`` retrigger and the chain an
        import starts -- so a raw archive taken in is postprocessed exactly the way asking
        for it later would be.

        ``campaign`` scopes the work to this campaign; with no ``vast_file`` the run reads
        the campaign's own ``_config/<name>.vast``. ``output_callback`` is what puts the
        step-by-step narrative ("[2/4] Executing: …", "✓ …") into whichever campaign log
        handler the caller opened. Without it those lines default to ``print`` and land on
        the service's stdout, so the phase file held only what modules logged themselves --
        the campaign log looked empty for the run you had just asked for.

        With *state*, those same lines also become the live ``stage`` marker -- see
        :func:`~robovast.execution.control_server.stage_output_callback`. Both callers have
        one, and both are watched from the campaign view, so a re-run narrates itself there
        exactly as an auto-chained run does; without it a retrigger was the case where the
        view showed ``postprocessing`` and nothing else for the whole run.
        """
        from robovast.execution.control_server import \
            stage_output_callback  # pylint: disable=import-outside-toplevel
        from robovast.results_processing.postprocessing import \
            run_postprocessing  # pylint: disable=import-outside-toplevel
        return run_postprocessing(
            results_dir=str(campaign_dir.parent), campaign=campaign_id,
            force=force, skip=list(skip),
            output_callback=stage_output_callback(state, logger.info))

    def _postprocess_after_import(self, state, campaign_id: str, target: Path) -> None:
        """Chain postprocessing when the imported campaign has none of its own.

        ``_execution/data.db`` is postprocessing's output and nothing else writes it, so its
        absence is the question already answered: this archive is a raw one -- what the
        share holds -- and a campaign with no metric tables is not one anybody can ask
        anything. A postprocessed archive is left exactly as it arrived.
        """
        from robovast.execution.status_recovery import (  # pylint: disable=import-outside-toplevel
            record_step_outcome, reconstruct_status_from_disk)

        if (target / "_execution" / "data.db").exists():
            logger.info("%s arrived postprocessed; nothing to compute", campaign_id)
        else:
            logger.info("%s arrived raw; running postprocessing", campaign_id)
            state.set_phase(Phase.POSTPROCESSING)
            ok, message = self._postprocess_campaign(campaign_id, target, state=state)
            record_step_outcome(target, postprocessing=(ok, message))
        # Read what the campaign says about itself BEFORE publishing, because publishing is
        # where the tree stops being readable: on a lane whose durable home is elsewhere,
        # `_publish_imported_campaign` drops the pod's copy once it is in the object store
        # (a multi-gigabyte campaign left on scratch is how a service pod fills its disk).
        # Reading after it reconstructs from a directory that is gone, which yields zeros --
        # exactly the empty report this is here to prevent, and invisible to a test on the
        # local lane, where publishing is a no-op and the tree survives either way.
        #
        # The tracked entry an import runs under is constructed EMPTY -- it exists to make
        # the campaign visible while its bytes arrive -- and it shadows the durable
        # ``outcome.json`` for as long as it lives. Without this an import ends reporting
        # ``0 runs`` and ``postprocessed: false`` over a campaign whose every table is
        # present, and the status advises running postprocessing that would recompute all of
        # it. Both arrival paths need it: the raw one records only the postprocessing
        # verdict, never the run tally, and the postprocessed one records nothing at all --
        # having nothing to *compute* is not having nothing to *report*.
        status = reconstruct_status_from_disk(target)
        # After postprocessing rather than before: this is where the campaign actually
        # becomes durable, and publishing first would publish a campaign without the
        # tables just computed.
        self._publish_imported_campaign(campaign_id, target)
        state.update(mode=status.mode, runs=status.runs,
                     batches_done=status.batches_done,
                     best_objective=status.best_objective,
                     postprocessed=status.postprocessed,
                     postprocessing_error=status.postprocessing_error,
                     share_error=status.share_error)
        state.set_phase(Phase.FINISHED)

    # -- the four things an import means something different by, per lane ------
    #
    # Local disk is both the working area and the durable home, so all four are trivial
    # here. A lane whose home is an object store overrides them; nothing else in the
    # import differs, which is why they are four small questions rather than a second
    # copy of the sequence.

    def _campaign_is_here(self, campaign_id: str) -> bool:
        """Whether importing this id would replace something. Asked before any transfer."""
        return self._campaign_dir(campaign_id).exists()

    def _release_durable_campaign(self, campaign_id: str) -> None:
        """Under ``force``: drop the durable copy this import is about to replace.

        Nothing to do locally — the directory *is* the durable copy, and
        ``claim_campaign_dir`` removes it.
        """

    def _publish_imported_campaign(self, campaign_id: str, target: Path) -> None:
        """Make the imported campaign durable. Locally it already is."""

    def _publish_failed_import(self, campaign_id: str, target: Path) -> None:
        """Make a FAILED import's account of itself durable. Locally it already is.

        Separate from :meth:`_publish_imported_campaign` rather than a flag on it, because
        the two publish different things for different reasons: a successful import
        publishes the *campaign*, this publishes only the few kilobytes that say why there
        is no campaign. A failed import can be gigabytes of a tree nobody can use, and
        uploading that to explain a missing ``_config/`` would spend a full transfer on a
        diagnosis.
        """

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
        # each answer that — and asking the daemon means shelling out with a timeout,
        # which is the last thing this call should ever wait on. `_api_server_url` in the
        # cluster lane's version() refuses to dial for the same reason.
        return VersionInfo(robovast_version=_robovast_version(),
                           code_revision=_code_revision(),
                           package_version=_package_version(),
                           built_at=_build_date(), backend="docker",
                           can_build_images=True,
                           results_root=str(self._campaigns_root()),
                           sources_root=str(self.store.registry.root),
                           web_base=self._declared_web_base())

    def _declared_web_base(self) -> str:
        """The origin this deployment declares for its callers, or ``""``.

        One input, from whoever knows: ``setup`` bakes it from the Ingress for a deployed
        service (which is given no RBAC to read its own), and ``serve`` fills it in from
        the address it bound for a service started by hand. Both arrive the same way, so
        there is nothing to reconcile here and no lane needs an override -- the cluster
        lane runs both in-pod and off-cluster through a port-forward, and a per-lane
        version of this would have had to remember not to blank the second case.

        ``""`` when nobody named one: unpublished, or bound to a wildcard where which
        address a caller used is not knowable from here.

        The literal rather than an import, like ``default_workspaces_root`` reads
        ``ROBOVAST_WORKSPACES_ROOT``: the writers name it
        (``robovast.service.app.PUBLIC_URL_ENV`` and the cluster lane's constant of the
        same name), and importing either from here would drag a serving layer, or a lane,
        into the cheapest call in the interface.
        """
        return os.environ.get("ROBOVAST_PUBLIC_URL", "").strip()

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
        return usage.model_copy(update={
            "exec_container": self._exec_container_state(),
            "query_containers": self._query_container_states()})

    def upgrade_info(self) -> UpgradeInfo:
        """The live campaigns, and a refusal: there is no Deployment here to roll.

        Split from the cluster answer the way :meth:`version` is -- the campaign list is
        lane-neutral and belongs here, and the lane below fills in what only it knows. The
        refusal names how this deployment *is* updated rather than reporting a capability
        it does not have: a local service is however it was installed and started.
        """
        listed = self.list_campaigns(ListCampaignsRequest(limit=100, offset=0)).campaigns
        return UpgradeInfo(
            supported=False,
            unsupported_reason=(
                "this service is not a Kubernetes Deployment, so it has nothing to roll. "
                "Update it the way it was installed and restart it."),
            active_campaigns=[c for c in listed if not is_terminal(c.phase)])

    def upgrade_service(self, force: bool = False) -> ActionResult:
        """Refuse: see :meth:`upgrade_info`.

        ``ValueError`` -> 400. Not the 409 the live-campaign refusal uses: that one is a
        conflict the caller can resolve and retry, this one is a request that does not
        apply to this deployment at all.
        """
        del force  # a refusal about the lane; forcing does not make a lane something else
        raise ValueError(self.upgrade_info().unsupported_reason)

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

    def _query_container_states(self) -> dict:
        """The held query containers by slot, ``{}`` when there are none.

        Separate from the one above because they are separate occupancy: a lane holding
        three query containers while reporting only the caller's would look like it had
        capacity it does not have. Same no-side-effects rule.
        """
        from robovast.service.container_exec import SLOT_USER
        if self._exec_mgr is None:
            return {}
        try:
            return {slot: state for slot, state in self._exec_mgr.states().items()
                    if slot != SLOT_USER}
        except Exception as e:  # noqa: BLE001 - capacity must still be answerable
            logger.debug("could not read query container states: %s", e)
            return {}

    def _compute_resource_usage(self) -> ResourceUsage:
        """Local host capacity + live utilization via ``psutil``.

        ``cpu_percent(interval=None)`` is non-blocking — it averages CPU load since
        the previous call rather than sleeping per request. The first reading after
        the process starts is ``0.0`` (no prior sample); the TTL cache means that is
        replaced by a real value on the next window. Overridden by
        :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`.

        This lane fills the ``*_measured`` fields and leaves ``*_reserved`` ``None``: it
        starts run containers without cpu/memory limits, one at a time, so it has a
        measurement and no reservation. See :class:`ResourceUsage`.
        """
        import psutil  # pylint: disable=import-outside-toplevel
        vm = psutil.virtual_memory()
        cores = psutil.cpu_count(logical=True)
        # Read ONCE and reused below. `cpu_percent(interval=None)` is stateful: it averages
        # since the previous call, so asking twice in one reading answers the second with
        # roughly zero -- `cpu_used` and `cpu_measured` are the same reading, not two.
        cpu_measured = cores * psutil.cpu_percent(interval=None) / 100.0
        jobs_running, jobs_pending = self._scenario_job_tally()
        disk, disk_unavailable = self._disk_space()
        return ResourceUsage(
            backend="docker",
            cpu_capacity=float(cores),
            cpu_used=cpu_measured,
            memory_capacity_bytes=vm.total,
            memory_used_bytes=vm.used,
            # This lane MEASURES, and reserves nothing: run containers are started without
            # cpu or memory limits and one at a time, so there is no reservation to report.
            # `cpu_reserved` stays None rather than echoing the measurement, which would
            # label consumption as a commitment -- the chart then draws one series here,
            # which is the truth about this lane and not missing data.
            cpu_measured=cpu_measured,
            memory_measured_bytes=vm.used,
            parallel_runs=False,   # Docker backend is single-flight: runs are sequential
            jobs_running=jobs_running,
            jobs_pending=jobs_pending,
            disk=disk,
            disk_unavailable=disk_unavailable,
            # `store` stays None: this lane's results store IS the filesystem `disk`
            # already reports, and a second identical meter would say nothing.
        )

    def _disk_space(self) -> "tuple[Optional[DiskSpace], Optional[str]]":
        """This host's results filesystem, or the reason it could not be read.

        The filesystem holding :meth:`_campaigns_root`, not ``/``: a campaign writes its
        rosbags and CSVs there, and where that is a separate mount -- a data disk, an NFS
        export -- ``/`` can look comfortable while the disk the next campaign needs is
        full. (The Docker graph dir is the other thing that fills, from image pulls; it is
        normally the same device, and a second disk would need a second meter.)

        Resolved to the nearest existing ancestor because ``_campaigns_root`` is a pure
        path resolver -- the directory is materialized lazily on the first run, so a
        service that has never run a campaign would otherwise fail to read the very disk it
        is about to write to. Same filesystem either way, unless the missing component is
        itself an unmounted mountpoint.
        """
        import psutil  # pylint: disable=import-outside-toplevel
        path = self._campaigns_root()
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            usage = psutil.disk_usage(str(path))
        except OSError as e:
            logger.debug("could not read disk usage for %s: %s", path, e)
            return None, f"could not read the results filesystem: {e}"
        return DiskSpace(capacity_bytes=usage.total, used_bytes=usage.used), None

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

        A context manager, so anything thread-scoped is established where the composition
        that reads it runs, and torn down when the campaign ends. Today that is only the
        aux-container runner, which is why this delegates: a campaign is one *span* over
        which a lane provides one, not the only span. Anything genuinely per-campaign
        belongs here rather than in :meth:`_aux_runner_context`, which preview also enters.
        """
        return self._aux_runner_context(campaign_id, project)

    def _aux_runner_context(self, tag: str, project, *, hold: bool = False):
        """How this lane provides a variation's auxiliary container, for one span.

        A context manager: entered in the thread that composes, because the factory it
        installs is a ContextVar and must be scoped to exactly that composition. *tag*
        names the span (a campaign id, or a digest identifying a previewed project) so a
        lane that creates something per span can name it.

        *hold* is who owns the container's death. False — a campaign — means the span does:
        it is torn down when the run ends, which is also what makes per-campaign cleanup
        able to find it. True — an interactive caller such as ``preview_configurations`` —
        means the container outlives the span and is reaped on idleness instead, because an
        authoring loop composes the same file repeatedly and would otherwise pay a cold
        start every time. An unbounded span is the one that needs a reaper.

        No-op locally, and deliberately: with no factory installed
        ``_make_container_runner`` falls back to an ephemeral ``docker run`` on the service
        host, which is what a local service — and the CLI, which has no transport at all —
        already wants, and where holding would buy about a second. The cluster lane
        overrides this, having no ``docker`` in the pod and a pull to amortize.
        """
        del tag, project, hold
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
        self._admit_image_provenance(target, request)
        return self._launch_campaign(request, target)

    def materialize_retrigger_workspace(self, campaign_id: str,
                                        workspace_name: str) -> WorkOrder:
        """See the interface.

        Built on ``create_workspace(from_campaign=...)`` rather than beside it: that path already
        reconstructs a campaign's project correctly -- and knows why a directory copy is wrong,
        since ``_config/`` archives the scenario at its basename while the config may declare a
        subdirectory path. What this adds is the migration afterwards, and the markers it leaves.
        """
        import yaml

        from robovast.common.migrations import (SUPPORTED_CONFIG_VERSION, UnmigratableConfig,
                                                find_migration_markers, upgrade_config_file)
        from robovast.service.retrigger import _read_vast

        info = self.create_workspace(CreateWorkspaceRequest(name=workspace_name,
                                                            from_campaign=campaign_id))
        project = self._resolve_project(info.workspace_id, "")
        staged = Path(project.config_path)

        reached, capability = SUPPORTED_CONFIG_VERSION, ""
        try:
            upgrade_config_file(staged, write=True)
        except UnmigratableConfig as e:
            reached, capability = e.reached, e.capability
            if e.partial is not None:
                # The step's own partial output. Written over the seeded copy, which loses comments
                # on keys the step rebuilt -- ruamel cannot carry those through a plain dict. Worth
                # the loss: without the partial the person gets nothing to work from.
                with open(staged, "w", encoding="utf-8") as handle:
                    yaml.dump(e.partial, handle, default_flow_style=False, sort_keys=False)

        markers = find_migration_markers(_read_vast(staged))
        logger.warning(
            "materialised %s as work order in workspace %s: %d unresolved marker(s). It will not "
            "validate until each is resolved, which is deliberate.",
            campaign_id, info.workspace_id, len(markers))
        return WorkOrder(
            workspace_id=info.workspace_id, config_path=str(staged),
            reached=reached, capability=capability,
            markers=[MigrationMarker(path=where, reason=reason) for where, reason in markers])

    def check_retrigger(self, campaign_id: str) -> RetriggerReport:
        """See the interface. A thin adapter over :func:`robovast.service.retrigger.check`.

        Same split as :meth:`retrigger_campaign`: the module decides everything about the
        source, and what belongs here is only which directory this lane reads it from.
        """
        from robovast.service import retrigger

        report = retrigger.check(self._retrigger_source_dir(campaign_id), campaign_id)
        return RetriggerReport(
            campaign_id=report["campaign_id"],
            runnable=report["runnable"],
            blocking=report["blocking"],
            axes={
                name: RetriggerAxis(
                    verdict=axis["verdict"], detail=axis["detail"],
                    # Everything beyond the verdict and its explanation is the axis's own
                    # structured findings, and they differ per axis -- so they travel as data
                    # rather than being flattened into fields most axes would leave empty.
                    data={k: v for k, v in axis.items() if k not in ("verdict", "detail")})
                for name, axis in report["axes"].items()
            })

    def retrigger_campaign(self, campaign_id: str) -> CampaignRef:
        """Launch a new campaign from *campaign_id*'s own records (see the interface).

        A thin orchestrator: :mod:`robovast.service.retrigger` decides everything about the
        source, and :meth:`_launch_campaign` runs it. What belongs here is only the ordering
        that needs the transport — which directory this lane reads the source from, the
        single-flight guard, and making sure a refusal leaves nothing staged behind.
        """
        from robovast.execution.notify import Notifier
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
            ref = self._launch_campaign(plan.request, WorkspaceTarget(
                config_path=plan.config_path,
                origin=self._retrigger_origin(campaign_id),
                materialize=plan.materialize,
                discard=plan.discard,
                pinned_images=plan.pinned_images))
        except BaseException:
            plan.discard()
            raise
        # Told on the SOURCE campaign's topic: a watcher following the campaign that was
        # re-run is the one who cannot otherwise learn which id the re-run got. The new
        # campaign announces its own start; this is not that message, and not a terminal
        # one -- the source is unmodified. Best-effort like every other send.
        Notifier.from_env(campaign_id).retriggered(ref.campaign_id)
        return ref

    def _retrigger_origin(self, source_id: str) -> CampaignOrigin:
        """The origin to record for a re-run of *source_id*.

        Built here rather than in :mod:`robovast.service.retrigger`, which deliberately
        does not import the service interface.

        The workspace fields are **copied from the source's own origin**, so they keep
        naming the workspace the configuration came from originally -- and a re-run of a
        re-run inherits it transitively, because the parent's record already holds it.
        Copied rather than resolved by walking ``from_campaign`` later, because the listing
        is paginated (a reader may not hold the parent at all) and because a parent is
        routinely deleted -- lineage that evaporates with it is lineage nobody can rely on.

        None of this is a link: the re-run runs from the source's frozen ``_config/``
        (:mod:`robovast.service.retrigger` says why), never from the workspace named here,
        which may be long gone. A source that recorded no origin leaves the workspace
        fields empty; ``from_campaign`` is still the answer to where this one came from.
        """
        parent = self._origin_for(source_id)
        return CampaignOrigin(
            kind=OriginKind.RETRIGGER,
            from_campaign=source_id,
            workspace_id=parent.workspace_id if parent else "",
            workspace_name=parent.workspace_name if parent else "",
            config_path=parent.config_path if parent else "")

    def _admit_image_provenance(self, target, request: CreateCampaignRequest) -> None:
        """Refuse to launch a campaign whose image nobody could later identify.

        Here rather than in :meth:`_launch_campaign`, and that placement is the whole point:
        ``_launch_campaign`` is shared with the retrigger path, and a *recorded* campaign is a
        different question. Its image digest already is provenance for "these bytes ran", so
        refusing it would make exactly the archived campaigns this must keep re-runnable
        un-re-runnable. The rule belongs to **authoring a new campaign**, which is this method.

        The same classifier the validator uses, so a config cannot validate and then refuse to
        launch.
        """
        from robovast.common.common import load_config
        from robovast.common.execution import opaque_image_containers

        if request.allow_opaque_image:
            logger.warning(
                "launching with allow_opaque_image: an image in this campaign cannot be "
                "identified, so its results will not say what ran. The exemption is recorded.")
            return
        try:
            raw = load_config(target.config_path)
        except Exception:  # noqa: BLE001 - a broken config is the validator's problem, not this one
            return
        opaque = opaque_image_containers(raw.get("execution") or {})
        if not opaque:
            return
        detail = "\n".join(why for _name, why in opaque)
        raise ValueError(
            f"refusing to launch: {len(opaque)} container(s) name an image that could not be "
            f"identified later.\n\n{detail}\n\n"
            f"Run 'vast configuration validate' to see this alongside anything else, or pass "
            f"allow_opaque_image to launch anyway -- the exemption is recorded on the campaign "
            f"so it is visible to whoever reads the results.")

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
        campaign_id = target.campaign_id or campaign_id_for(
            campaign_config, request.campaign_name or None)
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
        # Waived for a re-entry, which is the one caller that means to land on an
        # existing campaign: ``target.campaign_id`` names the campaign it is resuming,
        # and its directory holds what the earlier life already produced.
        campaign_root = os.path.join(results_dir, campaign_id)
        if target.campaign_id is None and os.path.exists(campaign_root):
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
                               created_by=request.created_by,
                               origin=target.origin)
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
                    # resolve_robovast_image). A failed build is not a failed *request*:
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
                # Re-record the launch, now that the symbolic image refs have become
                # concrete ones. Written here rather than merged in later because this is
                # the last moment before the campaign starts spending compute, and the
                # record is what a re-launch after a restart pins its images from -- a
                # re-resolve would silently pick up a base that moved in the meantime.
                self._record_launch(campaign_id, results_dir, request,
                                    images=options.images)
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
                            created_by=request.created_by, origin=target.origin)
                    else:
                        run_batch_campaign(
                            target.config_path, campaign_config, results_dir, runs,
                            config_filter=config_filter, backend=backend,
                            options=options, campaign_id=campaign_id, state=state,
                            notifier=notifier, description=request.description,
                            created_by=request.created_by, origin=target.origin)
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
                # After it, not before: `end_campaign` is what publishes the terminal
                # phase, so this is the first point at which the campaign has a finish
                # time to record anywhere.
                self._on_campaign_finished(campaign_id, state)

        thread = threading.Thread(
            target=_worker, name=f"robovast-{campaign_id}", daemon=True)
        entry.thread = thread
        thread.start()
        logger.info("Started campaign %s (search=%s)", campaign_id, is_search)
        # Joined rather than first-wins: two independent advisories can both apply to one
        # launch, and dropping the second would make it depend on the first being absent.
        notes = [n for n in (_show_gui_note(request, raw_config),
                             _no_timeout_note(raw_config)) if n]
        return CampaignRef(campaign_id=campaign_id, note=" ".join(notes))

    # -- image builds -------------------------------------------------------

    @property
    def _images(self):
        """This lane's image store — where its built experiment images live.

        The one member a lane overrides about images, and it is a **factory, not
        behavior**: everything that consumes the store is written once, here, so a lane
        cannot answer an image question wrongly by forgetting to override the method that
        asks it. Without the seam, :meth:`_exec_image` asks the local docker daemon on a
        lane whose images live in a registry, inside a pod with no docker at all, and
        reports every built image as unbuilt.
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
                                 getattr(campaign_config, 'plugins', None),
                                 position="append")
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
                # The context size and cache ref go in the header because they are the
                # two costs BuildKit's own output never names: a build whose every vertex
                # says CACHED can still spend minutes on them.
                detail = ""
                if getattr(status, "context_bytes", 0):
                    detail += f", context {status.context_bytes / 1e6:.1f} MB"
                if getattr(status, "cache_ref", ""):
                    detail += f", layer cache {status.cache_ref}"
                self._append_build_log(
                    log_path,
                    f"waiting for image {status.tag or '?'} (build {build_id}){detail}\n")
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
                "nothing to build: no container adds system_packages, "
                "python_packages or ros_packages, so every image is used as declared")
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
        """Remove every exec container left behind by a previous service process.

        Through the lane's own sweep rather than removing one fixed name: the user slot
        has a fixed name, but a query container's carries a hash of the identity it was
        started for, and nothing persists those across a restart. Removing only the fixed
        one left those holding memory with nothing able to name them.
        """
        try:
            removed = self._exec_lane().sweep_held()
            if removed:
                logger.info("removed %d stray exec container(s) from a previous run: %s",
                            len(removed), ", ".join(removed))
        except Exception as e:  # noqa: BLE001 - a broken docker must not break startup
            logger.debug("could not check for stray exec containers: %s", e)

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
        from robovast.service.container_exec import (SLOT_USER, query_slot, result_from,
                                                     stage, validate)
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
        query = bool(getattr(request, "query", False))
        out = self._exec_manager.run(spec, limit_s,
                                    keep_alive=request.keep_alive,
                                    identity=identity, query=query)
        # Report the slot this call actually used. Reporting the user's for a query would
        # tell a caller their container had been replaced when it had not been touched.
        slot = query_slot(identity) if query else SLOT_USER
        return result_from(out, spec=spec, limit_s=limit_s,
                           limit_source=limit_source,
                           duration_s=time.monotonic() - started,
                           container=self._exec_manager.state(slot))

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
        from robovast.common.simulators import apply_backend
        from robovast.service.image_store import ImageRef

        # Validate rather than reading the raw mapping: the build specs come off the
        # *model*, so handing this path a plain dict yields "no build section" for every
        # project that has one.
        campaign_config = validate_config(load_config(vast_file))
        # Apply the simulator backend BEFORE planning, exactly as image_build,
        # campaign_data and config_generation do. Without it the `simulation` block holds
        # only the backend's own keys -- no image, no command -- so plan_containers reads
        # the simulator as *not* separate, the `simulation` role resolves to the scenario
        # container, and an exec asking for it silently landed in the base image with no
        # simulator in it. `get_world_body_tree` runs `roqsim scenes describe` there and
        # could therefore never have worked on a project whose roqsim comes from the image
        # family. The stepped shape is unaffected: there the simulator genuinely *is* the
        # scenario container, and the backend says so.
        execution = apply_backend(campaign_config.execution.model_dump(),
                                  os.path.dirname(os.path.abspath(vast_file)))
        plan = plan_containers(execution)
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
        # Every test of this verb uses a fake transport, so nothing here is exercised by
        # them: a stale name in this body raises NameError in production only.
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
            # Each step's line also becomes the live ``stage`` marker: this phase has no run
            # counter, so its own narration is the only thing that separates a long step from
            # a stuck one for a reader watching the campaign view.
            from robovast.execution.control_server import stage_output_callback
            ok, message = run_postprocessing(
                results_dir=results_dir, campaign=campaign_id,
                output_callback=stage_output_callback(state, logger.info))
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
            # Re-write the durable outcome to reflect the final postprocessing state: the
            # record _finish_campaign writes is made while postprocessing is still pending.
            # Success or failure, one record then carries the accurate
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

    def _record_launch(self, campaign_id, results_dir, request, images=None):
        """Persist how this campaign was asked for, to ``_execution/launch.yaml``.

        The counterpart of :meth:`_record_outcome` at the other end of the campaign: what was
        requested, rather than how it ended. ``config_filter`` in particular is recorded
        **nowhere else** — it is consumed inside ``build_campaign_data`` and then gone — so
        without this "was this the full sweep or a one-config pilot?" cannot be answered about
        any campaign in the results root, by a retrigger or by a human.

        Called at the top of the worker so it lands before anything that can fail, for the
        same reason :meth:`_on_campaign_started` is there, and **again** once the launch has
        resolved its images — see ``write_launch_record``'s ``images``. Never fatal: a
        campaign that runs correctly must not be failed by an unwritable record.

        Publishing follows writing, in the same call: a record that exists only on this disk
        is missing from precisely the campaigns worth looking at on a lane whose disk is
        scratch, which is the defect :meth:`_publish_campaign_records` exists to close.
        """
        from robovast.common.campaign_data import write_launch_record
        campaign_root = Path(results_dir) / campaign_id
        try:
            write_launch_record(campaign_root, request, images=images)
        except OSError as e:
            logger.warning("Could not write launch.yaml for %s: %s", campaign_id, e)
            return
        self._publish_campaign_records(campaign_id, campaign_root)

    def _publish_campaign_records(self, campaign_id: str, campaign_root: Path) -> None:
        """Put the campaign's records where they survive this process. No-op here.

        A local campaign's durable home *is* this directory, so there is nowhere else to put
        them. :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`
        overrides it: there the driver's disk is scratch, and a record left on it is lost by
        the next restart — which is exactly the moment someone comes looking.

        The service's half of a pair. This one publishes what the *service* writes before a
        controller exists (``launch.yaml``); ``ExecutionBackend.publish_records`` publishes
        what the *controller* writes (``campaign.db``), because by then the backend is the
        only route to the store. Neither can do the other's job: at this point there is no
        backend, and at that point there is no service.
        """

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
            snap = self._derive_postprocessed(campaign_id, entry.state.snapshot())
            return self._attach_health(campaign_id, snap)
        # Not tracked in this process — reconstruct from disk (past campaign). Nothing to ask:
        # a campaign this process does not drive has no running job here to look into.
        return self._status_from_disk(campaign_id)

    def get_search_history(self, campaign_id: str) -> SearchHistory:
        """A search's per-batch objective trajectory, from its ``campaign.db``.

        Resolved through :meth:`_record_dir`, which is what makes one implementation serve
        every case: a campaign this process is driving answers from the store its controller is
        writing right now, and any other from its durable records. That is also why this reads
        the store rather than going through the SQL query endpoint — on the cluster lane that
        endpoint materialises a snapshot the campaign publishes only when it *finishes*, so a
        query there returns nothing at all for the running search this exists to show.
        """
        from robovast.common.store import read_batch_objectives
        history = read_batch_objectives(self._record_dir(campaign_id))
        if history is None:
            return SearchHistory(unavailable="no_store")
        return SearchHistory(**history)

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
        uses — :func:`~robovast.common.campaign_data.campaign_has_derived_data`, which both
        call so they cannot disagree; what ``_postprocess`` records is untouched, and so is
        the archive decision that reads it. Best-effort on the cluster lane in exactly the
        way the recovery path already is: ``data.db`` is not among ``_RECORD_OBJECTS``, so a
        campaign whose derived data was never fetched here answers the same as before.

        Two states are deliberately *not* promoted, both of which the plain existence of
        ``data.db`` would promote — this is the live path, so it sees them where the
        recovery path (which runs only once nothing is driving the campaign) mostly cannot:

        * a build **in progress**. The file appears at 0%, so a campaign would spend the whole
          of a twenty-minute ``data.db`` build reporting that its results were ready. The web
          UI gates its Results views on exactly this flag, so it would offer them over a
          database being appended to.
        * a build that **failed**. ``postprocessing_error`` sets the flag False on purpose;
          promoting it back would hide the error behind "results are ready".
        """
        if snap.postprocessed or snap.postprocessing_error:
            return snap
        from robovast.common.campaign_data import campaign_has_derived_data
        try:
            if campaign_has_derived_data(self._record_dir(campaign_id)):
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
        ``logs/system_<name>.log`` beside the main container's ``logs/system.log``. Reading
        only the latter shows scenario-execution and neither the simulator nor nav2 -- the
        two whose output explains a failed run. See
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
        from robovast.common.campaign_data import KIND_KILLED, record_intervention
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
        record_intervention(campaign_dir, kind=KIND_KILLED, job_dir=job_dir, job_name=job_name,
                            source=source, detail=reason, runs=(job_name,))
        self._kill_scenario_container()
        return ActionResult(
            ok=True,
            message=(f"killed job {job.display_name or job_name}; the campaign continues "
                     f"with its remaining runs and this run is recorded as 'killed'"))

    def _campaign_execution(self, campaign_id: str) -> dict:
        """The ``execution`` block of this campaign's own frozen configuration.

        One reader, in :func:`~robovast.common.results_utils.campaign_execution`, which is also
        what the scene cache asks: two readers of one archived block are free to disagree, and the
        one that lived here disagreed by handing back a pydantic model where a mapping was wanted.

        Raises for an unreadable config, which :meth:`_read_health` turns into a stated reason. It
        must stay a raise rather than an empty block: an empty one is indistinguishable from a
        campaign whose simulator cannot report on itself, and that reads as "nothing is wrong".
        """
        from robovast.common.results_utils import campaign_execution
        return campaign_execution(Path(self._retrigger_source_dir(campaign_id)))

    def get_job_state(self, campaign_id: str, job_name: str) -> "JobState":
        """What a running job is doing, from the run's own tools by fixed commands.

        Two readers, asked independently on purpose: the scenario's tree is there whatever the
        simulator is, so a campaign whose simulator cannot report on itself still gets the more
        useful half. Coupling them would have made the absence of one hide the other.

        Neither parses another component's file format. The tool that owns each record reads it
        and prints JSON, so a record can be reshaped by its owner without breaking this.

        The health half is served from whatever the status path last pulled (see
        :meth:`_read_health`), so an agent asking is never charged for a check a poll has already
        paid for. The scenario's tree is always read fresh: it is the expensive half -- a fold over
        every recorded transition rather than a tail -- which is exactly why it is asked for here
        and never polled.

        **Each read is asked of the container that owns it**, which is why the target is resolved
        per role and not per job: the scenario runs in the scenario container always, while a
        simulator with a container of its own answers only there. Sending both to one target sent
        ``roqsim health`` into a container with no roqsim in it for every ROS-shape campaign.

        Lane-independent by construction: everything that decides *what* is asked is here, and only
        :meth:`_job_state_target` differs between a local container and a job's pod.
        """
        from robovast.service.interface import JobState

        job = self._require_running_job(campaign_id, job_name)
        state = JobState(job_name=job_name, status=job.status)
        try:
            target, run_dir = self._job_state_target(campaign_id, job_name, SCENARIO_CONTAINER)
            run_dir, state.run = self._job_live_run(campaign_id, job_name, target, run_dir)
            job_dir = self._job_output_dir(campaign_id, job_name, run_dir)
        except Exception as err:  # noqa: BLE001 - a job between scheduling and running, or gone
            state.unavailable.append(str(err))
            return state
        self._read_scenario_state(state, target, run_dir)
        self._read_resources(state, target, job_dir)
        document, reason = self._read_health(campaign_id, job_name, job_dir, run_dir)
        if reason:
            state.unavailable.append(reason)
        state.simulator = document
        return state

    def _job_output_dir(self, campaign_id: str, job_name: str, run_dir: str) -> str:
        """Where this job writes its **job-level** artifacts, which is not where its runs are.

        The two are different subtrees and conflating them is why two reads failed at once:
        ``behaviors.jsonl`` and the simulator's records are per RUN
        (``<config>/<run>/``), while the logs, the sysinfo and the resource monitor's CSVs are
        per JOB, under ``_jobs/<batch>/job-<idx>/`` -- as
        :func:`~robovast.common.execution.job_artifact_dir` says outright: *"never into the run
        dir, so ``<config>/<run>/logs/`` stays empty and reading there yields a silently blank
        log."* One path for both meant whichever read matched the path worked and the other
        reported the run had written nothing.

        Resolved through the manifest, which is written before the first job starts, so a RUNNING
        job resolves. Falls back to *run_dir* when it cannot be resolved -- the read that follows
        then reports finding nothing, which is the honest outcome and names the directory it
        looked in.
        """
        from robovast.common.execution import job_artifact_dir
        try:
            rel = job_artifact_dir(self._campaigns_root() / campaign_id, job_name)
        except Exception as err:  # noqa: BLE001 - the documented startup race, among others
            logger.debug("no job artifact dir for %s of %s: %s", job_name, campaign_id, err)
            return run_dir
        rel = str(rel).strip("/")
        return f"/out/{rel}" if rel else run_dir

    def _job_live_run(self, campaign_id: str, job_name: str, target, run_dir: str) -> tuple:
        """``(run_dir, run_key)`` for the run this job is working on **right now**.

        Locally a job *is* one run, so this is what the target already said. The hook exists for
        the cluster, where a Job may pack several runs -- and the packer runs them
        **sequentially** (see ``KubernetesBackend``), so at any instant exactly one of them is
        live and "where is this job" has a single right answer. Which is the point: pointing the
        readers at the Job's whole ``/out`` let each of them pick a run for itself, silently, and
        a caller could not tell which run it had been told about.
        """
        del campaign_id, target, run_dir
        return f"/out/{job_name}", job_name

    # -- what the running jobs' simulators say about themselves ---------------------------------
    #
    # The service *pulls*, with a command it chose itself, so nothing has to run in a container
    # and nothing is emitted anywhere. That is what makes this free for a campaign nobody is
    # debugging: an unwatched campaign is never asked, because only a status read asks.

    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        """``(target, run_dir)`` for one running job's *role* — the lane-specific part of a read.

        Locally *job_name* **is** the run key (``<config>/<run>``), which is also where the run
        writes inside the container: ``/out/<config>/<run>``, the same path both lanes mount.
        Derived from the run rather than read from ``RUN_OUTPUT_DIR``, which the backends set only
        for a job that is exactly one run and so is absent from a packed one.

        The run dir does not depend on the role: ``/out`` is mounted into every container of a run,
        which is what lets a sidecar be asked about the run being written under it.
        """
        return self._job_container(role, campaign_id), f"/out/{job_name}"

    def _health_targets(self, campaign_id: str) -> list:
        """``(job_name, job_dir, run_dir)`` for every job of this campaign running right now.

        A different question from :meth:`_require_running_job`'s, which is why it is a different
        method: that one enforces "this named job is running" for a caller who named one, this one
        asks "which jobs are there to ask". Local Docker is sequential, so at most one.

        No target: the health read resolves its own, because the container it belongs in is the
        simulator's and not the job's.
        """
        out = []
        for job in self.list_jobs(campaign_id).jobs:
            if job.status != "running":
                continue
            try:
                target, run_dir = self._job_state_target(
                    campaign_id, job.job_name, SCENARIO_CONTAINER)
                run_dir, _run = self._job_live_run(campaign_id, job.job_name, target, run_dir)
                job_dir = self._job_output_dir(campaign_id, job.job_name, run_dir)
            except Exception as err:  # noqa: BLE001 - a job that is no longer there is not an error
                logger.debug("no health target for job %s of %s: %s",
                             job.job_name, campaign_id, err)
                continue
            out.append((job.job_name, job_dir, run_dir))
        return out

    def _read_health(self, campaign_id: str, job_name: str, *dirs: str) -> tuple:
        """``(document, reason)``: what this job's simulator says about itself, or why nothing.

        Served from the cache while it is fresh, so the status path and an explicit
        :meth:`get_job_state` share one exec per interval instead of one each.

        Asked of the **simulation** container, which is the simulator's own and is not the job's:
        in the ROS shape the simulator is a sidecar with its own image, and a health command sent
        to the scenario container there names a tool that container does not have.

        *dirs* are tried **in order, job dir first**, because a simulator's records move: while a
        run is live its clock record sits in the job's own output dir, and only results collection
        puts it beside the run. This read is only ever asked about a *running* job, so the job dir
        is the answer -- but the run dir is tried after it rather than assumed away, since where a
        backend writes is the backend's business and one lane's layout is not the other's. Only a
        read that found nothing pays for the second exec.

        Exactly one of the two returns is set. ``reason`` is never left empty for a read that
        failed: a diagnostic whose own failure is silent reports a wedged run as a fine one, which
        is the failure mode this whole path exists to prevent.
        """
        from robovast.common.execution import in_run_env
        from robovast.common.simulators import health_command

        cached = self._cached_health(campaign_id, job_name)
        if cached is not None:
            return cached
        try:
            execution = self._campaign_execution(campaign_id)
        except Exception as err:  # noqa: BLE001 - an unreadable config is a reason, not a crash
            return self._store_health(
                campaign_id, job_name,
                (None, f"could not read this campaign's configuration: {err}"))
        try:
            # Through the lane hook, not :meth:`_job_container`: on the cluster a target is a
            # ``(pod, container)`` pair, and only the hook knows that.
            target, _run_dir = self._job_state_target(
                campaign_id, job_name, SIMULATION_CONTAINER)
        except Exception as err:  # noqa: BLE001 - a job that has gone is a reason, not a crash
            return self._store_health(campaign_id, job_name, (None, str(err)))

        reasons: list = []
        for candidate in dict.fromkeys(d for d in dirs if d):
            try:
                command = health_command(execution, run_dir=candidate)
            except Exception as err:  # noqa: BLE001 - a backend that cannot say is a reason
                return self._store_health(
                    campaign_id, job_name,
                    (None, f"could not read this campaign's configuration: {err}"))
            if not command:
                # A normal answer, and the same kind `simulation_screenshot` gives: this campaign's
                # simulator does not report on itself. Never rendered as a healthy run.
                return self._store_health(campaign_id, job_name, (
                    None,
                    "this campaign's simulator does not report its own state, so there is nothing "
                    "to read from a live run"))
            exit_code, stdout, stderr, timed_out = self._exec_lane().exec_in(
                target, in_run_env(command), _JOB_STATE_LIMIT_S)
            document, reason = self._health_from_output(
                command, exit_code, stdout, stderr, timed_out)
            if document is not None:
                if reasons:
                    # Said out loud, because a fallback that works is otherwise invisible: this
                    # shape pays an extra exec on every read, and nobody would know which of the
                    # two directories its simulator actually writes to.
                    logger.debug("health for %s of %s came from %s after %d earlier candidate(s)",
                                 job_name, campaign_id, candidate, len(reasons))
                return self._store_health(campaign_id, job_name, (document, None))
            reasons.append(reason)
            if timed_out:
                # A container that did not answer will not answer faster about another directory,
                # and this read has a budget the status path is waiting on.
                break
        return self._store_health(campaign_id, job_name, (None, "; also: ".join(reasons)))

    @staticmethod
    def _health_from_output(command: str, exit_code: int, stdout: str, stderr: str,
                            timed_out: bool) -> tuple:
        """One health command's result as ``(document, reason)``.

        Shared by both lanes deliberately: what the reply *means* is not lane-specific, and two
        lanes interpreting the same output separately is how they drift.
        """
        if timed_out:
            return None, (
                f"{command!r} did not answer within {_JOB_STATE_LIMIT_S}s. The container may be "
                "wedged, which is itself a finding -- but this call cannot confirm it.")
        text = (stdout or "").strip()
        if not text:
            return None, (f"{command!r} exited {exit_code}"
                          + (LocalTransport._said(stderr) or " and printed nothing at all"))
        try:
            return json.loads(text), None
        except ValueError:
            # Reported rather than swallowed: a tool whose output cannot be read is a different
            # problem from a run that is misbehaving, and conflating them hides both.
            return None, f"{command!r} exited {exit_code} but its output was not JSON"

    def _cached_health(self, campaign_id: str, job_name: str):
        """This job's last ``(document, reason)`` while it is inside the TTL, else ``None``."""
        with self._health_guard:
            job = (self._health.get(campaign_id, {}).get("jobs") or {}).get(job_name)
            if job is None or (time.monotonic() - job["at"]) >= _HEALTH_TTL_S:
                return None
            return job["read"]

    def _store_health(self, campaign_id: str, job_name: str, read: tuple) -> tuple:
        """Remember one job's read and return it, so a caller stores and answers in one line."""
        with self._health_guard:
            entry = self._health.setdefault(campaign_id, {})
            entry.setdefault("jobs", {})[job_name] = {"at": time.monotonic(), "read": read}
        return read

    def _attach_health(self, campaign_id: str, snap: "Status") -> "Status":
        """Put the running jobs' findings on a status snapshot, and never wait to do it.

        The findings served are the ones already in hand; a stale cache triggers a refresh on its
        own thread and this read answers with what it has. That ordering is the point: the exec
        has a timeout, and a status read that waited even that long for a wedged container would
        make every watcher of a broken campaign slow -- exactly when a reader needs an answer.
        One poll's worth of latency is nothing against runs measured in minutes.

        A terminal campaign is forgotten rather than asked. What a run reported while it was wedged
        is history once it is over, and the results are the record then.
        """
        if is_terminal(snap.phase):
            with self._health_guard:
                self._health.pop(campaign_id, None)
            return snap
        snap.health, snap.health_skipped = self._health_findings(campaign_id)
        return snap

    def _health_findings(self, campaign_id: str) -> tuple:
        """``(findings, skipped)`` from the cache, refreshing off-thread when they have aged out.

        Where "N watchers cost one check" is enforced: the refresh is claimed under the lock, so
        concurrent status reads produce one exec per job per interval however many are asking.

        The two travel together because they are one read's answer, and separating them would let a
        surface report the findings of one interval beside the skips of another.
        """
        now = time.monotonic()
        with self._health_guard:
            entry = self._health.setdefault(campaign_id, {})
            claim = ((now - entry.get("at", 0.0)) >= _HEALTH_TTL_S
                     and not entry.get("refreshing"))
            if claim:
                entry["refreshing"] = True
            findings = list(entry.get("findings") or [])
            skipped = list(entry.get("skipped") or [])
        if claim:
            threading.Thread(target=self._refresh_health, args=(campaign_id,),
                             name=f"health-{campaign_id}", daemon=True).start()
        return findings, skipped

    def _refresh_health(self, campaign_id: str) -> None:
        """Ask every running job once, and replace what this campaign reports.

        Replaces rather than accumulates: a finding is a statement about the run *now*, and one
        that has stopped being true must stop being reported. Failures are left to
        :meth:`get_job_state` to explain -- nothing is invented here, because a finding RoboVAST
        made up is a finding no simulator can be held to.
        """
        findings: list = []
        skipped: list = []
        try:
            for job_name, *paths in self._health_targets(campaign_id):
                document, _reason = self._read_health(campaign_id, job_name, *paths)
                findings.extend(self._findings_from_document(job_name, document))
                skipped.extend(self._skips_from_document(job_name, document))
        except Exception as err:  # noqa: BLE001 - a diagnostic that crashes a service is worse
            logger.debug("health refresh for %s failed: %s", campaign_id, err)
        finally:
            with self._health_guard:
                entry = self._health.setdefault(campaign_id, {})
                entry["at"] = time.monotonic()
                entry["findings"] = findings
                entry["skipped"] = skipped
                entry["refreshing"] = False

    @staticmethod
    def _findings_from_document(job_name: str, document) -> list:
        """The findings in one simulator's reply, as RoboVAST's own two-word wire model.

        Read defensively on purpose: the document belongs to the simulator, so a shape RoboVAST
        did not expect is that simulator's business and must not take a status read down with it.
        ``level`` and ``check`` are required because they are the two fields anything downstream
        acts on -- a finding with neither can be neither matched nor ranked, so it is not one.
        """
        from robovast.client.status import HealthFinding

        out = []
        for raw in (document or {}).get("findings") or []:
            if not isinstance(raw, dict):
                continue
            level, check = raw.get("level"), raw.get("check")
            if not level or not check:
                continue
            out.append(HealthFinding(job_name=job_name, level=str(level), check=str(check),
                                     detail=str(raw.get("detail") or "")))
        return out

    @staticmethod
    def _skips_from_document(job_name: str, document) -> list:
        """The checks one simulator says it did not run, each prefixed with the job.

        Carried because a check that never ran and a check that passed are the same *absence* of a
        finding, and the absence reads as "nothing is wrong". The simulator states its own reason,
        so nothing is composed here beyond saying which job it came from.

        Not turned into ``warn`` findings: a finding has a ``level`` its simulator chose, and
        manufacturing one would put RoboVAST's word in the simulator's mouth -- for a check whose
        whole point is that it reached no verdict.
        """
        out = []
        for note in (document or {}).get("skipped") or []:
            text = str(note).strip()
            if text:
                out.append(f"{job_name}: {text}")
        return out

    #: How many lines of a failed read's stderr travel with its reason.
    #:
    #: More than one, because one was not enough to act on: the environment setup a read runs
    #: through says on stderr which overlays it found, and dropping every line but the last left
    #: "No module named 'scenario_execution'" -- a message a missing overlay and a genuinely absent
    #: module produce identically. Bounded, because a stack trace is not a reason.
    _STDERR_TAIL_LINES = 4

    @classmethod
    def _said(cls, stderr: str, prefix: str = ": ") -> str:
        """The tail of what the container said, or ``""`` when it said nothing.

        Composed with *prefix* so a caller's sentence reads as one line whether or not there was
        anything to append -- the alternative being every call site branching on it.
        """
        lines = [line for line in (stderr or "").strip().splitlines() if line.strip()]
        if not lines:
            return ""
        return prefix + " | ".join(lines[-cls._STDERR_TAIL_LINES:])

    #: How scenario-execution reports where a scenario has got to. A *fixed* command, so this is
    #: a read and not a probe -- and named here rather than derived from a backend because the
    #: scenario runs in every campaign whatever the simulator is.
    _TREE_STATE_COMMAND = "python3 -m scenario_execution.tree_state"

    def _read_scenario_state(self, state, target, run_dir: str) -> None:
        """Fold the run's behaviour-tree log into ``state.scenario``, or say why not.

        Asked of scenario-execution's own reader rather than parsed here: the log's shape is
        its record to change, and a second implementation of someone else's format in this repo
        would be the thing that breaks when it does.

        Run through :func:`~robovast.common.execution.in_run_env`, which is not optional:
        ``scenario_execution`` is colcon-built into ``/ws`` and is on no interpreter's path until
        that overlay is sourced, so a bare argv answers ``No module named 'scenario_execution'``
        in every image ever built.
        """
        from robovast.common.execution import in_run_env
        # The exit code is not consulted: the reader states its own outcome in the JSON (``found``
        # plus its reason), and a nonzero exit with a usable reply is its business, not ours.
        _code, stdout, stderr, timed_out = self._exec_lane().exec_in(
            target, in_run_env(f"{self._TREE_STATE_COMMAND} {shlex.quote(run_dir)}"),
            _JOB_STATE_LIMIT_S)
        if timed_out:
            state.unavailable.append(
                f"reading the scenario's tree did not finish within {_JOB_STATE_LIMIT_S}s")
            return
        text = (stdout or "").strip()
        if not text:
            # The reader is a command RoboVAST ships, so there are only two ways it can be
            # missing, and the stderr above distinguishes them: the note says whether the run's
            # overlay was sourced, and Python says whether it was the package or the module it
            # could not find. Naming that here rather than leaving a runpy sentence to be
            # interpreted -- three rounds of this were spent deciding which of the two it was.
            state.unavailable.append(
                "could not read the scenario's tree"
                + (self._said(stderr) or ": it printed nothing at all")
                + ". If the note says the overlay was sourced, this image's scenario-execution "
                  "is older than the reader and the image needs rebuilding; if it says no "
                  "overlay was found, the container is not one a run's tools live in.")
            return
        try:
            reply = json.loads(text)
        except ValueError:
            state.unavailable.append("the scenario's tree reader did not return JSON")
            return
        if not reply.get("found"):
            # Its own stated reason -- a run with bt_log off, or one that has not ticked yet --
            # which is more use than "unavailable" and is already phrased for a reader.
            state.unavailable.append(reply.get("error", "the scenario reported no tree"))
            return
        state.scenario = reply

    #: How the run's own resource monitor is read back while the run is still going. Its files sit
    #: under the run dir on the shared ``/out``, so ONE read in the scenario container returns every
    #: container's, rather than an exec per container.
    #:
    #: Header plus tail, not the whole file: the header carries the column contract the parser
    #: checks, and the tail is enough for the newest complete tick however long the run has been
    #: going. A whole-file read would grow without bound for an answer about *now*.
    _RESOURCE_TAIL_LINES = 200

    #: Depth of the search for the monitor's CSVs, from the **job** dir (see
    #: :meth:`_job_output_dir`) -- they sit directly in it, one per container. A level of slack for
    #: the fallback case where that dir could not be resolved and the run dir is searched instead.
    _RESOURCE_FIND_DEPTH = 2

    def _read_resources(self, state, target, run_dir: str) -> None:
        """Put each container's newest resource sample on ``state.resources``, or say why not.

        The question this answers is the one neither other read can: a run stuck at 0% CPU is
        deadlocked, one at 100% is spinning, and both look identical in a log and in a tree that
        says "still RUNNING". Passed through as numbers and never scored -- which of the two is
        wrong is not RoboVAST's to judge.

        The monitor writes this file itself for every container of the run, so nothing new runs in
        the run and nothing is added to the image.
        """
        from robovast.results_processing.resource_usage import ScanStats, parse_container_rows

        script = (f'find {shlex.quote(run_dir)} -maxdepth {self._RESOURCE_FIND_DEPTH} '
                  f'-name "resource_usage_*.csv" -type f | while read -r f; do '
                  f'echo "@@ $(basename "$f")"; head -1 "$f"; '
                  f'tail -n {self._RESOURCE_TAIL_LINES} "$f"; done')
        _exit_code, stdout, stderr, timed_out = self._exec_lane().exec_in(
            target, ["/bin/bash", "-c", script], _JOB_STATE_LIMIT_S)
        if timed_out:
            state.unavailable.append(
                f"reading this run's resource samples did not finish within {_JOB_STATE_LIMIT_S}s")
            return
        blocks = self._split_resource_blocks(stdout or "")
        if not blocks:
            state.unavailable.append(
                "this run has recorded no resource samples under " + run_dir
                + self._said(stderr, prefix="; the container said: "))
            return
        stats = ScanStats()
        out = {}
        for container, lines in blocks.items():
            samples = parse_container_rows(lines, container, stats)
            if not samples:
                continue
            newest = max(s.wall_ts for s in samples)
            out[container] = {
                "at": newest,
                "processes": [{"name": s.name, "cpu_percent": s.cpu_percent,
                               "memory_rss_bytes": s.memory_rss_bytes}
                              for s in samples if s.wall_ts == newest],
            }
        if not out:
            # The parser's own account of why, which names a changed header or an empty file --
            # both more use than "no samples", and neither invented here.
            state.unavailable.append(
                "this run's resource samples could not be read: "
                + "; ".join(stats.unreadable + stats.empty))
            return
        state.resources = out

    @staticmethod
    def _split_resource_blocks(text: str) -> dict:
        """``{container: [csv lines]}`` from the marked concatenation the read above prints.

        The container is taken from the file name, which is what
        :func:`~robovast.results_processing.resource_usage.expected_container_files` already
        encodes: ``resource_usage_<container>.csv``, with ``main`` for the scenario container.

        A packed Job holds several runs under one ``/out``, so the same container appears more than
        once. The **last** block for a name wins, and the parser then keeps that block's newest
        tick: the question is what is happening now, and the run still being appended to is the one
        that answers it. Merging the blocks would put a finished run's processes beside a live
        one's under a single container.
        """
        blocks: dict = {}
        current = None
        for line in text.splitlines():
            if line.startswith("@@ "):
                name = line[3:].strip()
                current = name.removeprefix("resource_usage_").removesuffix(".csv")
                blocks[current] = []
            elif current is not None:
                blocks[current].append(line)
        return {k: v for k, v in blocks.items() if v}

    def exec_in_job(self, campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> "ExecResult":
        """Run *command* in the live job's container, recording the probe first.

        Locally the scenario runs in a container of a fixed name and every sidecar takes its role's
        name, which is the same mapping ``logs/system_<name>.log`` follows.

        Run in the run's own environment, not a bare login shell: ``ros2`` and everything else
        colcon-built lives in an overlay no shell rc sources, so ``ros2 topic list`` -- the single
        most likely thing to type here -- answered ``command not found``.
        """
        from robovast.common.campaign_data import KIND_PROBED, record_intervention
        from robovast.common.execution import in_run_env, job_artifact_dir
        from robovast.service.interface import ExecResult

        if not (command or "").strip():
            raise ValueError("exec_in_job needs a command: there is no scenario to start here, "
                             "only a live job to look at.")
        campaign_dir = self._campaigns_root() / campaign_id
        self._require_running_job(campaign_id, job_name)
        try:
            job_dir = os.path.relpath(job_artifact_dir(campaign_dir, job_name), campaign_dir)
        except (FileNotFoundError, OSError, ValueError):
            # The documented startup race, as in stop_job: no manifest entry yet. The run key below
            # is this lane's own job identity, so resolution does not depend on it.
            job_dir = ""
        # Before the command, not after: it may change the run or wedge it, and a crash in between
        # must not leave perturbed data with nothing saying why.
        record_intervention(campaign_dir, kind=KIND_PROBED, job_dir=job_dir, job_name=job_name,
                            source=source, detail=command, runs=(job_name,))
        target = self._job_container(container, campaign_id)
        exit_code, stdout, stderr, timed_out = self._exec_lane().exec_in(
            target, in_run_env(command), _PROBE_LIMIT_S)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                          timed_out=timed_out, limit_s=_PROBE_LIMIT_S, limit_source="command")

    def _job_container(self, role: str, campaign_id: str = "") -> str:
        """The container a role runs in on this lane.

        The scenario's container has a fixed name; a sidecar's is its role. Mapped rather than taken
        verbatim so a caller names the role it means and the lane resolves it -- the cluster answers
        the same question with a pod and a container, which is why the callers never build one.

        With a *campaign_id* the mapping comes from that campaign's own container plan, which is the
        only thing that knows how many containers back a role: a simulator stepped in-process **is**
        the scenario container, so ``simulation`` must resolve to it rather than to a name nothing
        started. Without one -- a caller that has no campaign in hand -- the role's own name is the
        best available answer, and a role this campaign does not have fails on the exec rather than
        here, which is the same outcome as before.
        """
        from robovast.common.config import CONTAINER_ROLES
        if role not in CONTAINER_ROLES:
            raise ValueError(f"unknown container role {role!r}; expected one of "
                             f"{', '.join(CONTAINER_ROLES)}")
        if campaign_id:
            role = self._plan_role(campaign_id, role)
        return self._CONTAINER_NAME if role == SCENARIO_CONTAINER else role

    def _plan_role(self, campaign_id: str, role: str) -> str:
        """*role* resolved to the container name this campaign actually runs it in.

        :func:`~robovast.common.containers.plan_containers` is the one map every other addresser of
        these containers uses (compose generation, the job manifest, the image build), and its whole
        point is that a second lookup is free to disagree with what runs -- silently, as a
        diagnostic entering the wrong container.

        ``simulation`` falls back to the scenario container when the plan names nothing for it, and
        the other roles do not. That asymmetry is a fact about the roles rather than a convenience:
        a simulator either has a container of its own or is stepped inside the scenario container,
        so "no simulation container" means "in the scenario one" -- while a ``sut`` that nothing
        declares is genuinely not there, and answering with a different container would be the
        silent misdirection this map exists to prevent.

        An unreadable config leaves the role as its own name rather than raising: the reads that
        follow report their own failures with a reason, and a config error surfaced from here would
        replace that reason with this one.

        A role the plan does not name is likewise returned as itself. It must **not** fall back to
        the scenario container: that fallback existed, and it turned "I could not tell" into "the
        simulator is in the scenario container" -- a confident wrong answer that sent a health read
        into a container with no simulator. Where the fold is real the plan says so, and where the
        plan cannot be read the *pod* knows (see ``ClusterService._job_pod_target``).
        """
        from robovast.common.containers import plan_containers
        try:
            roles = plan_containers(self._campaign_execution(campaign_id)).roles
        except Exception as err:  # noqa: BLE001 - the reads downstream state their own reasons
            logger.debug("no container plan for %s, addressing %r by name: %s",
                         campaign_id, role, err)
            return role
        return roles.get(role, role)

    def _require_running_job(self, campaign_id: str, job_name: str):
        """The named job, or raise — shared by both lanes' :meth:`stop_job` preconditions.

        Resolved through :meth:`list_jobs` rather than a lane-specific probe so the
        precondition is checked against the very status the caller was shown. ``KeyError``
        for a job that does not exist, ``RuntimeError`` naming the phase for one that
        exists but is not running: only a job that is *underway* has something to kill.

        A node-calibration probe is refused outright, whatever its status. Stopping a job
        records a ``killed`` intervention against the runs it was carrying, and a probe
        carries none -- it is not one of the campaign's runs, and is deliberately absent from
        the job-links manifest that resolves them -- so the record would name runs that do
        not exist. Refused here rather than in the cluster lane's ``stop_job`` because this
        is the precondition both lanes share and the one the web UI mirrors when it decides
        whether to offer the button.
        """
        jobs = self.list_jobs(campaign_id).jobs
        job = next((j for j in jobs if j.job_name == job_name), None)
        if job is None:
            known = ", ".join(j.job_name for j in jobs) or "none"
            raise KeyError(f"job {job_name!r} not found in campaign {campaign_id!r} "
                           f"(jobs: {known})")
        if job.kind == JobKind.CALIBRATION:
            raise RuntimeError(
                f"job {job_name!r} is a node-calibration probe, not one of the campaign's "
                f"runs — it cannot be stopped individually: there is no run to record as "
                f"killed, and the batch abandons its own probes when it ends")
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

    def _adopts_on_restart(self) -> bool:
        """Whether a successor process picks this lane's running campaigns back up.

        ``False`` here: a local campaign's compute is containers this process started, so
        nothing comes back for them and exiting has to tear them down or they are orphaned.
        :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`
        overrides it -- a cluster campaign's compute is Kubernetes Jobs that outlive any one
        service process, and startup adoption re-attaches to them.

        A property of the **lane**, deliberately not of the environment. Asking whether this
        process happens to run inside a pod answers a different question and gets it wrong
        for an off-cluster service driving a cluster, which adopts on its next start like
        any other.
        """
        return False

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

        None of that happens on a lane that :meth:`_adopts_on_restart`: there the
        campaigns are meant to outlive this process, and the successor re-attaches.
        """
        # Held containers first, and unconditionally: they are the ones nothing else
        # reaps, and a service with no running campaign would otherwise return below while
        # still holding a multi-gigabyte image. Every slot, not just the caller's -- a query
        # container outliving the process is exactly the leak the pool cap exists to bound.
        if self._exec_mgr is not None:
            try:
                self._exec_mgr.stop_all()
            except Exception as e:  # noqa: BLE001 - shutdown must not fail on cleanup
                logger.warning("could not stop held exec containers: %s", e)
        with self._lock:
            running = [e for e in self._campaigns.values() if not self._is_done(e)]
        if not running:
            return
        if self._adopts_on_restart():
            # Left running on purpose: this lane's compute outlives the process and the
            # next one adopts it. Stopping here would do worse than discard the work --
            # the cooperative stop below persists a terminal ``outcome.json``, and a
            # campaign that has recorded an ending is one no successor will pick up again.
            logger.info(
                "Shutting down — leaving %d running campaign(s) for the successor: %s",
                len(running), ", ".join(e.campaign_id for e in running))
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

    def _on_campaign_finished(self, campaign_id: str, state) -> None:
        """The campaign is over: record that wherever it must be discoverable.

        No-op here. The local lane needs nothing — the driver has already written
        ``_execution/outcome.json``, which is where :func:`read_campaign_finished_at` reads
        the time from. :class:`ClusterService` publishes a marker as well, because ordering
        a listing there must not mean fetching a record per campaign.

        The counterpart of :meth:`_on_campaign_started`, and deliberately not symmetrical
        with it in placement: that one runs before anything that can fail, so a doomed
        campaign is still findable, while this one can only run once there is an ending to
        report.
        """

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
            entries = dict(self._campaigns)
        elsewhere = self._extra_live_ids()
        mem = set(entries) | elsewhere

        def is_live(cid: str) -> bool:
            """Whether *cid* is being worked on right now.

            ``_is_done`` rather than a phase test of our own, because it is already the
            class's at-rest predicate (`_rest_key`, `_ensure_deletable`) — one notion of
            "running" here, not two that can drift. Registration is not it: an entry is
            removed only on delete, so it outlives the campaign and would pin every
            campaign of this service's life to the top.
            """
            entry = entries.get(cid)
            return cid in elsewhere or (entry is not None and not self._is_done(entry))

        # Live campaigns first, then newest first by recorded start time within each group.
        # Ordering by activity and not by recency alone is the point: a campaign runs for
        # hours to days, so the one the caller is asking about is the one still being driven,
        # and strict recency buries it under everything launched since.
        #
        # A campaign that becomes active *again* — a re-triggered postprocessing, an
        # upload-to-share, an import — is carried by the same term: `_dispatch_background`
        # registers a fresh entry with its phase already set, so it reads live from the next
        # listing and falls back on its own once the worker ends. Deliberately not done by
        # restamping `created_at`, which that method refuses for this exact reason: the
        # campaign rises because it is live, while its start time stays the truth.
        #
        # Never sort on the id: it is `<name>-<timestamp>` with a user-supplied name (see
        # `campaign_id_for`), so id order is alphabetical by name and only chronological
        # within one name. That matters beyond display, because offset/limit slice *this*
        # order — a name-ordered window would hide the newest campaigns from the caller
        # entirely. A campaign whose start time is unknown (no readable store, no execution
        # record) sorts last; the id only breaks ties, so the order is deterministic even
        # though the input is a set.
        #
        # Every term is answered from memory — `_started_at_for` is memoised and the
        # liveness read is an in-memory snapshot — so this pass still costs no I/O, which
        # matters because the campaign-list SSE stream repeats it once a second.
        # Within the terminal group the key is when a campaign ENDED, falling back to when
        # it started. That is the question asked of a finished campaign -- which of these
        # results is fresh -- and start time answers it badly: a campaign that ran for eight
        # hours and ended a minute ago is the newest thing here and sorts near the bottom by
        # start. Live campaigns keep sorting by start, because they have no end yet and
        # because a just-launched one belongs at the top.
        #
        # The fallback is not a transitional measure: a campaign whose record carries no
        # terminal outcome never gets one, so those keep ordering exactly as they did.
        started = {cid: self._started_at_for(cid) for cid in disk | mem}
        finished = {cid: self._finished_at_for(cid) for cid in started}
        def _key(c: str):
            live = is_live(c)
            when = started[c] if live else (finished[c] or started[c])
            return (live, when is not None, when or "", c)

        all_ids = sorted(started, key=_key, reverse=True)
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

        An **import** is dispatched through here too, and it is the one case where the
        campaign directory does not exist yet: registering the entry is exactly what makes
        the campaign visible while its bytes are still arriving. So the two reads below are
        allowed to find nothing and fall back — which is also the honest answer, since a
        campaign being imported has no earlier start time than now.
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
            # ...but the FINISH time is restamped, and must be: this operation ends the
            # campaign again, later than last time. Dropping the cached value is what makes
            # the next listing re-read it; `_started_at_cache` needs no such thing because
            # a start time is written once and never edited.
            self._finished_at_cache.pop(campaign_id, None)
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
            from robovast.execution.notify import Notifier
            from robovast.execution.status_recovery import record_step_outcome
            handler = None
            try:
                handler = add_campaign_log_handler(
                    str(campaign_dir / "_execution" / "postprocessing.log"))
            except Exception:  # pylint: disable=broad-except
                logger.warning("Could not open postprocessing.log for %s",
                               request.campaign_id, exc_info=True)
            try:
                ok, message = self._postprocess_campaign(
                    request.campaign_id, campaign_dir,
                    force=request.force, skip=list(request.skip or []), state=state)
            finally:
                remove_campaign_log_handler(handler)
            status = record_step_outcome(campaign_dir, postprocessing=(ok, message))
            state.update(postprocessed=status.postprocessed,
                         postprocessing_error=status.postprocessing_error)
            state.set_phase(Phase.FINISHED)
            # Same one-shot notifier as a re-triggered share: this op runs from disk with
            # no live entry to inherit one from, and it reports on a campaign that ended
            # long ago -- so neither branch is the campaign's terminal message.
            notifier = Notifier.from_env(request.campaign_id)
            if ok:
                notifier.postprocessed()
            else:
                notifier.postprocessing_failed(message)

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
            from robovast.execution.controller import make_upload_progress_cb
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
                backend.share_campaign(str(campaign_dir), options,
                                       progress_callback=make_upload_progress_cb(state))
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

    def validate_project(self, workspace_id: str, path: str = "",
                         check_world: bool = True) -> ValidationReport:
        from robovast.common.config_validation import validate_project_file
        try:
            project = self._resolve_project(workspace_id, path)
            result = validate_project_file(project.config_path)
        except Exception as e:  # noqa: BLE001 - editor sends in-progress YAML; never 500
            return ValidationReport(valid=False, problems=[
                ValidationProblem(stage="error", message=str(e))])
        # Only once the cheap checks pass. Compiling a world for a file with a schema
        # error spends a container to report something already in the reply, and the world
        # a broken file names is not necessarily the one it will name when it is fixed.
        if check_world and result.get("valid"):
            result = self._with_world_check(workspace_id, path, project, result)
        return ValidationReport.model_validate(result)

    def _with_world_check(self, workspace_id: str, path: str, project,
                          result: dict) -> dict:
        """*result* plus any problem with the world(s) this campaign would load.

        The one check here that runs a container. It is worth it because the failure it
        catches is otherwise per-trial: a world that does not compile fails every run of
        the sweep, after the image pull and the schedule, with nothing earlier to say so.
        The container is *held* (see ``ExecRequest.query``), so a second validation of the
        same project costs an exec rather than a container start.

        A failure of the check itself is never a failure of the campaign: an advisory says
        the world was not checked and why, and ``valid`` is left as the cheap checks found
        it.
        """
        from robovast.common.common import load_config
        from robovast.service.world_query import world_problems
        try:
            parameters = load_config(project.config_path) or {}
            problems = world_problems(
                self.exec_in_container,
                workspace_id=self.store.registry.require(workspace_id)["workspace_id"],
                # The workspace-relative path as the caller gave it; empty is fine and
                # means the sole .vast, which is what exec_in_container resolves too.
                config_path=path,
                vast_dir=str(Path(project.config_path).parent),
                parameters=parameters)
        except Exception as e:  # noqa: BLE001 - an unavailable check is not a bad campaign
            logger.debug("the world check did not run: %s", e)
            return result
        if not problems:
            return result
        fatal = [p for p in problems if "was NOT checked" not in p["message"]]
        return {**result,
                "valid": result.get("valid", False) and not fatal,
                "problems": list(result.get("problems") or []) + problems}

    def preview_configurations(
        self, workspace_id: str, max_configs: int = 0, path: str = ""
    ) -> PreviewResponse:
        from robovast.common.common import load_config
        from robovast.common.config_generation import generate_scenario_variations
        project = self._resolve_project(workspace_id, path)
        aux_containers: list = []
        # Both branches compose, so both need whatever this lane uses to reach a variation's
        # helper image -- and a search .vast reaches it through the same variation loop.
        # Held rather than span-scoped: this is the authoring loop, previewed repeatedly.
        with self._aux_runner_context(_preview_tag(workspace_id, path), project, hold=True):
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
                aux_containers = list(campaign_data.get("aux_containers") or [])
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
                    sut=convert_dataclasses_to_dict(c.get("sut", {})),
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
                               aux_containers=aux_containers,
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
        # Through the exec lane's held query container, on BOTH lanes. Not the local
        # `docker run` fallback: in a controller pod that would run on whatever host the
        # service happens to sit on -- a different image cache, or no docker at all -- with
        # nothing in the reply to say the answer did not come from the cluster. This is
        # also what lets the cluster lane answer at all, which otherwise has no runner
        # outside a campaign's composition.
        from robovast.common.config_generation import set_container_runner_factory
        from robovast.service.world_query import ExecSlotContainerRunner, _reset_factory
        runner = ExecSlotContainerRunner(
            self.exec_in_container,
            workspace_id=self.store.registry.require(workspace_id)["workspace_id"],
            config_path=path)
        token = set_container_runner_factory(lambda _spec, _r=runner: _r)
        try:
            payload, image = describe_world_payload(
                execution, block, str(Path(project.config_path).parent),
                entities=entities, targets=targets)
        except WorldQueryUnavailable as exc:
            raise ValueError(str(exc)) from None
        finally:
            _reset_factory(token)
            runner.close()
        return WorldDescription(
            backend=backend_name(execution) or "",
            image=image,
            world=str(payload.get("world") or ""),
            packaged=bool(payload.get("packaged")),
            inputs=[str(p) for p in (payload.get("inputs") or [])],
            components=list(payload.get("components") or []),
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
        max_bytes: int | None = None,
    ) -> "DataQueryResult":
        from robovast.results_processing.data_query import query_data_db
        from robovast.service.interface import DataQueryResult
        result = query_data_db(self._query_dir(campaign_id), sql, max_rows,
                               max_bytes=max_bytes)
        return DataQueryResult(campaign_id=campaign_id, **result)

    def stream_campaign_query_csv(self, campaign_id: str, sql: str):
        # Resolved through _query_dir like the JSON path, so both lanes name the campaign
        # the same way rather than this one needing its own override.
        from robovast.results_processing.data_query import stream_query_csv
        return stream_query_csv(self._query_dir(campaign_id), sql)

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
        this call and each caller states what it needs instead — :meth:`_query_dir` (the
        campaign a query names), :meth:`_config_dir` (the frozen ``.vast``), or
        :meth:`_whole_campaign_dir` (everything, said out loud).

        That refusal is the point. While this method silently answered "the whole
        campaign", every inherited method that touched it became a whole-campaign
        download — ``list_campaign_plots`` pulled every rosbag to read one YAML file, per
        campaign, on every Results page load.
        """
        return self._campaign_dir(campaign_id)

    def _whole_campaign_dir(self, campaign_id: str):
        """Campaign dir for a caller that genuinely needs **arbitrary** files from it.

        The honest, explicit form of an arbitrary-file need: notebook rendering
        against run outputs, and the ``/results`` file address space. On the cluster this is
        a full ``fetch_campaign``, which is expensive and says so at the call site rather
        than hiding behind a resolver name.
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
        """Dir a **query** names. The rows come from the central index; all this has to
        carry is which campaign is being asked about.

        Locally identical to :meth:`_data_dir`. Separate from it because ``ClusterService``
        refuses ``_data_dir`` — there a campaign dir means an object-store transfer, and a
        query needs none. Callers needing actual files say which: the frozen config via
        :meth:`_config_dir`, or everything via :meth:`_whole_campaign_dir`."""
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
        from robovast.common.config import (CUSTOM_PANEL_TYPE, always_on_panel_types,
                                            flatten_panel_shorthand, visualization_block)
        from robovast.common.config_validation import _safe_load
        from robovast.common.simulators import merge_default_panels
        from robovast.service.interface import CampaignPanelsResponse
        from robovast.service.postprocessing_edit import campaign_vast
        # Through `_config_dir` and not `_campaign_dir`: on the cluster lane a campaign this pod
        # does not drive has only its two record objects fetched, so `_campaign_dir` names a cache
        # holding `campaign.db` and `_execution/` and no `_config/` at all -- and `campaign_vast`
        # then raises "no .vast", which reaches the browser as a 500 on this route. The run view
        # asks for its panels before it draws anything, so that one failure emptied the whole view:
        # no `playback`, no backend-contributed `scene3d`, just the run-selection header. Only
        # `_config_dir` materialises the frozen snapshot first, which is why the panel-asset reader
        # below already uses it. Its parent is the campaign dir `campaign_vast` wants.
        cfg, _ = _safe_load(str(campaign_vast(Path(self._config_dir(campaign_id)).parent)))
        run_view = visualization_block(cfg, "results", "run_view") or {}
        authored = run_view.get("panels") or []
        # Contributed panels: the transport bar every run view needs, plus the ones that replay
        # what the configured simulator always records (roqsim's `scene3d`) -- so a campaign never
        # declares a panel it could not do without. Merged here rather than in the UI, so the
        # served list and the view cannot disagree -- which is also why `transport_only` below is
        # answered here: whether anything in the list is *content* is a question about the merge.
        raw = merge_default_panels(authored, (cfg or {}).get("execution") or {})
        # Each panel is a single-key mapping ``{<type>: <props-or-null>}`` (``log:`` for a bare
        # panel), or the plain string ``"log"`` for a bare ``- log`` with no colon; flatten to the
        # ``{type, ...fields}`` the web UI consumes, through the same function that decides that
        # shape everywhere else.
        # Attach a Module-Federation ``remote`` descriptor to panels rendered as remotes:
        # package panels (entry-point types shipping WEB_PANEL) and user ``custom`` panels.
        pkg_remotes = _panel_remotes()
        panels = []
        for i, entry in enumerate(raw):
            # Copied, because the flattened form of an already-flat entry is the entry itself --
            # and attaching a `remote` below would then write into the loaded config, or worse
            # into the module-level contributed list.
            panel = dict(flatten_panel_shorthand(entry))
            ptype = panel.get("type")
            if ptype == CUSTOM_PANEL_TYPE:
                remote = panel.get("remote")
                if remote:
                    rel = remote if remote.endswith(".js") \
                        else f"{remote.rstrip('/')}/remoteEntry.js"
                    panel["remote"] = {
                        "name": f"panel_{i}",
                        "remote_entry_url": Routes.campaign_panel_asset(campaign_id, rel),
                        "module": panel.get("module") or "./panel",
                    }
            elif ptype in pkg_remotes:
                panel["remote"] = pkg_remotes[ptype]
            panels.append(panel)
        # The transport bar is the clock the other panels follow, not something to look at, so a
        # list of nothing but always-on panels is a bare run view -- whoever wrote them: a campaign
        # that declares `playback` itself (to move or re-title the bar) has still authored no
        # content, and a backend's contributed panel is content even though no `.vast` asked for it.
        always_on = always_on_panel_types()
        return CampaignPanelsResponse(
            campaign_id=campaign_id, panels=panels, timeline=run_view.get("timeline"),
            transport_only=all(p.get("type") in always_on for p in panels))

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

    # None means 'nothing to arrange', per the docstring
    def _scene_runner_context(self, campaign_id: str, identity: dict):  # pylint: disable=useless-return
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

    # Node levels the web Explorer tree can address (campaign → batch → config → run).
    # ``batch`` is a *logical* level: it has no directory of its own (see
    # :meth:`_node_data_dir`), so it is identified by the injected ``BATCH`` index instead,
    # and it only appears in the tree for a search campaign. Taken from the config module,
    # which is where the set a ``.vast`` may name belongs -- validation rejects a scope
    # outside it, and a second copy here is one that could disagree with what was accepted.
    _VIS_LEVELS = EXPLORER_SCOPES

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
        None" does not mean "done" — a not-yet-started campaign is still live.
        Done means either a terminal phase, or a thread that existed and has ended
        (covering a crashed worker that never recorded a terminal phase)."""
        return is_terminal(entry.state.snapshot().phase) or (
            entry.thread is not None and not entry.thread.is_alive())

    #: Every file a campaign's summary or reconstructed status is derived from. ``campaign.db``
    #: carries the run tallies and the mode; ``outcome.json`` the terminal outcome; ``data.db``
    #: is what ``postprocessed`` is derived from (see _derive_postprocessed). The SQLite
    #: sidecars are listed because a store in WAL mode commits into ``-wal`` and can leave the
    #: main file's mtime standing still -- no journal_mode is set today, so this is insurance
    #: against a later change silently freezing every card rather than a current requirement.
    _REST_FILES = ("campaign.db", "campaign.db-wal", "campaign.db-journal",
                   "_execution/outcome.json", "_execution/data.db")

    def _rest_dir(self, cid: str) -> Optional[Path]:
        """The campaign's record directory **if it is already on local disk**, else ``None``.

        Deliberately never fetches, which is what separates it from :meth:`_record_dir`: it
        exists to be called on the listing's hot path, where ``_record_dir`` is the cost being
        avoided. On the cluster lane that method re-validates its two objects against the store
        on every call, so asking it for a cache key would pay the price the cache exists to
        save. ``None`` means "not cheaply knowable here" and the caller falls back to the full
        path, which is the honest answer for a campaign whose records have never been fetched.
        """
        local = self._campaign_dir(cid)
        return local if (local / "campaign.db").is_file() else None

    def _rest_key(self, cid: str, entry) -> Optional[tuple]:
        """Cache key for a campaign **at rest**, or ``None`` when it must not be cached.

        Two parts, and both are load-bearing.

        *Is it at rest?* Only a campaign nothing is driving may be cached. Note this is not the
        same as "terminal": ``_dispatch_background`` puts a campaign back under a live entry for
        an export-to-share, a re-triggered postprocessing or an import, so those reactivations
        exclude themselves here with no special case. The entry's identity goes into the key as
        well, because such a dispatch constructs a *new* ``ControllerState`` -- so the next read
        misses by construction rather than by a file stat that a share might not have moved.

        *Has anything changed?* The stat tuple of :data:`_REST_FILES`. Deliberately a stat and
        not an invalidation call from each mutating operation: enumerating those means the next
        operation somebody adds forgets one and a card goes stale forever with nothing to point
        at -- and share and re-postprocessing would have been two of the entries to remember. A
        stat key needs nobody to remember, and it also catches what no invalidation can, such as
        a results directory repaired or imported out of band.
        """
        if entry is not None and not self._is_done(entry):
            return None
        root = self._rest_dir(cid)
        if root is None:
            return None
        stats = []
        for rel in self._REST_FILES:
            try:
                st = (root / rel).stat()
                stats.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stats.append(None)  # absent is itself a fact the answer depends on
        return (id(entry.state) if entry is not None else None, tuple(stats))

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
        with self._lock:
            entry = self._campaigns.get(cid)
        # The listing's hot path. The campaign-list SSE stream re-lists once a second for as
        # long as any tab is open, and everything below -- a status reconstruction, the run
        # tallies, the mode -- is three SQLite opens and a JSON read *per campaign*, for
        # campaigns that are not being driven and whose answers therefore cannot change. The
        # four cheap facts beside it have been memoised for exactly this reason since
        # _campaign_fact was written; these are the expensive ones, and they were not.
        key = self._rest_key(cid, entry)
        if key is not None:
            hit = self._summary_cache.get(cid)
            if hit is not None and hit[0] == key:
                return hit[1]
        campaign_dir = self._record_dir(cid)
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
        summary = CampaignSummary(
            campaign_id=cid, phase=snap.phase, postprocessed=snap.postprocessed,
            description=self._description_for(cid) or "",
            created_by=self._created_by_for(cid) or "",
            origin=self._origin_for(cid),
            started_at=started_at,
            finished_at=self._finished_at_for(cid),
            # The store is consulted behind the snapshot rather than instead of it: a
            # reconstructed Status can carry no mode at all, because the `outcome.json`
            # early-return path hands back whatever the controller journalled and an older
            # record predates the field. `read_campaign_mode` is the read-only fallback that
            # exists for exactly this, and "" is recorded when neither knows.
            mode=snap.mode or read_campaign_mode(campaign_dir) or "",
            num_runs=counts["num_runs"], num_passed=counts["num_passed"],
            num_failed=counts["num_failed"] + counts["num_errors"],
            num_composition_failed=counts.get("num_composition_failed", 0),
            num_no_sample=counts.get("num_no_sample", 0),
            # From the same snapshot as the phase, so a listing cannot show a campaign as
            # finished-and-fine while its Status says postprocessing failed.
            postprocessing_error=snap.postprocessing_error or "",
            share_error=snap.share_error or "",
            # First line only -- see the field's note. Free here: `snap` is already in hand.
            error=(snap.error or "").strip().splitlines()[0] if snap.error else "")
        if key is not None:
            self._summary_cache[cid] = (key, summary)
        return summary

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

        ``num_no_sample`` is 0 for a different reason: those cells DID leave directories,
        and this walk counts their runs -- but "the extractor could not measure this cell"
        is a scoring verdict, not something a ``test.xml`` walk can re-derive. So the runs
        are reported and the coverage loss is not; only the store records that.
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
            "num_no_sample": 0,
        }

    def _started_at_for(self, cid: str) -> Optional[str]:
        """Start time of *cid* as an ISO-8601 UTC string, or None if unknown.

        Read through :meth:`_campaign_fact`, which owns the precedence and the caching.
        What is specific here: a campaign this service is driving reports its in-memory
        launch time, so it is ordered correctly from t=0 — before the controller has
        written the ``campaign`` row seconds later. Listing has to know every candidate's
        start time to order them, and a recorded one never changes
        (``CampaignStore.create_campaign`` stamps it once, and the post-hoc indexer
        preserves it across rebuilds), so caching it is safe.
        """
        return self._campaign_fact(
            cid, lambda entry: entry.created_at,
            read_campaign_created_at, self._started_at_cache)

    def _finished_at_for(self, cid: str) -> Optional[str]:
        """When *cid* ended, as an ISO-8601 UTC string, or None while it is still going.

        Read through :meth:`_campaign_fact`, which owns the precedence and the caching.
        What is specific here: a campaign this service is **driving** answers from its live
        state, so the moment it ends its own listing already reflects that -- and answers
        ``None`` until then, because a campaign that is not over has no finish time and
        must not be given one derived from a phase it is still in.

        Cached like its neighbours, but invalidated rather than assumed permanent: see
        ``_finished_at_cache``. A campaign whose record says nothing (one that predates the
        durable outcome, or an import that arrived without one) is simply unknown, and the
        listing orders it by its start time instead.
        """
        def from_entry(entry):
            snap = entry.state.snapshot()
            if not is_terminal(snap.phase) or not snap.phase_since:
                return None
            return datetime.fromtimestamp(snap.phase_since, tz=timezone.utc).isoformat()

        return self._campaign_fact(
            cid, from_entry, read_campaign_finished_at, self._finished_at_cache)

    def _description_for(self, cid: str) -> Optional[str]:
        """The campaign's description, or None when it was launched without one.

        Read through :meth:`_campaign_fact`, which owns the precedence and the caching.
        A description is written once with the campaign row and never edited, so caching
        it is safe.
        """
        return self._campaign_fact(
            cid, lambda entry: entry.description or None,
            read_campaign_description, self._description_cache)

    def _created_by_for(self, cid: str) -> Optional[str]:
        """Who says they launched *cid*, or None when nobody gave a name.

        Read through :meth:`_campaign_fact`, which owns the precedence and the caching.
        Written once with the campaign row and never edited, so caching it is safe.
        """
        from robovast.common.store import read_campaign_created_by
        return self._campaign_fact(
            cid, lambda entry: entry.created_by or None,
            read_campaign_created_by, self._created_by_cache)

    def _origin_for(self, cid: str):
        """Where *cid*'s configuration came from, or None when it was not recorded.

        Read through :meth:`_campaign_fact`, which owns the precedence and the caching.
        Written once with the campaign row and never edited, so caching it is safe.

        ``None`` is the honest answer for a campaign that ran before the origin was kept.
        Nothing is reconstructed from its frozen ``_config/``: that holds a ``.vast``
        basename and says nothing about which workspace, so it would fill in half the
        answer -- and reading it would cost a per-campaign glob (an object-store lookup on
        the cluster lane) on the listing's hot path.
        """
        from robovast.common.store import read_campaign_origin
        return self._campaign_fact(
            cid, lambda entry: entry.origin,
            read_campaign_origin, self._origin_cache)

    def _campaign_fact(self, cid: str, from_entry, from_disk, cache: dict):
        """One campaign fact, read live-then-durable and memoised.

        The shared body of :meth:`_started_at_for`, :meth:`_description_for`,
        :meth:`_created_by_for` and :meth:`_origin_for`, which differ only in which
        attribute, which reader and which cache they use.

        The precedence is the point: a campaign **this process is driving** answers from
        its in-memory entry, because the controller writes the ``campaign`` row seconds
        later (minutes, if an image has to build) and until then the entry is the only
        copy. Every other campaign is read from its durable record.

        Memoised because the SSE stream re-lists once a second and each of these is
        written once with the campaign row and never edited, so a cached value cannot go
        stale. ``None`` is deliberately **not** cached: a campaign whose store does not
        exist yet must be re-read on the next poll, or it would be remembered as absent
        for the life of the process.
        """
        with self._lock:
            entry = self._campaigns.get(cid)
        if entry is not None:
            return from_entry(entry)
        cached = cache.get(cid)
        if cached is not None:
            return cached
        value = from_disk(self._record_dir(cid))
        if value is not None:
            cache[cid] = value
        return value

    def _status_from_disk(self, campaign_id: str) -> Status:
        """Reconstruct an untracked campaign's Status from its records, memoised at rest.

        Cached for the same reason as the summary and against the same key, but for a
        different traffic shape: this is not the 1 Hz listing, it is the **page-load burst**.
        Every campaign card fetches its status once on mount, and the browser reaches the
        service over HTTP/2 -- so with no connection limit to throttle them, a hundred cards
        issue a hundred of these at once, each a JSON read plus a store read, against a
        40-thread pool.
        """
        from robovast.execution.status_recovery import reconstruct_status_from_disk
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        key = self._rest_key(campaign_id, entry)
        if key is not None:
            hit = self._disk_status_cache.get(campaign_id)
            if hit is not None and hit[0] == key:
                return hit[1]
        status = reconstruct_status_from_disk(self._record_dir(campaign_id))
        if key is not None:
            self._disk_status_cache[campaign_id] = (key, status)
        return status
