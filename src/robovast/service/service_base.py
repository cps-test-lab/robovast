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

"""``ServiceBase`` — the in-process core every service implementation shares.

The in-process half of the :class:`~robovast.service.interface.RobovastInterface`: hosting
a campaign's driver (:func:`robovast.execution.controller.run_batch_campaign`) on a
background thread, serving live status from its
:class:`~robovast.execution.control_server.ControllerState`, the workspace store, the
campaign registry, the service's caches and event log, and every reader that resolves a
campaign under the results root. What depends on where the runs happen is an abstract
hook here and a body in the implementation: ``ClusterService`` is the one production
implementer, and the test suite's ``NullService`` (``tests/service/null_service.py``) the
other, so the code here is exercised without a cluster.

Two rules follow. A body here is correct for *any* implementation, or it is a hook. And an
implementation that does not offer an operation refuses it in its own class with
:class:`~robovast.service.interface.UnsupportedOperation`, never through a default left here.

This module imports no Kubernetes: an implementation reaches its driver inside the hook
that needs it (``tests/service/test_serve_backends.py`` pins that importing the base loads
no implementation).
"""

from abc import abstractmethod
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from robovast.client import file_address
from robovast.client.safe_path import safe_join
from robovast.common import file_view
from robovast.common.config import (EXPLORER_SCOPES, SCENARIO_CONTAINER,
                                    SIMULATION_CONTAINER)
from robovast.common.campaign_data import (LaunchImages, campaign_has_runs,
                                           read_campaign_finished_at,
                                           read_campaign_results_bytes)
from robovast.common.errors import InsufficientStorageError
from robovast.common.store import read_campaign_created_at, read_campaign_description
from robovast.execution.control_server import (STOP_RUNS,
                                               ControllerState, Phase, Status, failure_detail,
                                               is_terminal, stop_checker)
from robovast.service.interface import (ActionResult, CampaignOrigin, CampaignRef,
                                        CampaignDeletion, CampaignTablesCleared,
                                        DeleteCampaignsRequest, ExportRef, ExportStatus,
                                        DeleteCampaignsResponse, OutputsIngested,
                                        CampaignSummary, ConfigNames, OriginKind, ShareListing,
                                        CreateCampaignRequest, CreateUploadRequest,
                                        CreateWorkspaceRequest, EditFileRequest, FileEntry,
                                        FileListing, FileMeta, FileText,
                                        ImportCampaignRequest, JobKind,
                                        ListCampaignsRequest, ListCampaignsResponse,
                                        CampaignLogChunk, JobLogChunk,
                                        ListWorkspacesResponse, TAP_MAX_S,
                                        PreviewConfiguration, PreviewResponse, ResourceUsage,
                                        CacheSize, KeptCacheEntry, ServiceCache,
                                        MigrationMarker, RetriggerAxis, RetriggerReport,
                                        RobovastInterface, Routes, SearchHistory, WorkOrder,
                                        UploadGrant, ValidationProblem,
                                        ValidationReport, VariationTypeInfo, VariationTypeParam,
                                        VariationTypesResponse, VersionInfo, WorkspaceInfo,
                                        WorldDescription, WriteFileRequest)
from robovast.common.disk_reserve import reserve_disabled
from robovast.common.query_limits import query_limits
from robovast.service.storage_reserve import storage_refusal

logger = logging.getLogger(__name__)

#: What the scene cache is called where a reader sees it.
SCENE_CACHE = "scene cache"

#: The built tables under every campaign's ``.cache/`` (:mod:`robovast_data`): rebuilt from
#: the campaign's records the next time something names them.
TABLE_CACHE = "table cache"

#: Below this, a refusal for disk space does not suggest clearing the cache: a clear that frees
#: a few hundred megabytes would send the caller to the wrong lever.
_CACHE_WORTH_CLEARING_BYTES = 1000 ** 3


@dataclass
class _Swept:
    """One cache's share of a :class:`ServiceCache`: what remains, what went, what stayed."""

    size: CacheSize
    freed_bytes: int = 0
    removed: int = 0
    kept: list = field(default_factory=list)


def _extended_bases(candidates, project_dir):
    """Of *candidates*, the ones another candidate names in its ``extends:``.

    A base is identified by something extending it, not by its shape. Guessing from shape --
    "no ``execution:`` block, so a fragment" -- would also swallow a campaign someone is
    halfway through writing, turning a validation error that names the missing section into
    "this workspace has no .vast file".
    """
    import yaml  # pylint: disable=import-outside-toplevel

    bases = set()
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = next(iter(yaml.safe_load_all(f)), None)
        except Exception:  # pylint: disable=broad-except
            continue
        ext = first.get("extends") if isinstance(first, dict) else None
        if isinstance(ext, str) and ext.strip():
            bases.add(Path(os.path.abspath(os.path.join(os.path.dirname(str(path)), ext))))
    return bases


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
    from robovast.common.scene_markers import \
        contribution_for_block  # pylint: disable=import-outside-toplevel
    return contribution_for_block(config, config.get("_config_block") or {}, vast_dir)


# ---------------------------------------------------------------------------
# The service half
# ---------------------------------------------------------------------------


def _no_timeout_note(raw_config: dict) -> str:
    """Say, at launch, that this campaign cannot be judged stalled — before it is.

    ``execution.timeout`` is what makes ``stalled`` a verdict rather than ``null``, and
    ``null`` is the one answer nobody acts on: a campaign that declared no budget gets no
    stall verdict, and so ``vast campaign wait`` can never end on one. Told here because the fix is
    a line in the ``.vast`` and this is the last moment before the compute is spent; four
    minutes into a wedged sweep it is only an explanation.

    Advisory, not a validation error: a campaign with no declared per-run budget is a
    legitimate thing to run.
    """
    from robovast.common.config import declared_job_seconds

    execution = (raw_config or {}).get("execution") or {}
    if not isinstance(execution, dict) or declared_job_seconds(execution):
        return ""
    return ("this project declares no execution.timeout, so no stall verdict is possible "
            "for it — `vast campaign wait` cannot end on one, and get_campaign_status reports "
            "stalled: null, which is not 'healthy'. Declare it to get a verdict. (A "
            "simulator that reports on itself is unaffected: its own findings still end "
            "the wait.)")


class _TrackedCampaign:
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
    #: The launch record's digests (``campaign_data.LaunchImages``) to replay: every image the
    #: campaign runs -- its containers, the sidecar and the aux helpers -- comes from here,
    #: nothing is built and nothing is resolved again. ``None`` for a launch that fixes its
    #: own. Data rather than a callback: it is read in the request handler, so a campaign
    #: whose record lacks a digest fails the *request* with the reason instead of becoming a
    #: failed campaign someone has to go and inspect.
    pinned_images: Optional[LaunchImages] = None
    #: Adopt this campaign id rather than minting a fresh one. Set only when re-entering a
    #: campaign that already exists and is still owed work -- one whose driver a service
    #: restart took away (see ``cluster_execution.campaign_resume``).
    #:
    #: This is the whole difference between a launch and a re-launch, which is why it is one
    #: field here rather than a mode flag on :meth:`ServiceBase._launch_campaign`: that
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

#: Seconds the tap's in-container bound is given past the relay's own, before ``timeout``
#: escalates its ``INT`` to a ``KILL``: a ``ros2`` process shuts down cleanly on the first.
_TAP_KILL_GRACE_S = 5

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
    """Whether the archive carries derived data, from the tar index alone.

    Read from postprocessing's provenance record, which is what says a campaign has been
    postprocessed. Answerable from the member list, without extracting anything, before the
    import even starts. Matched on the member list only: whether the record has ENTRIES needs
    its bytes, and the difference (a campaign that ran postprocessing and derived nothing) is
    not worth reading the archive twice for. The chain that follows re-asks properly with
    ``campaign_has_derived_data``.
    """
    import tarfile  # pylint: disable=import-outside-toplevel

    from robovast.common.campaign_data import \
        POSTPROCESSING_RECORD  # pylint: disable=import-outside-toplevel
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            return any(name.endswith("/" + POSTPROCESSING_RECORD)
                       for name in tar.getnames())
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


def require_scheduling_change(priority, paused) -> None:
    """Refuse a call that asked for nothing.

    Both halves are optional so that setting one leaves the other alone, which makes neither
    of them required -- and a call naming neither would be answered "done" having changed
    nothing, which is the shape of report this interface exists not to give.
    """
    if priority is None and paused is None:
        raise ValueError(
            "nothing to set: give a priority, a paused state, or both.")


class ServiceBase(RobovastInterface):
    """The in-process half of the interface, over the implementation's hooks.

    A campaign always runs a **workspace's** ``.vast``: ``workspace_id`` is the only
    project binding this service accepts (see :meth:`_resolve_project`), and
    ``config_path``/``vast_path`` selects among several ``.vast`` files in that
    workspace. Nothing ambient selects what the service runs; the results root is
    named by ``vast serve --results-dir`` (see
    :func:`~robovast.common.results_root.local_results_root`).
    """

    #: How long a :meth:`resource_usage` reading is reused. The UI chip and the MCP
    #: tool poll this, so the real sampling (node/pod lists on the cluster) is memoised
    #: for this window — N concurrent clients cost one
    #: sampling per window, not N.
    _USAGE_CACHE_TTL = 10.0

    def __init__(self, store=None, results_dir=None):
        #: Where local campaigns land, when the caller pinned one (``vast serve
        #: --results-dir``). ``None`` leaves it to the service-owned default; see
        #: :meth:`_campaigns_root`.
        self._results_dir = results_dir
        self._campaigns: dict[str, _TrackedCampaign] = {}
        self._lock = threading.Lock()
        #: True once :meth:`shutdown` has begun. Read by work that is worth starting only
        #: if it can finish: a shutdown stops every live campaign, so from a worker's side
        #: it is indistinguishable from an operator's Stop, and the analysis a stop leaves
        #: owed must not be started against storage this process is about to lose.
        self._shutting_down = False
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
        # The jobs with a tap open right now, one relay per job: a second reader shares the
        # first's stream or waits, since two following commands in one container is two probes.
        self._taps: set = set()
        self._taps_guard = threading.Lock()
        # campaign_id -> recorded start time (see _started_at_for). Only known values
        # are held, and a recorded one never changes while the campaign exists, so the only
        # invalidation is delete_campaign's, which drops every per-campaign cache here.
        self._started_at_cache: dict[str, str] = {}
        #: campaign_id -> recorded finish time (see _finished_at_for). Unlike the caches
        #: beside it this one CAN go stale: a re-triggered postprocessing or a re-run
        #: export ends the campaign again and moves its finish time, so
        #: `_dispatch_background` drops the entry when it registers such an operation.
        self._finished_at_cache: dict[str, str] = {}
        #: campaign_id -> recorded results size, or None for a campaign that ended unmeasured
        #: (see _results_bytes_for). Holds only answers read from a terminal record, and is
        #: invalidated with ``_finished_at_cache`` for the same reason.
        self._results_bytes_cache: dict[str, Optional[int]] = {}
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
        # campaign_id -> {(config_name, run_id): (key, (identity, cache key))}, the same
        # contract for a run's scene identity; see _scene_identity for the extra key part.
        self._scene_identity_cache: dict[str, dict] = {}
        # token -> (expiry, staged archive path) for campaign-archive uploads. In memory by
        # design; see the "taking a campaign in" section.
        self._archive_grants: dict[str, tuple[float, Path]] = {}
        self._archive_grants_lock = threading.Lock()
        # (workspace_id, .vast path) -> (the .vast's mtime it was composed from, ConfigNames).
        # See list_config_names; one small entry per .vast ever asked about.
        self._config_names: dict[tuple, tuple[int, ConfigNames]] = {}
        self._config_names_lock = threading.Lock()
        if store is None:
            from robovast.service.workspaces import WorkspaceStore
            store = WorkspaceStore()
        self.store = store
        #: This service's durable event log, opened lazily. See :meth:`_event_log`.
        self._events = None
        self._events_guard = threading.Lock()
        #: Campaigns whose tables a build-all is building now: their tables are in use, so a
        #: clear keeps them and a second build is refused.
        self._table_builds: set = set()
        self._table_builds_lock = threading.Lock()
        #: The exports building in this process; see :mod:`robovast.service.exports`.
        from robovast.service.exports import ExportStore  # pylint: disable=import-outside-toplevel
        self._exports = ExportStore()
        self._sweep_staged_projects()

    # -- the durable record -------------------------------------------------

    def _event_log(self):
        """Where a campaign driven from here says what it did, kept across restarts.

        Addressed by **path**, beside the workspace registry, which is the same place and
        the same fallback the app serving ``/admin/events`` resolves — so the two agree on
        which file the log is without either having to hold the other's handle. A second
        SQLite handle on one file is what that costs, and it costs nothing: every append is
        one small INSERT under a busy timeout.
        """
        with self._events_guard:
            if self._events is None:
                from robovast.service import event_log
                from robovast.service.workspaces import default_workspaces_root
                try:
                    root = Path(self.store.registry.root)
                except Exception:  # noqa: BLE001 - a transport need not have a store
                    root = Path(default_workspaces_root())
                self._events = event_log.EventLog(root / event_log.EVENTS_FILENAME)
            return self._events

    def _notifier(self, campaign_id: str):
        """A campaign's notifier, wired to both sinks.

        The one place the service builds one, so no path can announce a campaign's life to
        a phone and leave nothing behind on the machine that ran it.
        """
        from robovast.execution.notify import Notifier
        return Notifier.from_env(campaign_id, events=self._event_log())

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
                "List them with 'vast workspace list' / list_workspaces(), or "
                "upload one with 'vast workspace init <dir>'.")
        return self._project_for_workspace(workspace_id, vast_path)

    def _campaigns_root(self) -> Path:
        """The single results root every local campaign shares.

        Campaigns are self-contained and **workspace-independent**, so every
        campaign — launched from a workspace *or* the CWD project — lands here and is listed / reconstructed / queried from here. Writing them
        under ``<workspace>/results`` instead would both hide them from the
        service's readers and let ``delete_workspace`` take the campaigns with it.

        ``vast serve --results-dir`` wins when it was given: the caller naming a directory
        on the serve host is the most specific answer there is, and the only one, since no
        project file binds a results dir. Otherwise the precedence lives in
        :func:`~robovast.common.results_root.local_results_root`, shared with the MCP results
        reader so the two cannot disagree about where a campaign is.

        Pure path resolver — the dir is materialized lazily by ``CampaignStore`` on first
        run, so simply asking where campaigns live never creates a stray local directory.

        :class:`ClusterService` uses this too, and for the same campaigns: a cluster
        campaign's home is this directory as well, written by the driver, extended by the
        data plane as pods deliver their outputs, and read by everything downstream.
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
            vasts = sorted(project_dir.rglob("*.vast"))
            # A base another .vast is built on is not itself a campaign to launch, so it does
            # not make the workspace ambiguous. One that nothing extends still does -- an
            # orphan is indistinguishable from a second campaign, and saying so is right.
            bases = _extended_bases(vasts, project_dir)
            vasts = [v for v in vasts if Path(os.path.abspath(str(v))) not in bases] or vasts
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
        if request.from_campaign and request.from_share:
            raise ValueError(
                "a workspace is seeded from a campaign or from the share, not both; "
                "pass one of from_campaign and from_share")
        # The share's archive names the workspace when the caller did not, so the slug is
        # resolved BEFORE the registry entry exists -- a name is chosen once, and a create
        # that would have to be renamed afterwards is one the registry cannot suffix
        # against its own entry.
        name, object_name = request.name, ""
        if request.from_share:
            object_name, slug = self._find_share_workspace(request.from_share)
            name = name or slug
        entry = self.store.registry.create(name)
        if request.from_campaign or object_name:
            try:
                if object_name:
                    self._seed_from_share(entry["workspace_id"], object_name)
                else:
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

        source_dir = self.campaign_dir(campaign_id)
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
        # Lenient for the same reason: `load_config` returns the raw document, and a strict
        # pass would refuse a key the campaign ran without.
        retrigger.reconstruct_project(
            source_dir, project_dir,
            validate_config(load_config(str(vast_path), upgrade=True), strict=False))
        for dropped in retrigger.strip_archived_kubernetes_keys(project_dir / vast_path.name):
            logger.info("workspace %s from campaign %s: removed %s from its config, which is "
                        "not a campaign setting and did not affect the run",
                        workspace_id, campaign_id, dropped)
        missing = retrigger.missing_run_files(source_dir, project_dir)
        if missing:
            raise ValueError(
                f"campaign {campaign_id!r} froze a configuration missing {len(missing)} file(s) "
                f"its own run used: {', '.join(sorted(missing))}. A workspace seeded from it "
                f"would name that campaign's configuration while running a different one, so "
                f"this refuses instead.")

    def _seed_from_share(self, workspace_id: str, object_name: str) -> None:
        """Fill a new workspace with the project files in *object_name* on the share.

        The service downloads with its own credentials -- the caller may be a browser,
        which has none -- into the same staging area an imported campaign uses, and the
        staged file is removed whether or not the extraction succeeds.

        The archive's single top-level directory is stripped, because it names the
        workspace it was exported *from* and this is a different workspace with a
        different id. Anything else is refused rather than flattened: an archive with two
        top-level entries is not one of ours, and guessing which to take would seed a
        project from half of something.
        """
        import tarfile  # pylint: disable=import-outside-toplevel

        from robovast.client.safe_path import \
            UnsafePathError  # pylint: disable=import-outside-toplevel

        provider = self._share_provider()
        project_dir = self.store.registry.project_dir(workspace_id)
        staging = self._staging_dir()
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / f"{secrets.token_urlsafe(16)}.tar.gz"
        try:
            provider.download_archive(object_name, str(staged))
            with tarfile.open(staged, "r:gz") as tar:
                members = tar.getmembers()
                # `./` is not a top-level entry: `tar` writes it for the archive root, so an
                # archive rolled by hand carries one and reading it as the project's
                # directory would nest the whole tree one level down -- silently, which is
                # worse than the refusal below. Stripped the way the campaign side strips it.
                paths = {m: m.name.removeprefix("./").strip("/") for m in members}
                roots = {rel.split("/", 1)[0] for rel in paths.values() if rel}
                if len(roots) != 1:
                    raise ValueError(
                        f"{object_name!r} holds {len(roots)} top-level entries, so it is not a "
                        f"workspace archive; one is the whole project under a single directory")
                root = roots.pop()
                for member in members:
                    rel = paths[member][len(root):].lstrip("/")
                    if not rel:
                        continue
                    # `safe_join` rather than a prefix check: a member may name `..` or an
                    # absolute path, and an archive is the one input that arrives from
                    # somewhere this service does not control.
                    target = safe_join(project_dir, rel)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        # A symlink or a device node in a project tree is not something a
                        # workspace can hold: `/sources` serves files, and a link is a path
                        # into a tree the importing service does not have.
                        continue
                    source = tar.extractfile(member)
                    if source is None:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as fh:
                        shutil.copyfileobj(source, fh)
                    # The executable bit and nothing else: the rest of the mode is whoever
                    # exported it, and a project file arriving unreadable helps no one.
                    if member.mode & 0o111:
                        target.chmod(target.stat().st_mode | 0o111)
        except UnsafePathError as e:
            raise ValueError(
                f"{object_name!r} holds a member that would land outside the workspace "
                f"({e}), so it is not extracted") from e
        finally:
            staged.unlink(missing_ok=True)

    def list_workspaces(self) -> ListWorkspacesResponse:
        busy, preparing = self._workspaces_in_use()
        return ListWorkspacesResponse(workspaces=[
            WorkspaceInfo.model_validate(
                {**e, "running_campaigns": busy.get(e["workspace_id"], []),
                 "preparing_campaigns": preparing.get(e["workspace_id"], [])})
            for e in self.store.registry.list()])

    def _workspaces_in_use(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """Live campaigns per workspace, and the ones still reading it.

        ``({workspace_id: [campaign_id, ...]}, {workspace_id: [campaign_id, ...]})``. Both
        are live state only this process knows, and only while it lasts — which is exactly
        the question a client has to be able to ask before it pushes.

        The two differ because a campaign stops reading its workspace part-way through: it
        composes and stages out of it, and from then on it runs from its own copy (see
        ``CampaignController._on_configs_staged``). Such a campaign is still live and still
        came from here, but this workspace can change without changing it — so it is listed
        as running and not as preparing, and only the latter refuses a push.
        """
        in_use: dict[str, list[str]] = {}
        preparing: dict[str, list[str]] = {}
        with self._lock:
            entries = list(self._campaigns.values())
        for entry in entries:
            if not entry.workspace_id or self._is_done(entry):
                continue
            in_use.setdefault(entry.workspace_id, []).append(entry.campaign_id)
            if not entry.state.project_released:
                preparing.setdefault(entry.workspace_id, []).append(entry.campaign_id)
        return in_use, preparing

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
        root = Path(self.campaign_dir(owner))
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
        return file_view.build_listing(
            FileListing,
            file_address.format_address(namespace, owner, _as_dir(rel)),
            file_view.scan_dir(target, recursive=recursive),
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
        presence: every implementation subclasses this one, so the attribute is never absent
        and a ``getattr(impl, "local_file", None) is None`` check can only ever be False. The
        service holds a campaign's results under its
        own results root (:meth:`campaign_dir`), so the path returned is a file already on
        this host's disk and nothing is fetched to answer.
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
        than calling this, so only the process holding the workspace store can serve it.
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

    def campaign_is_live(self, campaign_id: str) -> bool:
        """Whether work is still happening on *campaign_id* as far as this service knows.

        What the registry says. A campaign it has never heard of is not live: what a
        download of it produces is whatever is on disk, which for a finished campaign of a
        previous service life is all of it.
        """
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is not None:
            return not self._is_done(entry)
        return False

    def _snapshot_facts(self, campaign_id: str) -> dict:
        """What the snapshot marker says about the moment a live campaign was archived.

        Best-effort by construction: this runs to describe a campaign that is moving, so a
        status read that fails describes it no worse than one that succeeds and is stale by
        the time it lands. The marker's value is that it EXISTS; the tallies are a courtesy.
        """
        try:
            snap = self.get_status(campaign_id)
        except Exception:  # noqa: BLE001 - a marker is not worth failing a download over
            return {}
        return {"phase": str(snap.phase),
                "runs_completed": snap.runs.completed, "runs_total": snap.runs.total}

    def campaign_archive_name(self, campaign_id: str) -> str:
        """The file name this campaign's archive is offered under.

        A campaign still running is named ``<id>.incomplete.tar.gz``, and that is the whole
        reason the name is computed rather than composed by each caller: a snapshot has the
        shape of a finished campaign, so once it is sitting in a downloads directory next to
        real ones its name is all that distinguishes it. The browser, the CLI and anyone who
        forwards the file get the same warning without opening it.
        """
        from robovast.execution.share_providers.naming import \
            INCOMPLETE, archive_name  # pylint: disable=import-outside-toplevel
        if self.campaign_is_live(campaign_id):
            return archive_name(campaign_id, INCOMPLETE)
        return f"{campaign_id}.tar.gz"

    # -- the data plane, in-process --
    #
    # The five tar operations are the data plane's (:mod:`robovast.service.data_app`),
    # which reads the results tree directly; this transport delegates to one over its own
    # root so that ``vast serve``'s single process and the cluster's separate data
    # container answer with the same code. What this transport adds is what it knows and
    # the tree does not: whether a campaign is live, from its registry.

    def _data_plane(self):
        from robovast.service.data_app import DataPlane  # pylint: disable=import-outside-toplevel
        return DataPlane(self._campaigns_root())

    def start_serving(self) -> None:
        """Hook: the service is about to answer; adopt what a previous process left.

        Nothing here -- a local campaign's driver dies with the process that held it, so
        there is nothing to adopt. :class:`ClusterService` overrides it: its campaigns are
        Kubernetes Jobs that outlive the driver. Called by ``build_app`` after
        :meth:`bind_auth_token` and before the port is bound, which is the one window in
        which an adopted campaign can mint its pods' token and no launch can race it.
        """

    def bind_auth_token(self, token: str) -> None:
        """The shared secret this service verifies, so it can mint scoped tokens for pods.

        Set by ``build_app`` from the token the gate enforces -- the ephemeral one it mints
        included -- so a token this transport hands a pod is one the gate will honour.
        """
        self._auth_token = token

    def scoped_token(self, scope: str) -> str:
        """A bearer token reaching *scope*'s data routes and nothing else.

        Raises when no secret was bound: a token minted against a guess would be refused
        by every gate, and a pod that cannot deliver its outputs should fail to launch,
        not to upload.
        """
        from robovast.service import auth  # pylint: disable=import-outside-toplevel
        token = getattr(self, "_auth_token", None)
        if not token:
            raise RuntimeError("no auth token bound to this service; build it with build_app")
        return auth.scoped_token(token, scope)

    def campaign_tar_stream(self, campaign_id: str):
        """Tar this host's campaign directory straight into the response.

        ``.cache`` is left out: it is rebuilt from the records. A campaign that is still
        running carries a snapshot marker
        (see ``campaign_archive.iter_campaign_tar``) so what lands cannot be mistaken for
        a finished one; liveness is this transport's knowledge, from its registry.
        """
        live = self.campaign_is_live(campaign_id)
        return self._data_plane().campaign_tar_stream(
            campaign_id, live=live,
            facts=self._snapshot_facts(campaign_id) if live else None)

    def campaign_live(self, campaign_id: str, run: str, tables):
        """A subscription to a run's tables as it records; the data plane's, over this root.

        The watchers behind it are per results root in this process
        (:class:`robovast.service.live.LiveCampaigns`), so the plane made here per call
        reaches the same ones every time.
        """
        return self._data_plane().campaign_live(campaign_id, run, tables)

    def campaign_frame(self, campaign_id: str, run: str, topic: str, t=None):
        """``(stamp, JPEG)`` of a run's camera frame at or before *t*; the data plane's."""
        return self._data_plane().campaign_frame(campaign_id, run, topic, t)

    def campaign_frame_index(self, campaign_id: str, run: str, topic: str):
        """The stamps of a run's image topic's frames; the data plane's."""
        return self._data_plane().campaign_frame_index(campaign_id, run, topic)

    def _workspace_tar_members(self, workspace_id: str):
        """``(add_members, workspace_id)`` for tarring a workspace's project tree.

        Shared by the download and the share export so the two cannot produce different
        archives -- the export is the download, sent somewhere else.
        """
        workspace_id = self.store.registry.require(workspace_id)["workspace_id"]
        project_dir = self.store.registry.project_dir(workspace_id)

        def _add(tar):
            tar.add(str(project_dir), arcname=workspace_id)

        return _add, workspace_id

    def workspace_tar_stream(self, workspace_id: str):
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        add_members, _ = self._workspace_tar_members(workspace_id)
        return campaign_archive.iter_tar(add_members)

    def export_workspace(self, workspace_id: str) -> "ShareWorkspaceArchive":
        """Tar the workspace straight into the share, with no file on the way.

        ``upload_archive_stream`` rather than a staged file: the archive is produced by
        reading the project tree, so writing it to disk first would only add a copy this
        service has to have room for and clean up.
        """
        from robovast.execution import campaign_archive  # pylint: disable=import-outside-toplevel
        from robovast.execution.share_providers.naming import (  # pylint: disable=import-outside-toplevel
            workspace_archive_name, workspace_slug)
        from robovast.service.interface import \
            ShareWorkspaceArchive  # pylint: disable=import-outside-toplevel

        entry = self.store.registry.require(workspace_id)
        provider = self._share_provider()
        slug = workspace_slug(entry.get("name") or "", entry["workspace_id"])
        object_name = workspace_archive_name(slug)
        add_members, _ = self._workspace_tar_members(entry["workspace_id"])
        with campaign_archive.tar_stream(add_members) as fh:
            provider.upload_archive_stream(fh, object_name)
        logger.info("workspace %s exported to the %s share as %s",
                    entry["workspace_id"], provider.SHARE_TYPE, object_name)
        return ShareWorkspaceArchive(slug=slug, object_name=object_name,
                                     url=provider.archive_url(object_name))

    def campaign_inputs_tar_stream(self, campaign_id: str, job_tags: "list[str]",
                                   config_files: "list[tuple[str, str]] | None" = None):
        return self._data_plane().campaign_inputs_tar_stream(campaign_id, job_tags,
                                                             config_files)

    def ingest_campaign_outputs(self, campaign_id: str, stream) -> "OutputsIngested":
        return self._data_plane().ingest_campaign_outputs(campaign_id, stream)

    def staged_tar_stream(self, slot: str, path: str = ""):
        return self._data_plane().staged_tar_stream(slot, path)

    def ingest_staged(self, slot: str, stream) -> "OutputsIngested":
        return self._data_plane().ingest_staged(slot, stream)

    def staged_dir(self, slot: str) -> Path:
        """Where this service stages *slot* for a pod to fetch (see ``DataPlane.staged_dir``)."""
        return self._data_plane().staged_dir(slot)

    def discard_staged(self, slot: str) -> bool:
        return self._data_plane().discard_staged(slot)

    def create_archive_upload(self) -> UploadGrant:
        # At the grant, so a refusal comes before a multi-gigabyte upload rather than after.
        self._admit_storage("take in a campaign archive")
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

        An import deletes the copy it consumed. A failed import keeps it, so a retry with
        force costs no second transfer, and deleting that campaign removes a copy fetched from
        the share (staged under the campaign id). What is left is an upload, staged under its
        token: one never imported -- a refused pre-flight, or a browser that went away between
        the PUT and the POST -- or one whose import failed. Those bytes are a campaign archive,
        so leaving them is leaving gigabytes per attempt on the results volume.

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
        from robovast.execution.share_providers.naming import (  # pylint: disable=import-outside-toplevel
            parse_archive_name, parse_workspace_archive_name)
        from robovast.service.interface import (  # pylint: disable=import-outside-toplevel
            ShareArchive, ShareWorkspaceArchive)

        if not share_type_configured():
            return ShareListing(configured=False)
        provider = self._share_provider()
        archives = []
        workspaces = []
        # One listing, classified by name: the provider reports every archive it holds, and
        # which kind each is, is this layer's question -- a provider that had to answer it
        # would be four implementations of one grammar.
        for object_name, size in provider.list_archives_with_size():
            basename = os.path.basename(object_name)
            parsed = parse_archive_name(basename)
            if parsed is None:
                slug = parse_workspace_archive_name(basename)
                if slug is not None:
                    workspaces.append(ShareWorkspaceArchive(
                        slug=slug, object_name=object_name, size=size,
                        url=provider.archive_url(object_name)))
                continue
            campaign_id, variant = parsed
            archives.append(ShareArchive(
                campaign_id=campaign_id, variant=variant, object_name=object_name,
                size=size, url=provider.archive_url(object_name)))
        # Newest first, keyed on the timestamp inside the campaign id rather than on any
        # modification time: no provider reports one -- every
        # `list_archives_with_size` yields `(name, size)` -- and what a reader of
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
        # By slug, which is the only order a workspace archive has: no timestamp in the
        # name and no modification time from any provider.
        workspaces.sort(key=lambda w: (w.slug, w.object_name))
        return ShareListing(configured=True, share_type=provider.SHARE_TYPE,
                            archives=archives, workspaces=workspaces)

    def import_campaign(self, request: ImportCampaignRequest) -> CampaignRef:
        """Take a campaign in, as a tracked background operation.

        Long by construction -- a share download, a multi-gigabyte extraction, and then
        postprocessing when what arrived was raw -- so it returns a handle rather than
        blocking a request until it is over. Everything that is knowable up front is
        settled here, synchronously, so the caller learns about a bad archive or a name
        collision as an error and not as a background failure five minutes later.
        """
        # An extraction, and for a share import a download first: both write the whole
        # campaign again.
        self._admit_storage("import a campaign")
        campaign_id, fetch, raw = self._resolve_import_source(request)
        note = ("this archive carries no postprocessing record, so postprocessing runs once "
                "it lands -- the import is not over when the extraction is; its tables are "
                "built from its records the first time something names them") if raw else ""

        # Pre-flight, the same shape as preflight_upload_to_share: the authoritative claim
        # (which deletes, under force) happens in the worker once the busy guard has
        # passed, so nothing here can destroy a campaign that is still being worked on.
        if self.campaign_dir(campaign_id).exists() and not request.force:
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
        for object_name, _size in provider.list_archives_with_size():
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

    def _find_share_workspace(self, wanted: str):
        """``(object_name, slug)`` for the workspace archive *wanted* on the share.

        *wanted* is a slug or a full object name, resolved through the share's own
        listing like :meth:`_find_share_archive` -- so a name that is not there is an
        error saying what is, rather than a download of nothing.
        """
        from robovast.execution.share_providers.naming import \
            parse_workspace_archive_name  # pylint: disable=import-outside-toplevel

        provider = self._share_provider()
        available = []
        for object_name, _size in provider.list_archives_with_size():
            basename = os.path.basename(object_name)
            slug = parse_workspace_archive_name(basename)
            if slug is None:
                continue
            available.append(slug)
            if wanted in (slug, basename):
                return object_name, slug
        raise KeyError(
            f"no workspace archive for {wanted!r} on the {provider.SHARE_TYPE} share. "
            f"It holds: {', '.join(sorted(available)[:10]) or '(no workspaces)'}")

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
        """
        from robovast.client.logging_config import (  # pylint: disable=import-outside-toplevel
            add_campaign_log_handler, remove_campaign_log_handler)
        from robovast.service.ingest import (  # pylint: disable=import-outside-toplevel
            blocking_summary, claim_campaign_dir, extract_archive, ingest_campaign,
            read_campaign_id)
        from robovast.service.interface import \
            IngestReport  # pylint: disable=import-outside-toplevel

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
                              force: bool = False, replay: bool = False, skip=(),
                              state=None) -> tuple:
        """Run the campaign's own postprocessing pipeline; return ``(ok, message)``.

        One call for both callers -- the ``run_postprocessing`` retrigger and the chain an
        import starts -- so a raw archive taken in is postprocessed exactly the way asking
        for it later would be. It runs in this process, beside the campaign on the results
        volume.

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
            force=force, replay=replay, skip=list(skip),
            output_callback=stage_output_callback(state, logger.info),
            # A re-run is a tracked campaign like any other while it is going, so
            # ``stop_campaign`` reaches it -- and with this, ends it.
            should_stop=stop_checker(state))

    def run_postprocessing(self, request) -> ActionResult:
        self._admit_storage(f"postprocess {request.campaign_id}")
        campaign_dir = self.campaign_dir(request.campaign_id)

        def work(state):
            from robovast.client.logging_config import (add_campaign_log_handler,
                                                        remove_campaign_log_handler)
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
                    force=request.force, replay=request.replay,
                    skip=list(request.skip or []), state=state)
            finally:
                remove_campaign_log_handler(handler)
            status = record_step_outcome(campaign_dir, postprocessing=(ok, message))
            state.update(postprocessed=status.postprocessed,
                         postprocessing_error=status.postprocessing_error)
            # The recorded phase, not `finished`: `record_step_outcome` preserves how the
            # campaign ended, and a live entry that disagreed with the record would say
            # `finished` until the next service restart said `stopped` -- the same fact,
            # two answers depending on uptime.
            state.set_phase(status.phase)
            # Same one-shot notifier as a re-triggered share: this op runs from disk with
            # no live entry to inherit one from, and it reports on a campaign that ended
            # long ago -- so neither branch is the campaign's terminal message.
            notifier = self._notifier(request.campaign_id)
            if ok:
                notifier.postprocessed()
            elif state.postprocessing_stop_requested:
                # A re-run is a tracked campaign while it lasts, so ``stop_campaign``
                # reaches it and ends it. What comes back then is the operator's own
                # doing, and announcing it as a failure would file that under faults.
                notifier.postprocessing_cancelled(message)
            else:
                notifier.postprocessing_failed(message)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.POSTPROCESSING, work=work)

    def _postprocess_after_import(self, state, campaign_id: str, target: Path) -> None:
        """Chain postprocessing when the imported campaign has none of its own.

        Asked of postprocessing's provenance record, which is what says a campaign has been
        postprocessed. An archive that arrived complete is not postprocessed again: its tables
        are built from its records when something names them, like any campaign's.
        """
        from robovast.execution.status_recovery import (  # pylint: disable=import-outside-toplevel
            record_step_outcome, reconstruct_status_from_disk)

        from robovast.common.campaign_data import \
            campaign_has_derived_data  # pylint: disable=import-outside-toplevel

        if campaign_has_derived_data(target):
            logger.info("%s arrived postprocessed; nothing to compute", campaign_id)
        else:
            logger.info("%s arrived raw; running postprocessing", campaign_id)
            state.set_phase(Phase.POSTPROCESSING)
            ok, message = self._postprocess_campaign(campaign_id, target, state=state)
            record_step_outcome(target, postprocessing=(ok, message))
        # The tracked entry an import runs under is constructed EMPTY -- it exists to make
        # the campaign visible while its bytes arrive -- and it shadows the durable
        # ``outcome.json`` for as long as it lives. Without this an import ends reporting
        # ``0 runs`` and ``postprocessed: false`` over a campaign whose every table is
        # present, and the status advises running postprocessing that would recompute all of
        # it. Both arrival paths need it: the raw one records only the postprocessing
        # verdict, never the run tally, and the postprocessed one records nothing at all --
        # having nothing to *compute* is not having nothing to *report*.
        #
        # Read once the tree is complete and before anything else touches it.
        status = reconstruct_status_from_disk(target)
        state.update(mode=status.mode, runs=status.runs,
                     batches_done=status.batches_done,
                     best_objective=status.best_objective,
                     postprocessed=status.postprocessed,
                     postprocessing_error=status.postprocessing_error,
                     share_error=status.share_error)
        state.set_phase(Phase.FINISHED)

    # -- interface ----------------------------------------------------------

    def _version_info(self, **implementation_fields) -> VersionInfo:
        """The handshake's shared half: what code this is, and where it says it is.

        Each implementation composes its :meth:`version` from this and the fields only it
        knows, so no field here is one an implementation would have to *un-say* (a
        filesystem root that is not the caller's, a build capability it cannot vouch for).
        """
        return VersionInfo(robovast_version=_robovast_version(),
                           code_revision=_code_revision(),
                           package_version=_package_version(),
                           built_at=_build_date(),
                           web_base=self._declared_web_base(),
                           **implementation_fields)

    def _active_campaigns(self) -> list:
        """The campaigns not yet over -- what an upgrade would interrupt."""
        listed = self.list_campaigns(ListCampaignsRequest(limit=100, offset=0)).campaigns
        return [c for c in listed if not is_terminal(c.phase)]

    def _declared_web_base(self) -> str:
        """The origin this deployment declares for its callers, or ``""``.

        One input, from whoever knows: ``setup`` bakes it from the Ingress for a deployed
        service (which is given no RBAC to read its own), and ``serve`` fills it in from
        the address it bound for a service started by hand. Both arrive the same way, so
        there is nothing to reconcile here and no override is needed -- the cluster
        service runs both in-pod and off-cluster through a port-forward.

        ``""`` when nobody named one: unpublished, or bound to a wildcard where which
        address a caller used is not knowable from here.

        The literal rather than an import, like ``default_workspaces_root`` reads
        ``ROBOVAST_WORKSPACES_ROOT``: the writers name it
        (``robovast.service.app.PUBLIC_URL_ENV`` and the cluster service's constant of the
        same name), and importing either from here would drag a serving layer, or the
        cluster service, into the cheapest call in the interface.
        """
        return os.environ.get("ROBOVAST_PUBLIC_URL", "").strip()

    def resource_usage(self) -> ResourceUsage:
        """Backend capacity/usage, cached for ``_USAGE_CACHE_TTL`` seconds.

        The cache (and its lock) live here; subclasses supply the actual reading by
        overriding :meth:`_compute_resource_usage`. Computing under the lock means
        concurrent polls collapse to a single sampling per window.
        """
        with self._usage_lock:
            cached = self._usage_cache
            if cached is not None and time.monotonic() - cached[0] < self._USAGE_CACHE_TTL:
                usage = cached[1]
            else:
                usage = self._compute_resource_usage()
                # Judged here, once, on the readings just taken -- which is
                # what makes the refusal and the meters one measurement.
                usage = usage.model_copy(update={"storage_refusal": storage_refusal(usage)})
                self._usage_cache = (time.monotonic(), usage)
        # Attached outside the cache: a held exec container comes and goes far faster
        # than the sampling window, and a stale "still holding 6 GB" would be worse than
        # not reporting it at all.
        return usage.model_copy(update={
            "exec_container": self._exec_container_state(),
            "query_containers": self._query_container_states()})

    def service_cache(self) -> ServiceCache:
        return self._sweep_caches(clear=False)

    def clear_service_cache(self) -> ServiceCache:
        return self._sweep_caches(clear=True)

    def _sweep_caches(self, clear: bool) -> ServiceCache:
        """Report every cache this service keeps, removing what may go when *clear*.

        The scene cache, and the table cache: the built tables under each campaign's
        ``.cache/``. The results directory is otherwise the campaigns' durable home, not a
        copy of one, so nothing else under it is ever offered.
        """
        report = ServiceCache()
        for sweep in (self._sweep_scene_cache, self._sweep_table_cache):
            swept = sweep(clear)
            report.caches.append(swept.size)
            report.kept.extend(swept.kept)
            report.freed_bytes += swept.freed_bytes
            report.removed_entries += swept.removed
        if clear and report.removed_entries:
            logger.info("cleared %d cache entr%s, %d bytes", report.removed_entries,
                        "y" if report.removed_entries == 1 else "ies", report.freed_bytes)
        return report

    @staticmethod
    def _sweep_scene_cache(clear: bool) -> _Swept:
        from robovast.service import scene_cache  # pylint: disable=import-outside-toplevel
        removed, kept = scene_cache.clear(scene_cache.cache_root(), dry_run=not clear)
        remaining = kept if clear else kept + removed
        return _Swept(
            size=CacheSize(name=SCENE_CACHE, size_bytes=sum(size for _, size in remaining),
                           entries=len(remaining)),
            freed_bytes=sum(size for _, size in removed) if clear else 0,
            removed=len(removed) if clear else 0,
            kept=[KeptCacheEntry(cache=SCENE_CACHE, name=name, size_bytes=size,
                                 reason="a viewer is loading it right now")
                  for name, size in kept])

    def _tables_in_use(self, campaign_id: str) -> str:
        """Why *campaign_id*'s tables may not be removed now, or ``""`` when they may."""
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is not None and not self._is_done(entry):
            return "its campaign is running, and its tables are read and built as it goes"
        with self._table_builds_lock:
            if campaign_id in self._table_builds:
                return "they are being built right now"
        if self._exports.running(campaign_id):
            return "an export is reading them right now"
        return ""

    def _sweep_table_cache(self, clear: bool) -> _Swept:
        from robovast.results_processing.campaign_tables import (  # pylint: disable=import-outside-toplevel
            clear_tables, table_cache_bytes)
        root = self._campaigns_root()
        remaining, removed, freed, kept = [], 0, 0, []
        campaigns = sorted(p for p in root.iterdir() if (p / "campaign.db").is_file()) \
            if root.is_dir() else []
        for campaign_dir in campaigns:
            size = table_cache_bytes(str(campaign_dir))
            if not size:
                continue
            reason = self._tables_in_use(campaign_dir.name) if clear else ""
            if clear and not reason:
                freed += clear_tables(str(campaign_dir))
                removed += 1
                continue
            remaining.append(size)
            if reason:
                kept.append(KeptCacheEntry(cache=TABLE_CACHE, name=campaign_dir.name,
                                           size_bytes=size, reason=reason))
        return _Swept(size=CacheSize(name=TABLE_CACHE, size_bytes=sum(remaining),
                                     entries=len(remaining)),
                      freed_bytes=freed, removed=removed, kept=kept)

    def build_campaign_tables(self, request) -> ActionResult:
        """Build a finished campaign's tables in the background; see the interface."""
        from robovast_decode.build import available_tables  # pylint: disable=import-outside-toplevel
        from robovast_decode.layout import decoder_config  # pylint: disable=import-outside-toplevel

        campaign_id = request.campaign_id
        campaign_dir = self.campaign_dir(campaign_id)
        if not (campaign_dir / "campaign.db").is_file():
            raise FileNotFoundError(f"no campaign {campaign_id!r}")
        busy = self._tables_in_use(campaign_id)
        if busy:
            raise RuntimeError(f"not building {campaign_id}'s tables now: {busy}")
        self._admit_storage(f"build the tables of {campaign_id}")
        tables = list(request.tables) or sorted(
            available_tables(str(campaign_dir), decoder_config(str(campaign_dir))))
        with self._table_builds_lock:
            if campaign_id in self._table_builds:
                raise RuntimeError(f"{campaign_id}'s tables are already being built")
            self._table_builds.add(campaign_id)
        self._archive_repeatable_sections(campaign_id)

        def work():
            from robovast.client.logging_config import (  # pylint: disable=import-outside-toplevel
                add_campaign_log_handler, remove_campaign_log_handler)
            from robovast.results_processing.campaign_tables import \
                build_tables  # pylint: disable=import-outside-toplevel
            handler = None
            try:
                handler = add_campaign_log_handler(
                    str(campaign_dir / "_execution" / "tables.log"))
                logger.info("Building %d table(s) of %s: %s", len(tables), campaign_id,
                            ", ".join(tables))

                def progress(done, total):
                    if done == total or done % 10 == 0:
                        logger.info("  built %d/%d run(s)", done, total)

                problems = build_tables(str(campaign_dir), tables, progress=progress)
                for problem in problems[:50]:
                    logger.warning("  %s", problem)
                if len(problems) > 50:
                    logger.warning("  ... %d more", len(problems) - 50)
                logger.info("Tables of %s built%s", campaign_id,
                            f"; {len(problems)} could not be built for some runs"
                            if problems else "")
            except Exception:  # pylint: disable=broad-except
                logger.exception("Building the tables of %s failed", campaign_id)
            finally:
                remove_campaign_log_handler(handler)
                with self._table_builds_lock:
                    self._table_builds.discard(campaign_id)

        threading.Thread(target=work, name=f"tables-{campaign_id}", daemon=True).start()
        return ActionResult(ok=True, message=(
            f"building {len(tables)} table(s) of {campaign_id} for every run; progress is in "
            "the campaign log's TABLES section. Not needed for any answer: each table is "
            "built the first time something names it."))

    def create_export(self, campaign_id: str, request) -> ExportRef:
        """Start an export of *campaign_id*; see the interface.

        The plan is made here, before anything starts: the catalog is read (building
        nothing), a table the campaign does not have refuses the request, and the reserve
        is checked; only then does the export's directory exist and its thread run.
        """
        from robovast.service.exports import plan_tables  # pylint: disable=import-outside-toplevel
        from robovast_data import Engine, Scope  # pylint: disable=import-outside-toplevel

        campaign_dir = self.campaign_dir(campaign_id)
        if not (campaign_dir / "campaign.db").is_file():
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        if self.campaign_is_live(campaign_id):
            raise RuntimeError(f"not exporting {campaign_id} now: it is still running, and "
                               "its records and tables are changing as it goes")
        tables = plan_tables(Engine([Scope(str(campaign_dir))], **query_limits()).catalog(),
                             request.tables)
        self._admit_storage(f"export {campaign_id}")
        logger.info("Exporting %s: %d table(s) as %s, bags %s, records %s", campaign_id,
                    len(tables), request.format, request.bags, request.records)
        return self._exports.start(campaign_dir, campaign_id, request, tables)

    def get_export_status(self, campaign_id: str, export_id: str) -> ExportStatus:
        """Where an export has got to; see the interface."""
        campaign_dir = self.campaign_dir(campaign_id)
        if not campaign_dir.is_dir():
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        return self._exports.status(campaign_dir, campaign_id, export_id)

    def export_tar_stream(self, campaign_id: str, export_id: str):
        """A finished export's file, from the data plane over this root."""
        return self._data_plane().export_tar_stream(campaign_id, export_id)

    def clear_campaign_tables(self, campaign_id: str) -> CampaignTablesCleared:
        """Remove one campaign's built tables; see the interface."""
        from robovast.results_processing.campaign_tables import \
            clear_tables  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        if not (campaign_dir / "campaign.db").is_file():
            raise FileNotFoundError(f"no campaign {campaign_id!r}")
        busy = self._tables_in_use(campaign_id)
        if busy:
            raise RuntimeError(f"not clearing {campaign_id}'s tables now: {busy}")
        return CampaignTablesCleared(campaign_id=campaign_id,
                                     freed_bytes=clear_tables(str(campaign_dir)))

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

        Separate from the one above because they are separate occupancy: a service holding
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

    @abstractmethod
    def _compute_resource_usage(self) -> ResourceUsage:
        """One reading of the service's capacity and load, uncached; see :meth:`resource_usage`
        for the TTL cache over it.
        """

    @abstractmethod
    def _scenario_job_tally(self) -> "tuple[int, int]":
        """``(running, pending)`` scenario jobs over this service's live campaigns, for
        :meth:`resource_usage`.
        """

    # -- launch hooks (each implementation answers these for itself) ---------
    #
    # create_campaign below is shared by every implementation. They differ only in these
    # hooks — the driver loop, its worker thread, the status/outcome bookkeeping and the
    # postprocess tail are identical.

    @abstractmethod
    def _guard_new_campaign(self) -> None:
        """Refuse a launch this service cannot admit right now, as a ``RuntimeError`` (409).

        A single-flight implementation refuses a second campaign; one with a queue admits.
        """

    def _admit_storage(self, action: str) -> None:
        """Refuse new disk-consuming work while free space is below the reserve.

        Called first by every operation that takes on work of unknown size -- a campaign, a
        rerun, an image build, an archive upload or import, postprocessing -- and by none that
        continues work already accepted: resuming a live campaign after a restart, stopping
        or deleting one. Refusing those would abandon running work, or refuse the very thing
        that frees space. See :mod:`robovast.service.storage_reserve`.

        Reads the same cached reading ``/usage`` serves, so what refuses here is what the
        meters show. With no reserve configured it reads nothing at all. A reading that
        fails is not judged, for the reason an unmeasured meter is not a full disk: refusing
        every launch because the capacity could not be read would make a campaign depend on
        a permission it never needed. The failure is logged, and ``/usage`` reports it.
        """
        if reserve_disabled():         # raises on a malformed value, naming the variable
            return
        try:
            refusal = self.resource_usage().storage_refusal
        except Exception as e:  # noqa: BLE001 - see above: unjudged, not refused
            logger.warning("free space could not be read before trying to %s, so the "
                           "reserve was not applied: %s", action, e)
            return
        if refusal:
            clearable = self._clearable_cache_bytes()
            if clearable >= _CACHE_WORTH_CLEARING_BYTES:
                raise InsufficientStorageError(
                    f"Cannot {action}. {refusal} Clearing the service cache frees "
                    f"{clearable / 1000 ** 3:.0f} GB ('vast service cache --clear'); deleting "
                    "campaigns no longer needed frees more.",
                    next_step="vast service cache --clear")
            raise InsufficientStorageError(
                f"Cannot {action}. {refusal} Delete campaigns no longer needed, then retry.")

    def _clearable_cache_bytes(self) -> int:
        """What clearing the service cache would free now; 0 when that cannot be measured.

        Asked only once a refusal is certain, because it walks the caches. A measurement that
        fails costs the hint, never the refusal it would have been attached to.
        """
        try:
            report = self.service_cache()
        except Exception:  # noqa: BLE001 - see above
            logger.debug("could not measure the service cache for a refusal", exc_info=True)
            return 0
        held = sum(part.size_bytes for part in report.caches)
        return max(0, held - sum(entry.size_bytes for entry in report.kept))

    @abstractmethod
    def _build_backend(self, state):
        """The :class:`~robovast.execution.backends.ExecutionBackend` that runs this
        service's jobs, over *state*.
        """

    def _register_scheduling(self, campaign_id: str, request) -> None:
        """Tell the queue how to treat this campaign. Nothing to do where there is no queue.

        An addition rather than a policy, which is why it is concrete here: an
        implementation that queues campaigns seeds its queue, and one that runs one at a time
        has nothing to seed -- :meth:`_admit_scheduling` has already refused anything but the
        default by the time a launch reaches here.
        """

    @abstractmethod
    def _queues_campaigns(self) -> bool:
        """Whether this implementation queues campaigns against each other, so a rank and a
        hold mean something.

        One declaration per implementation, read by both :meth:`version` (as
        ``can_schedule``) and its own :meth:`_admit_scheduling`, so what a client is
        *offered* and what the service *accepts* cannot disagree -- a second answer to this
        question would sooner or later offer an entry the service then refuses, or hide one
        it would accept. A property of the implementation, fixed when the service starts,
        not of how busy it is.
        """

    @abstractmethod
    def _admit_scheduling(self, request) -> None:
        """Admit or refuse a rank or a hold at launch, for this implementation.

        The default asks for nothing and is always admitted, which is what keeps a launch
        that never mentions scheduling working. An implementation with no queue raises
        :class:`UnsupportedOperation` for anything else, before a campaign directory
        exists.
        """

    @abstractmethod
    def _run_options(self, request) -> "RunOptions":  # noqa: F821
        """The :class:`~robovast.execution.backends.RunOptions` a launch *request* means for
        this service.
        """

    def _campaign_context(self, campaign_id: str, project, should_stop=None, options=None):
        """Per-campaign setup entered *inside* the worker thread.

        A context manager, so anything thread-scoped is established where the composition
        that reads it runs, and torn down when the campaign ends. Today that is only the
        aux-container runner, which is why this delegates: a campaign is one *span* over
        which the implementation provides one, not the only span. Anything genuinely per-campaign
        belongs here rather than in :meth:`_aux_runner_context`, which preview also enters.

        *should_stop* is the campaign's own stop flag as a predicate, for the waits inside
        the span that are long enough for an operator to give up on — a helper image being
        pulled, most of all. A preview has no campaign and passes none.

        *options* are the campaign's :class:`~robovast.execution.backends.RunOptions`: which
        image project it resolves from, and the digests it has fixed or replays. The images
        its auxiliary containers run are the campaign's, so they are fixed and recorded like
        every other image it runs. A preview has no campaign and passes none.
        """
        return self._aux_runner_context(campaign_id, project, should_stop=should_stop,
                                        options=options)

    @abstractmethod
    def _aux_runner_context(self, tag: str, project, *, hold: bool = False,
                            should_stop=None, options=None):
        """A context yielding the runner a variation plugin's auxiliary container is started
        through, or ``None`` where each call starts an ephemeral one.

        *options* are given for a campaign's span only: its auxiliary containers and the
        sidecar beside them then run digests the campaign's launch record holds, fixed before
        the first pod on a fresh launch and taken from the record on a replay.
        """

    def _record_campaign_failure(self, campaign_id, results_dir, state, exc, backend):
        """Durably record a failed campaign. Local writes ``_execution/outcome.json``."""
        self._record_outcome(campaign_id, results_dir, state)

    def create_campaign(self, request: CreateCampaignRequest) -> CampaignRef:
        # Before anything is resolved or created, so a refusal leaves nothing behind.
        self._admit_scheduling(request)
        self._admit_storage("start a campaign")
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
                                                find_migration_markers, read_vast,
                                                upgrade_config_file)

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

        markers = find_migration_markers(read_vast(staged))
        logger.warning(
            "materialised %s as work order in workspace %s: %d unresolved marker(s). It will not "
            "validate until each is resolved, which is deliberate.",
            campaign_id, info.workspace_id, len(markers))
        return WorkOrder(
            workspace_id=info.workspace_id, config_path=str(staged),
            reached=reached, capability=capability,
            markers=[MigrationMarker(path=where, reason=reason) for where, reason in markers])

    @abstractmethod
    def _image_labels(self, ref: str) -> "dict | None":
        """Every label *ref* carries per its registry, or ``None`` when it could not be read."""

    @abstractmethod
    def _image_build_lock(self, ref: str) -> dict:
        """The build lock inside *ref* per its registry, ``{}`` when it has none or cannot be
        read."""

    def check_retrigger(self, campaign_id: str) -> RetriggerReport:
        """See the interface. A thin adapter over :func:`robovast.service.retrigger.check`.

        Same split as :meth:`retrigger_campaign`: the module decides everything about the
        source, and what belongs here is only which directory the service reads it from.
        """
        from robovast.service import retrigger

        report = retrigger.check(str(self.campaign_dir(campaign_id)), campaign_id,
                                 image_labels=self._image_labels,
                                 build_lock=self._image_build_lock)
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

    @staticmethod
    def _admit_retrigger(report: dict, force: bool) -> None:
        """Refuse a launch the pre-flight blocks on, unless the caller asked for it anyway.

        On the operation rather than in each client, because a check a client may skip is not
        a gate: the campaign whose recorded image is outside this host's protocol window then
        fails in the backend, minutes after a launch that looked accepted. ``retrigger.check``
        stages nothing and starts no container, so the launch pays a few record reads for it.

        ``force`` is the caller's judgement about an axis they understand. It is logged
        rather than carried onto the campaign, so the service's own log is where "this one was
        launched past a refusal" can be read back.
        """
        from robovast.service.retrigger import RetriggerRefused

        blocking = report["blocking"]
        if not blocking:
            return
        axes = ", ".join(blocking)
        if force:
            logger.warning("retrigger of %s forced past a blocking pre-flight: %s",
                           report["campaign_id"], axes)
            return
        raise RetriggerRefused(
            f"cannot retrigger {report['campaign_id']!r}: its pre-flight blocks on {axes}.\n"
            + "\n".join(f"  {name}: {report['axes'][name]['detail']}" for name in blocking)
            + f"\n  Fix what the detail names, or re-run it anyway with force "
              f"('vast campaign rerun {report['campaign_id']} --force', or force on the "
              f"request). 'vast campaign rerun --check {report['campaign_id']}' reports "
              f"every axis, including the ones that pass.")

    def retrigger_campaign(self, campaign_id: str, force: bool = False) -> CampaignRef:
        """Launch a new campaign from *campaign_id*'s own records (see the interface).

        A thin orchestrator: :mod:`robovast.service.retrigger` decides everything about the
        source, and :meth:`_launch_campaign` runs it. What belongs here is only the ordering
        that needs the transport — which directory the service reads the source from, the
        single-flight guard, and making sure a refusal leaves nothing staged behind.
        """
        from robovast.service import retrigger
        from robovast.service.interface import DESCRIPTION_MAX_LEN
        source_dir = str(self.campaign_dir(campaign_id))
        self._admit_retrigger(retrigger.check(source_dir, campaign_id,
                                              image_labels=self._image_labels,
                                              build_lock=self._image_build_lock),
                              force)
        # Before `prepare`, which stages the source's tree: a refusal leaves nothing behind.
        self._admit_storage(f"re-run {campaign_id}")
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
                origin=self._retrigger_origin(campaign_id, plan.config_migration),
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
        self._notifier(campaign_id).retriggered(ref.campaign_id)
        return ref

    def _retrigger_origin(self, source_id: str, config_migration: dict) -> CampaignOrigin:
        """The origin to record for a re-run of *source_id*, staged as *config_migration* says.

        Built here rather than in :mod:`robovast.service.retrigger`, which deliberately
        does not import the service interface.

        The workspace fields are **copied from the source's own origin**, so they keep
        naming the workspace the configuration came from originally -- and a re-run of a
        re-run inherits it transitively, because the parent's record already holds it.
        Copied rather than resolved by walking ``from_campaign`` later, because the listing
        is paginated (a reader may not hold the parent at all) and because a parent is
        routinely deleted -- lineage that evaporates with it is lineage nobody can rely on.

        The config version comes from the plan that staged the tree, so the record states
        the version this run actually read rather than the one the source's frozen ``.vast``
        would migrate to if it were staged again today.

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
            config_path=parent.config_path if parent else "",
            config_version_from=config_migration["from"],
            config_migration_steps=config_migration["steps"])

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

        # The raw mapping as well as the validated model: the launch advisories read the
        # mapping, which is what the author wrote.
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
        entry = _TrackedCampaign(campaign_id, results_dir, state,
                               description=request.description,
                               workspace_id=request.workspace_id,
                               created_by=request.created_by,
                               origin=target.origin)
        runs = request.runs if request.runs and request.runs > 0 else None
        # Before the worker exists, so a campaign launched demoted or held is already ranked
        # when its first batch reaches the queue -- seeding it later would let one batch be
        # admitted at the ordinary rank first.
        self._register_scheduling(campaign_id, request)
        options = self._run_options(request)
        # Who ends the campaign: the builders' finish tail, which is outermost because
        # nothing of the campaign happens in this process after it returns -- the
        # implementation's driver chains postprocessing inside it.
        options.finalize_phase = True

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
            backend = None
            # Built here, not left to the builder, because the worker is
            # the campaign's outermost scope (see options.finalize_phase): the builder
            # returns while postprocessing is still to come, so the one notification
            # that says "this campaign is over" has to be sent from out here.
            notifier = self._notifier(campaign_id)

            def _record_stop():
                """Persist the stop and run the analysis the finished batches are owed.

                Persisted so ``stopped`` survives a service restart instead of
                reconstructing as an ambiguous ``finished``. The batches that DID finish
                are complete on disk, so their analysis is still owed:
                ``controller._finish_campaign`` cannot run it on this path -- on Ctrl+C the
                storage tunnel dies with the controller's process group -- but the service
                can, and ``_record_campaign_stopped`` draws exactly that line ("succeeds
                for a Stop-button stop; on Ctrl+C the tunnel is already gone"). It ends
                back at ``stopped``: how the campaign ended is not this step's to restate.

                Skipped while shutting down, for that same tunnel reason; skipped for a
                campaign that asked for no postprocessing; and skipped for one that was
                stopped before any run existed, which has nothing to derive -- on the
                cluster the pass is a Job of its own, a pod scheduled to read an
                empty campaign.
                """
                logger.info("Campaign %s stopped by request", campaign_id)
                # Here rather than only in the controller: a stop that lands before the
                # runs begin -- staging, the plugin install, the image wait -- is raised by
                # the driver itself, and the outcome recorded below is written from the
                # phase, so a campaign stopped while starting would persist as ``starting``
                # and reconstruct after a restart as something that never ended.
                state.set_phase(Phase.STOPPED)
                self._record_campaign_stopped(campaign_id, results_dir, state, backend)
                if not request.postprocess or self._shutting_down:
                    return
                if not campaign_has_runs(Path(results_dir) / campaign_id):
                    logger.info("Campaign %s was stopped before any run; there is nothing "
                                "to postprocess", campaign_id)
                    return
                self._postprocess(campaign_id, results_dir, state, entry,
                                  ends_at=Phase.STOPPED)

            try:
                # How the campaign was ASKED FOR, recorded next to it. Here rather than in the
                # request handler for the same reason as the line above: a record written later
                # would be missing from exactly the campaigns someone comes looking at. A replay
                # states the digests it replays from this first write on; a fresh launch adds
                # each as it fixes it, before the pod that runs it exists.
                self._record_launch(campaign_id, results_dir, request,
                                    images=target.pinned_images)
                if target.materialize is not None:
                    state.set_phase(Phase.STARTING, stage="staging the project")
                    target.materialize()
                    # Cleared explicitly: set_phase leaves the stage alone when passed None, so
                    # without this "staging the project" is still the reported stage for the
                    # whole run — and, on a campaign that fails later, names the wrong step.
                    state.set_phase(Phase.STARTING, stage="")
                    state.raise_if_stopped("stopped while staging the project")
                if target.pinned_images is not None:
                    # Not a build, but still ``_build_specs_for``: it is what installs the
                    # campaign's ``plugins:`` into the project dir, which the cluster service's
                    # _campaign_context reads before anything else. (``_install_plugins`` in
                    # run_batch_campaign runs later, too late for that.) Cheap and pure — it
                    # never touches the build context, which is absent here by definition.
                    self._build_specs_for(target, campaign_config,
                                          should_stop=stop_checker(state, scope=STOP_RUNS))
                    # Every image the replay runs, and the flag that makes anything else a
                    # refusal: composition, the aux pods and the batch runner read these and
                    # resolve nothing from the environment.
                    options.images = dict(target.pinned_images.containers)
                    options.sidecar_image = target.pinned_images.sidecar
                    options.aux_images = dict(target.pinned_images.aux)
                    options.images_fixed = True
                    state.raise_if_stopped("stopped while installing the campaign's plugins")
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
                        image_project_tag=options.image_project_tag,
                        should_stop=stop_checker(state, scope=STOP_RUNS))
                    for build in builds:
                        self._await_build_image(build.build_id, state, campaign_root)
                    if builds:
                        options.images = self._resolve_built_images(
                            target, campaign_config,
                            image_project=options.image_project,
                            image_project_tag=options.image_project_tag,
                            should_stop=stop_checker(state, scope=STOP_RUNS))
                state.set_phase(Phase.STARTING)
                # The last boundary before the backend's own pre-flight -- a project push, a
                # registry read, the object-store tunnel -- none of which reads the flag.
                state.raise_if_stopped("stopped before the campaign's runs began")
                with self._campaign_context(campaign_id, target,
                                            should_stop=lambda: state.stop_requested,
                                            options=options):
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
                # phase "stopped". Not a failure — no error, no traceback.
                _record_stop()
                return
            except Exception as e:  # noqa: BLE001 - surfaced via status
                if state.stop_requested and not self._shutting_down:
                    # A stop whose first visible effect was something else failing: a
                    # composition worker killed mid-line, an auxiliary container removed
                    # under the command it was running, a backend pre-flight whose connection
                    # went with them. The exception describes the consequence and the flag
                    # describes the cause, and filing this as a failure would send whoever
                    # reads it after a bug in a step that was working. The same rule
                    # ``CampaignController.run`` applies to the loop's own exceptions.
                    logger.info("Campaign %s stopped by request (surfaced as %s)",
                                campaign_id, e)
                    _record_stop()
                    return
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
                # Analysis postprocessing — what the eval viewer and
                # `query_campaign_data_sql` read — is chained by the backend's driver. This
                # covers a stop that landed BETWEEN batches, where the loop ends cleanly
                # rather than raising: the controller's own chain skips itself whenever a
                # stop was requested, so nothing would postprocess at all and a search
                # stopped at a batch boundary would lose the analysis of every batch it
                # completed.
                # Ends at `finished` either way, which is not this branch's choice: a stop
                # seen at a batch boundary is an ordinary stopping criterion to the loop
                # (`stop_kind="external"`), so the campaign really did finish. Only the
                # raising path -- the run cut mid-batch -- ends `stopped`.
                stopped_runs = state.stop_requested and not self._shutting_down
                if request.postprocess and stopped_runs:
                    self._postprocess(campaign_id, results_dir, state, entry)
            finally:
                # The campaign's outermost scope, so the campaign ends here — on every
                # path, including the `return`s above and a campaign that asked for no
                # postprocessing at all. Without this the run leaves the phase at
                # `finishing` and every waiter blocks until its timeout.
                end_campaign(campaign_id, state, notifier)

        thread = threading.Thread(
            target=_worker, name=f"robovast-{campaign_id}", daemon=True)
        entry.thread = thread
        thread.start()
        logger.info("Started campaign %s (search=%s)", campaign_id, is_search)
        # Joined rather than first-wins: independent advisories can all apply to one
        # launch, and dropping one would make it depend on another being absent.
        notes = [n for n in (_no_timeout_note(raw_config),) if n]
        return CampaignRef(campaign_id=campaign_id, note=" ".join(notes))

    # -- image builds -------------------------------------------------------

    @property
    @abstractmethod
    def _images(self):
        """The :class:`~robovast.service.image_store.ImageBuildStore` this service builds into
        and resolves from.
        """

    def _build_specs_for(self, project, campaign_config, image_project=None,
                         image_project_tag=None, should_stop=None):
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
        # *should_stop* because this is a pip install: minutes on a campaign whose
        # plugins are not cached, and the first thing a stop requested at launch meets.
        ensure_workspace_plugins(str(project_dir),
                                 getattr(campaign_config, 'plugins', None),
                                 position="append", should_stop=should_stop)
        # base_dir also lets a backend named as a `<file>.py:<Class>` ref next to the
        # .vast resolve here -- the documented escape hatch, which silently did not work
        # on this path because nothing passed the directory it resolves against.
        specs = extract_build_specs(campaign_config, base_dir=str(project_dir),
                                    image_project=image_project,
                                    image_project_tag=image_project_tag)
        if not specs:
            return {}, None
        return specs, project_dir

    @abstractmethod
    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None, should_stop=None) -> list:
        """Submit (or join) this service's build of every image the campaign needs; return their
        refs, without waiting. :meth:`_await_build_image` does the waiting.
        """

    @abstractmethod
    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None, should_stop=None) -> dict:
        """The concrete image ref to pin per container once the builds are done.
        """

    #: Poll cadence of :meth:`_await_build_image`. Each tick is one build-status read plus
    #: one build-log read, so it is also how often the campaign's ``build.log`` grows.
    _BUILD_POLL_SECONDS = 2.0

    def _await_build_image(self, build_id: str, state: ControllerState,
                           campaign_root: str) -> None:
        """Wait for *build_id*, teeing its log into the campaign's ``_execution/build.log``.

        ``get_image_build_status`` and ``get_image_build_log`` are interface operations,
        so the in-cluster BuildKit Job is waited on by the base's loop.

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

    # -- container exec (diagnostic; produces no campaign) ------------------

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
                self._exec_mgr = ContainerExecManager(self._exec_runner())
                self._reap_stray_exec_container()
            return self._exec_mgr

    @abstractmethod
    def _exec_runner(self):
        """The :class:`~robovast.service.container_exec.ExecRunner` a diagnostic exec runs on:
        a container beside this process, or a pod.
        """

    def _reap_stray_exec_container(self) -> None:
        """Remove every exec workload left behind by a previous service process.

        Through the runner's own sweep (:meth:`_exec_runner`) rather than removing one fixed
        name: the user slot has a fixed name, but a query workload's carries a hash of the
        identity it was started for, and nothing persists those across a restart.
        """
        try:
            removed = self._exec_runner().sweep_held()
            if removed:
                logger.info("removed %d stray exec workload(s) from a previous run: %s",
                            len(removed), ", ".join(removed))
        except Exception as e:  # noqa: BLE001 - an unreachable driver must not break startup
            logger.debug("could not check for stray exec workloads: %s", e)

    def _exec_vast_file(self, request) -> str:
        """The ``.vast`` this request runs, from whichever source it named.

        A campaign's ``_config/`` *is* a project, so the two sources differ only here.
        Empty for the image-family source, which names no project at all.
        """
        from robovast.service.container_exec import vast_in_dir
        if getattr(request, "image_family", ""):
            return ""       # an image, not a project: nothing to resolve a .vast from
        if request.campaign_id:
            config_dir = self.campaign_dir(request.campaign_id) / "_config"
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
        vast_file = self._exec_vast_file(request)
        spec, _campaign_data, limit_s, limit_source = stage(
            # The staged entrypoint is rendered for this exec; a campaign's rendered
            # entrypoint is never copied across.
            vast_file, request.config_name,
            cluster=self.IMPLEMENTATION == "cluster",  # pylint: disable=no-member
            command=request.command, archived=bool(request.campaign_id))
        # Ownership of spec's staging tree passes to the manager: a held container mounts
        # it as /config, so it must outlive this call. On the way *in*, though, a failure
        # before that handover is ours to clean up.
        try:
            found = self._resolve_exec_image(
                vast_file, request.container or None,
                campaign_id=request.campaign_id or "",
                image_family=getattr(request, "image_family", ""))
            spec.image = found.ref
            # What the caller is told the container is. Never `found.ref`: that is
            # registry-qualified, and this value is reported back.
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
        # The workspace's CONTENTS belong in the identity, and this is the only member
        # of the tuple that is not already immutable. A campaign is frozen once it
        # starts, so its id is an identity; a workspace is editable by definition, and the
        # project reaches a held container exactly once, when the container is created
        # (an init container mirrors it in). So a reused container answers from the tree
        # it was staged from, and an edited workspace is answered for by the bytes it no
        # longer holds -- a validate that keeps reporting the problem its own fix already
        # removed.
        identity = (request.workspace_id, request.campaign_id,
                    getattr(request, "image_family", ""),
                    request.config_path, request.config_name, spec.image,
                    _workspace_sha(spec))
        started = time.monotonic()
        query = bool(getattr(request, "query", False))
        out = self._exec_manager.run(spec, limit_s,
                                    keep_alive=request.keep_alive,
                                    identity=identity, query=query,
                                    fresh=bool(getattr(request, "fresh", False)))
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

        A built image must already exist on the service's store: building implicitly would
        turn a seconds-long check into a multi-minute one the caller never asked for.
        """
        return self._resolve_exec_image(vast_file, container, campaign_id).ref

    def _resolve_exec_image(self, vast_file: str, container: "str | None" = None,
                            campaign_id: str = "",
                            image_family: str = "") -> "ImageRef":  # noqa: F821
        """The exec image as an :class:`~robovast.service.image_store.ImageRef`.

        Split from :meth:`_exec_image` because two callers want different halves of one
        resolution: a container is started from ``.ref``, while :meth:`resolve_image` hands
        ``.identity`` to a client and must not leak the concrete form. Resolving twice to
        get the two would be two chances to disagree.

        The branch is on the **config source**:

        * a *campaign* has already run, and recorded which image each role ran on, so the
          diagnostic runs those exact bytes — see :func:`campaign_role_image`. Re-deriving
          a content hash from the campaign's frozen ``_config/`` cannot work anyway: that
          snapshot holds the ``.vast``, the scenario and the run files, not the build
          inputs, so every source dir and workspace wheel hashes as a bare requirement and
          the hash differs from the one the build produced.
        * a *workspace* project is asked of the image store, which is the service's own
          answer to "what is this called here, and is it here".
        * an *image family* member skips all of it: the ref names the image directly.
        """
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.common.containers import plan_containers
        from robovast.common.execution import resolve_family_image, resolve_robovast_image
        from robovast.common.simulators import apply_backend
        from robovast.service.image_store import ImageRef

        if image_family:
            # No project, so no container plan and no build: the family ref IS the answer.
            # A resolved family image carries no build of ours and no registry the caller
            # would have to know about, so it is its own identity, as a declared one is.
            resolved = resolve_family_image(image_family, role="image-family exec")
            return ImageRef(ref=resolved, identity=resolved, build_id="")

        # Validate rather than reading the raw mapping: the build specs come off the
        # *model*, so handing this path a plain dict yields "no build section" for every
        # project that has one.
        # Strict for a workspace, lenient for a campaign: the split
        # :func:`validate_config` documents. A workspace is editable, so an undeclared key
        # there is a misspelling worth refusing; an archived campaign cannot be edited, so
        # refusing it only makes a finished run unreadable.
        campaign_config = (validate_config(load_config(vast_file, upgrade=True), strict=False)
                           if campaign_id else validate_config(load_config(vast_file)))
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
                                  os.path.dirname(os.path.abspath(vast_file)),
                                  recording=campaign_config.recording)
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

        :meth:`_resolve_image_digest` is the hook for the tag-only
        campaigns that predate per-role digests: a deliberate refusal on the cluster
        (guessing there would name bytes no node can pull).
        """
        from robovast.common.campaign_data import campaign_role_image
        from robovast.service.image_store import ImageRef
        image = campaign_role_image(self.campaign_dir(campaign_id), role,
                                    resolve_digest=self._resolve_image_digest)
        return ImageRef(ref=image, identity=image, build_id="")

    def _refuse_unbuilt(self, container_name: str, build_id: str) -> "NoReturn":  # noqa: F821
        """Refuse an exec whose image is not on the store, saying which state it is in.

        Never overridden: ``get_image_build_status`` is an interface operation (the
        cluster's even recovers an untracked build from its Job), so the classification has
        one implementation rather than several that drift — the same argument
        ``_await_build_image`` already makes for the build wait loop.
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
        boundary (it keys the per-image catalog cache and is reported to the caller), and
        the concrete form is registry-qualified.
        """
        from robovast.service.interface import ImageResolution
        vast_file = self._exec_vast_file(request)
        found = self._resolve_exec_image(
            vast_file, request.container or None,
            campaign_id=request.campaign_id or "",
            image_family=getattr(request, "image_family", ""))
        return ImageResolution(image=found.identity)

    def _postprocess(self, campaign_id, results_dir, state, entry,
                     ends_at=Phase.FINISHED):
        """Run analysis postprocessing for a just-ended local campaign.

        Advances the phase ``... → postprocessing → *ends_at*`` and generates the
        campaign's derived data; a failure surfaces via status.

        *ends_at* is **how the campaign ended**, not a result of this step: a campaign
        whose runs were stopped postprocesses the batches that did finish and then goes
        back to ``stopped``. The same rule
        :func:`~robovast.execution.status_recovery.record_step_outcome` applies on the
        re-run path -- a step that runs after a campaign has ended does not get to restate
        how it ended -- so this is that rule's second caller rather than a second answer.
        """
        from robovast.client.logging_config import (add_campaign_log_handler,
                                                    remove_campaign_log_handler)

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
            # Through the seam every other caller uses, so the implementation decides HOW
            # to postprocess. Running the pipeline here instead would run it in this process
            # where the cluster postprocesses in a pod of its own.
            ok, message = self._postprocess_campaign(
                campaign_id, Path(results_dir) / campaign_id, state=state)
            if ok:
                from robovast.results_processing.postprocessing import \
                    campaign_defines_postprocessing
                if campaign_defines_postprocessing(
                        str(Path(results_dir) / campaign_id)):
                    state.update(postprocessed=True)
                state.update(postprocessing_error=None)
                state.set_phase(ends_at)
            else:
                # The runs are over — a postprocessing failure does not change how the
                # campaign ended (that is ``ends_at``) and records the reason on its own
                # field, so it is re-triggerable and distinct from a failed run. Mirrors
                # the cluster auto-chain in controller._chain_postprocessing.
                #
                # A cancelled postprocess lands here too and keeps that shape: the runs and
                # their results are complete, so only the derived data is missing, which is
                # what the field says and a re-run supplies. Told apart by the flag, not by
                # the message.
                cancelled = state.postprocessing_stop_requested
                state.update(postprocessing_error=message, postprocessed=False)
                state.set_phase(ends_at, stage=(
                    message if cancelled else f"postprocessing failed: {message}"))
        except Exception as e:  # noqa: BLE001 - surfaced via status
            logger.exception("Postprocessing for %s failed", campaign_id)
            state.update(postprocessing_error=failure_detail(e), postprocessed=False)
            state.set_phase(ends_at, stage=f"postprocessing failed: {e}")
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

        Called at the top of the worker so it lands before anything that can fail. *images* are
        a replay's digests, stated from this first write; a fresh launch passes none and
        records each image as it fixes it (``update_launch_images``). Not fatal here, because
        what makes the record replayable comes next: the digests are written before any pod
        exists, and that write is fatal -- a campaign whose record cannot take them stops
        before it runs anything.
        """
        from robovast.common.campaign_data import write_launch_record
        campaign_root = Path(results_dir) / campaign_id
        try:
            write_launch_record(campaign_root, request, images=images)
        except OSError as e:
            logger.warning("Could not write launch.yaml for %s: %s", campaign_id, e)

    @abstractmethod
    def _scheduling_for(self, campaign_id: str, *, live: bool) -> dict:
        """``{"priority", "paused"}`` for a listing row, as this implementation answers it.

        Reported as a pair so a row that admits nothing says which of the two reasons
        it is. An implementation with no queue has one answer for every campaign.
        """

    def _record_campaign_stopped(self, campaign_id, results_dir, state, backend) -> None:
        """Persist a cooperatively-stopped campaign's terminal ``Status``.

        So the ``stopped`` phase survives a restart — otherwise a stopped campaign
        reconstructs from disk as an ambiguous ``finished``/``unknown``. The local home
        is the filesystem, so writing ``outcome.json`` is enough.
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

        One implementation serves every case: a campaign this process is driving answers
        from the store its controller is writing right now, and any other from its durable
        records. It reads the store directly: the per-batch objectives are the store's own
        rows, and a search running now is exactly the one this exists to show.
        """
        from robovast.common.store import read_batch_objectives
        history = read_batch_objectives(self.campaign_dir(campaign_id))
        if history is None:
            return SearchHistory(unavailable="no_store")
        return SearchHistory(**history)

    def _derive_postprocessed(self, campaign_id: str, snap: Status) -> Status:
        """Apply the recovery path's ``postprocessed`` rule to a **live** snapshot.

        ``reconstruct_status_from_disk`` states it: *postprocessed is a fact about the
        campaign, not about who last drove it*, and derives it from the provenance record
        the campaign-end pass writes last. The live ``ControllerState`` answers a narrower
        question — ``_postprocess`` records ``True`` only when the ``.vast`` declared
        postprocessing **entries**, which is what decides whether the stored archive is the
        postprocessed one. Both are wanted, but only the first is what a reader means by
        "is there data here", so the two have to agree on that: a campaign that declares no
        ``results_processing.postprocessing`` still has its tables built, and must not read
        as unpostprocessed for as long as this process tracks it and as postprocessed once
        a restart hands the question to the disk path.

        Only ever promotes ``False`` → ``True``, and only on the evidence the recovery path
        uses — :func:`~robovast.common.campaign_data.campaign_has_derived_data`, which both
        call so they cannot disagree; what ``_postprocess`` records is untouched, and so is
        the archive decision that reads it.

        Two states are deliberately *not* promoted, both of which a record left by an
        earlier pass would promote — this is the live path, so it sees them where the
        recovery path (which runs only once nothing is driving the campaign) mostly cannot:

        * a pass **in progress**, which the *phase* decides. A re-run leaves the previous
          record in place until it writes its own, so a campaign would otherwise report its
          results ready while its tables are being built again, and the web UI gates its
          Results views on exactly this flag. Read
          from the phase and not from "some earlier attempt left an error", which is a fact
          about the past that happens to correlate: a first postprocess, or a re-run of one
          that previously succeeded, has no such error and is no less in progress.
        * a build that **failed**. ``postprocessing_error`` sets the flag False on purpose;
          promoting it back would hide the error behind "results are ready".
        """
        if (snap.postprocessed or snap.postprocessing_error
                or snap.phase == Phase.POSTPROCESSING):
            return snap
        from robovast.common.campaign_data import campaign_has_derived_data
        try:
            if campaign_has_derived_data(self.campaign_dir(campaign_id)):
                snap.postprocessed = True
        except OSError:
            pass          # a status read must not fail over an unreachable record dir
        return snap

    def get_campaign_logs(self, campaign_id: str, cursor: str = "", *,
                          phase: Optional[str] = None, min_level: Optional[str] = None,
                          grep: Optional[str] = None) -> CampaignLogChunk:
        """The campaign's infrastructure log rows after *cursor*, from its phase files.

        The files under the campaign's ``_execution/`` grow in place while the campaign
        runs, so a running campaign and a finished one are read alike
        (:mod:`robovast.service.campaign_log`). ``eof`` once the campaign is over -- not
        driven here, or driven to its end -- and nothing was held back.
        """
        from robovast.service import campaign_log  # pylint: disable=import-outside-toplevel
        campaign_dir = self.campaign_dir(campaign_id)
        final = not self.campaign_is_live(campaign_id)
        read = campaign_log.read_rows(campaign_dir, cursor, final=final, phase=phase,
                                      min_level=min_level, grep=grep)
        return CampaignLogChunk(rows=read.rows, cursor=read.cursor,
                                eof=final and not read.pending, phases=read.phases)

    def campaign_log_watch(self, campaign_id: str):
        """A :class:`~robovast.service.campaign_log.CampaignLogWatch` over the campaign's
        phase files, for a stream that pushes rows as they are written. The caller closes
        it."""
        from robovast.service import campaign_log  # pylint: disable=import-outside-toplevel
        return campaign_log.CampaignLogWatch(self.campaign_dir(campaign_id))

    #: Directories under a campaign that are not per-configuration results.
    _RESERVED_DIRS = frozenset({"_config", "_execution", "_transient"})

    def _job_artifact_hint(self, campaign_id: str, job_name: str) -> str:
        """The campaign-relative directory of a job the job-link manifest does not name.

        The manifest names each run's job, keyed by ``<config>/<run>``. A service whose job
        names are not run keys -- a Kubernetes Job's is not -- answers here from what it
        knows of the job itself; ``""`` when it knows nothing.
        """
        del campaign_id, job_name
        return ""

    def _job_log_dir(self, campaign_id: str, job_name: str) -> Tuple[Optional[Path], List[str]]:
        """``(job directory, runs placed in it)``, or ``(None, [])`` before it is known.

        Raises ``KeyError`` for a job the campaign does not have.
        """
        from robovast.client.safe_path import             UnsafePathError  # pylint: disable=import-outside-toplevel
        from robovast.common.execution import (  # pylint: disable=import-outside-toplevel
            read_job_links, resolve_job_artifact_rel)
        from robovast.service import job_log  # pylint: disable=import-outside-toplevel

        campaign_dir = self.campaign_dir(campaign_id)
        if not campaign_dir.is_dir():
            raise KeyError(f"no campaign {campaign_id!r}")
        links = read_job_links(campaign_dir)
        try:
            rel = resolve_job_artifact_rel(links, job_name)
        except FileNotFoundError:
            rel = self._job_artifact_hint(campaign_id, job_name)
        if not rel:
            # Before the first job starts there is no manifest yet: a run the campaign has
            # is a job whose log does not exist yet, anything else is not a job of it.
            try:
                run_dir = safe_join(campaign_dir, job_name)
            except UnsafePathError as exc:
                raise KeyError(str(exc)) from exc
            if not links and run_dir.is_dir():
                return None, []
            raise KeyError(f"job {job_name!r} not found in campaign {campaign_id!r}")
        try:
            job_dir = safe_join(campaign_dir, rel)
        except UnsafePathError as exc:
            raise KeyError(str(exc)) from exc
        return job_dir, job_log.runs_of_job(links, rel)

    def job_log_watch(self, campaign_id: str, job_name: str):
        """A :class:`~robovast.service.job_log.LogWatch` over the job's log files, for a stream
        that pushes rows as they are written. The caller closes it."""
        from robovast.service import job_log  # pylint: disable=import-outside-toplevel
        job_dir, _runs = self._job_log_dir(campaign_id, job_name)
        return job_log.LogWatch(job_dir)

    def get_job_log(self, campaign_id: str, job_name: str, cursor: str = "") -> JobLogChunk:
        """A job's log rows after *cursor*, read from its ``logs/system*.log`` files.

        The files grow in the campaign directory while the job runs, so a
        running job and a finished one are read alike (:mod:`robovast.service.job_log`).
        ``eof`` once the job is over -- the campaign no longer live, or every run placed in
        the job has its settled verdict -- and a read found nothing more.
        """
        from robovast.service import job_log  # pylint: disable=import-outside-toplevel

        job_dir, runs = self._job_log_dir(campaign_id, job_name)
        live = self.campaign_is_live(campaign_id)
        if job_dir is None:
            job_log.decode_cursor(cursor)
            return JobLogChunk(cursor=cursor, eof=not live)
        finished = not live or job_log.runs_finished(self.campaign_dir(campaign_id), runs)
        rows, next_cursor, pending = job_log.read_rows(job_dir, cursor, final=finished)
        return JobLogChunk(rows=rows, cursor=next_cursor,
                           eof=finished and not rows and not pending)

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
        return campaign_execution(self.campaign_dir(campaign_id))

    def get_job_state(self, campaign_id: str, job_name: str) -> "JobState":
        """What a running job is doing: its scenario's tree from the run's own tables, its
        simulator's and resource monitor's word from the run's containers by fixed commands.

        Three readers, asked independently on purpose: the scenario's tree is there whatever the
        simulator is, so a campaign whose simulator cannot report on itself still gets the more
        useful half. Coupling them would have made the absence of one hide the other.

        The scenario half is folded from the run's ``behaviors`` and ``behaviors_meta`` tables
        (:mod:`robovast.service.scenario_state`) -- the rows the campaign's data engine reads out
        of the ``behaviors.jsonl`` the run writes, which grows in the campaign directory -- so
        nothing runs in the run to answer it. It is read fresh every time: a fold over
        every recorded transition rather than a tail, which is why it is asked for here and never
        polled. A log without its metadata record raises, naming the file.

        The health half is served from whatever the status path last pulled (see
        :meth:`_read_health`), so an agent asking is never charged for a check a poll has already
        paid for. It parses no simulator's file format: the tool that owns the record reads it and
        prints JSON, so the record can be reshaped by its owner without breaking this.

        **Each exec is asked of the container that owns it**, which is why the target is resolved
        per role and not per job: the resource monitor writes under the scenario container's
        ``/out``, while a simulator with a container of its own answers only there. Sending both
        to one target sent ``roqsim health`` into a container with no roqsim in it for every
        ROS-shape campaign.

        Everything that decides *what* is asked is here, and only :meth:`_job_state_target`
        knows how to address the job's pod.
        """
        from robovast.service.interface import JobState

        job = self._require_running_job(campaign_id, job_name)
        # `unavailable` is a pydantic list field; the linter reads the class attribute as
        # the FieldInfo descriptor rather than the instance's list and calls every append
        # below an error. Scoped to this function, which is the only place it appears.
        # pylint: disable=no-member
        state = JobState(job_name=job_name, status=job.status)
        try:
            target, run_dir = self._job_state_target(campaign_id, job_name, SCENARIO_CONTAINER)
            run_dir, state.run = self._job_live_run(campaign_id, job_name, target, run_dir)
            job_dir = self._job_output_dir(campaign_id, job_name, run_dir)
        except Exception as err:  # noqa: BLE001 - a job between scheduling and running, or gone
            state.unavailable.append(str(err))
            return state
        self._fold_scenario_state(state, campaign_id)
        self._read_resources(state, target, job_dir)
        document, reason = self._read_health(campaign_id, job_name, job_dir, run_dir)
        if reason:
            state.unavailable.append(reason)
        state.simulator = document
        return state

    def tap_job(self, campaign_id: str, job_name: str, selection: Optional[list] = None, *,
                max_seconds: int = TAP_MAX_S, source: str = "api"):
        """Start the backend's following command in the job's simulation container and
        relay its lines (:class:`~robovast.service.tap.TapStream`).

        Every decision is made **before** anything runs, in this order: the job is running
        (:meth:`_require_running_job`), the campaign's simulator has a tap for this
        selection (:func:`~robovast.common.simulators.tap_command`; ``None`` is refused naming
        the backend, since a tap that printed nothing would otherwise be indistinguishable
        from a simulator that cannot be tapped), no tap is open on this job, and only then
        the probe is recorded and the exec started. A refusal therefore records nothing.

        The command is bounded twice. The runner's ``stream_in`` ends the *relay* at the bound
        or when the reader closes; the process in the container outlives both
        (see :meth:`~robovast.service.container_exec.ExecRunner.stream_in`), so the command
        itself runs under ``timeout``, which ends it in the container at the same bound
        whatever became of the reader. Run through :func:`~robovast.common.execution.in_run_env`
        as every live-job exec is, so ``ros2`` resolves.

        Like :meth:`get_job_state`, what is asked is decided here; the implementation says the
        target (:meth:`_job_state_target`) and where the probe is recorded (:meth:`_job_probe_dir`).
        """
        from robovast.common.campaign_data import KIND_PROBED, record_intervention
        from robovast.common.execution import in_run_env
        from robovast.common.simulators import backend_name, tap_command
        from robovast.service.tap import TapStream

        selection = [str(name) for name in (selection or [])]
        limit_s = max(1, min(int(max_seconds), TAP_MAX_S))
        self._require_running_job(campaign_id, job_name)
        try:
            execution = self._campaign_execution(campaign_id)
        except Exception as err:  # noqa: BLE001 - the reason a tap cannot be chosen
            raise ValueError(f"could not read this campaign's configuration: {err}") from err
        target, run_dir = self._job_state_target(campaign_id, job_name, SIMULATION_CONTAINER)
        argv = tap_command(execution, run_dir=run_dir, selection=selection)
        if not argv:
            backend = backend_name(execution) or f"a '{execution.get('mode')}' campaign " \
                                                 f"without a simulator backend"
            raise ValueError(
                f"no tap for {backend}: this campaign's simulator names no following command, "
                f"so there is nothing to relay from a live run")
        with self._taps_guard:
            if (campaign_id, job_name) in self._taps:
                raise RuntimeError(
                    f"a tap is already open on job {job_name!r} of {campaign_id!r}; one relay "
                    f"per job at a time -- read that one, or wait for it to end")
            self._taps.add((campaign_id, job_name))

        def release():
            with self._taps_guard:
                self._taps.discard((campaign_id, job_name))

        try:
            job_dir, runs = self._job_probe_dir(campaign_id, job_name)
            # Before the command, as exec_in_job records: a process the service started is
            # about to run in the simulator's container, and a crash in between must not
            # leave a perturbed run with nothing saying why.
            record_intervention(self.campaign_dir(campaign_id), kind=KIND_PROBED,
                                job_dir=job_dir, job_name=job_name, source=source,
                                detail=f"tap {shlex.join(selection) or '(topic list)'} "
                                       f"for {limit_s}s", runs=runs)
            bounded = (f"exec timeout --signal=INT --kill-after={_TAP_KILL_GRACE_S} "
                       f"{limit_s} {shlex.join(argv)}")
            runner = self._exec_runner()
        except BaseException:
            release()
            raise
        return TapStream(
            lambda on_line, should_stop: runner.stream_in(
                target, in_run_env(bounded), limit_s=limit_s, on_line=on_line,
                should_stop=should_stop),
            on_close=release)

    @abstractmethod
    def _job_probe_dir(self, campaign_id: str, job_name: str) -> tuple:
        """``(job_dir, runs)`` a probe of *job_name* is recorded against: the job's
        campaign-relative artifact dir, and the run keys the implementation already knows it carries
        (see :func:`~robovast.common.campaign_data.record_intervention`).
        """

    @abstractmethod
    def _job_output_dir(self, campaign_id: str, job_name: str, run_dir: str) -> str:
        """Where a running job's own output is read from.
        """

    @abstractmethod
    def _job_live_run(self, campaign_id: str, job_name: str, target, run_dir: str) -> tuple:
        """The run directory a live *job_name* of *campaign_id* is writing, or ``None``.
        """

    # -- what the running jobs' simulators say about themselves ---------------------------------
    #
    # The service *pulls*, with a command it chose itself, so nothing has to run in a container
    # and nothing is emitted anywhere. That is what makes this free for a campaign nobody is
    # debugging: an unwatched campaign is never asked, because only a status read asks.

    @abstractmethod
    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        """``(target, run_dir)`` for a fixed read inside a running job: what the exec runner
        addresses, and the run directory inside it.
        """

    def _health_targets(self, campaign_id: str) -> list:
        """``(job_name, job_dir, run_dir)`` for every job of this campaign running right now.

        A different question from :meth:`_require_running_job`'s, which is why it is a different
        method: that one enforces "this named job is running" for a caller who named one, this one
        asks "which jobs are there to ask".

        No target: the health read resolves its own, because the container it belongs in is the
        simulator's and not the job's.

        A node-calibration probe **is** asked, exactly as a run job is. The probe runs one real
        configuration in the job shape so that what it measures stands for what the jobs will
        use -- and this read is part of that shape: it is a process the service starts *inside the
        simulator's container*, charged to the simulator's memory, on every interval somebody is
        watching. A probe spared it would be sized without it, and the jobs would then meet, on top
        of a limit that has no room for it, the one cost the probe never saw.
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
        backend writes is the backend's business. Only a
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
            # Through the hook, not :meth:`_job_container`: on the cluster a target is a
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
            exit_code, stdout, stderr, timed_out = self._exec_runner().exec_in(
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

        On the base deliberately: what the reply *means* does not depend on where the job
        runs, so it has one interpretation.
        """
        if timed_out:
            return None, (
                f"{command!r} did not answer within {_JOB_STATE_LIMIT_S}s. The container may be "
                "wedged, which is itself a finding -- but this call cannot confirm it.")
        text = (stdout or "").strip()
        if not text:
            return None, (f"{command!r} exited {exit_code}"
                          + (ServiceBase._said(stderr) or " and printed nothing at all"))
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
        that has stopped being true must stop being reported. Two sources: what each running
        job's simulator says about itself, and what RoboVAST recorded in the campaign's ledger
        about faults a job cannot report -- a container killed at a figure the campaign
        measured (:meth:`_findings_from_record`). Nothing else is inferred here; other failures
        are left to :meth:`get_job_state` to explain.
        """
        findings: list = []
        skipped: list = []
        try:
            findings.extend(self._findings_from_record(campaign_id))
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

    def _findings_from_record(self, campaign_id: str) -> list:
        """Findings RoboVAST itself recorded about this campaign's runs.

        The other source of findings, beside the document a running job writes about itself.
        Some faults cannot be self-reported: a container that was OOM-killed is not there to
        say so, and the Job carrying the evidence is deleted moments later -- so the runner
        writes what it saw into the campaign's ledger and this reads it back.

        **One finding per fault, not per run.** The ledger has an entry per lost run; a reader
        acts on the fault, and forty findings saying the same thing would hide whatever else
        the campaign is reporting. The count is what makes it a fault rather than a flake, so
        it is in the sentence.
        """
        from robovast.client.status import HealthFinding
        from robovast.common.campaign_data import KIND_SIZING, read_interventions

        entries = read_interventions(self.campaign_dir(campaign_id), KIND_SIZING)
        if not entries:
            return []
        newest = entries[-1]
        return [HealthFinding(
            job_name=str(newest.get("job_name") or ""),
            level="error",
            # The slug a reader keys on, and what `vast campaign wait` takes its edge from: one
            # check firing for every lost run is one exit, not a stream.
            check="calibrated-memory-oom",
            detail=(f"{len(entries)} run(s) lost: {newest.get('detail') or 'OOM-killed'}. "
                    "Every run is sized from one probe's peak, so this meets the runs still to "
                    "come on that node. To bound it, state `calibration.min.memory` or raise "
                    "`resources.memory`, and run again."))]

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

    def _fold_scenario_state(self, state, campaign_id: str) -> None:
        """Fold the run's behaviour-tree tables into ``state.scenario``, or say why not.

        From the campaign directory, which holds the run's ``behaviors.jsonl`` as it grows,
        and through the campaign's data engine, so the tree an agent is shown here
        is the one a query of ``behaviors`` sees. The run is the one ``state.run`` names; a job
        whose run could not be resolved has no log to fold, and says so.

        A log that has not been written or not ticked is the reader's own stated reason, already
        phrased for a reader. A log without its metadata record, or one the engine could not
        turn into rows, raises: an unreadable tree reported as "unavailable" beside a healthy
        simulator reads as a run with nothing to show, which it is not.
        """
        from robovast.service.scenario_state import scenario_state
        if state.run is None:
            state.unavailable.append(
                "which run this job is on could not be resolved, so its scenario tree was not "
                "read")
            return
        reply = scenario_state(str(self.campaign_dir(campaign_id)), campaign_id, state.run)
        if not reply.get("found"):
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
        from robovast_decode.resource_usage import ScanStats, parse_container_rows

        script = (f'find {shlex.quote(run_dir)} -maxdepth {self._RESOURCE_FIND_DEPTH} '
                  f'-name "resource_usage_*.csv" -type f | while read -r f; do '
                  f'echo "@@ $(basename "$f")"; head -1 "$f"; '
                  f'tail -n {self._RESOURCE_TAIL_LINES} "$f"; done')
        _exit_code, stdout, stderr, timed_out = self._exec_runner().exec_in(
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
        :func:`~robovast_decode.resource_usage.expected_container_files` already
        encodes: ``resource_usage_<container>.csv``, with ``main`` for the scenario container.
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
        """The named job, or raise — the precondition of :meth:`stop_job`.

        Resolved through :meth:`list_jobs` rather than a backend-specific probe so the
        precondition is checked against the very status the caller was shown. ``KeyError``
        for a job that does not exist, ``RuntimeError`` naming the phase for one that
        exists but is not running: only a job that is *underway* has something to kill.

        A job that is not one of the campaign's runs is refused outright, whatever its
        status: a node-calibration probe, and the postprocessing conversion. Stopping a job
        records a ``killed`` intervention against the runs it was carrying, and neither
        carries any -- they are not the campaign's runs, and are deliberately absent from the
        job-links manifest that resolves them -- so the record would name runs that do not
        exist. Refused here rather than in the cluster service's ``stop_job`` because this is
        the shared precondition and the one the web UI mirrors when it decides
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

    @abstractmethod
    def _shutdown_running_campaigns(self, running) -> None:
        """What exiting this process does to *running* campaigns: end them, because nothing
        comes back for their compute, or leave them for a successor to adopt. A
        property of the implementation, and never inherited.
        """

    def shutdown(self) -> None:
        """Stop any in-flight campaign so Ctrl+C on ``vast serve`` tears it down.

        Campaigns run on daemon worker threads, so a bare process exit would kill
        the worker mid-run and orphan its workloads. What is done about the campaigns
        still running is the implementation's decision
        (:meth:`_shutdown_running_campaigns`); what happens here is the shared part: the
        flag, the held exec containers, and finding out what is running.
        """
        # Set before anything is torn down, so a worker reaching its own tail during the
        # teardown sees it and does not start work this process cannot finish.
        self._shutting_down = True
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
        self._shutdown_running_campaigns(running)

    def list_campaigns(
        self, request: Optional[ListCampaignsRequest] = None
    ) -> ListCampaignsResponse:
        request = request or ListCampaignsRequest()
        results_dir = self._campaigns_root()
        from robovast.common.execution import is_campaign_dir

        # Which campaigns exist = those persisted on disk ∪ those being driven now
        # (registered in-memory, perhaps without a directory yet — a just-launched one is
        # still building/starting). Not two sources of truth: each id is resolved to a
        # summary by the same precedence get_status uses (live snapshot if tracked, else
        # reconstruct from its records).
        disk = {d.name for d in results_dir.iterdir()
                if d.is_dir() and is_campaign_dir(d.name)} if results_dir.is_dir() else set()
        with self._lock:
            entries = dict(self._campaigns)
        mem = set(entries)

        def is_live(cid: str) -> bool:
            """Whether *cid* is being worked on right now.

            ``_is_done`` rather than a phase test of our own, because it is already the
            class's at-rest predicate (`_rest_key`, `_ensure_deletable`) — one notion of
            "running" here, not two that can drift. Registration is not it: an entry is
            removed only on delete, so it outlives the campaign and would pin every
            campaign of this service's life to the top.
            """
            entry = entries.get(cid)
            return entry is not None and not self._is_done(entry)

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
        live = {cid: is_live(cid) for cid in started}
        recent = {cid: started[cid] if live[cid] else (finished[cid] or started[cid])
                  for cid in started}

        def _key(c: str):
            return (live[c], recent[c] is not None, recent[c] or "", c)

        # The default order, and the tie-break under every other one: sorting stably on the
        # requested key alone keeps campaigns that tie on it -- and the ones that have no
        # value for it -- in this order among themselves.
        all_ids = sorted(started, key=_key, reverse=True)
        if request.sort == "size":
            # From the same record the row's figure comes from (see _results_bytes_for), and
            # asked only under this sort, so the default listing reads nothing extra.
            value = {cid: self._results_bytes_for(cid) for cid in all_ids}
        else:
            value = recent
        if request.sort != "recent" or request.order != "desc":
            # Two stable passes: the value inside each group, then the group. A campaign with
            # no value goes last in its group whichever way the order runs -- an unknown size
            # is not the smallest one, and an unknown start is not the oldest.
            all_ids.sort(key=lambda c: (value[c] is not None, value[c]),
                         reverse=request.order == "desc")
            all_ids.sort(key=lambda c: (live[c], value[c] is not None), reverse=True)
        total = len(all_ids)
        window = all_ids[request.offset:request.offset + request.limit]
        summaries = [self._summary_for(cid) for cid in window]
        return ListCampaignsResponse(campaigns=summaries, total=total)

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

    def _campaign_siblings(self, campaign_id: str) -> list[Path]:
        """Files that belong to *campaign_id* but live outside its directory.

        The copy an import fetched from the share and kept because the import failed. An
        *uploaded* archive is staged under its grant token, not the campaign id, so it is
        not found here; ``_sweep_staged_archives`` removes it.
        """
        staged = self._staging_dir() / f"{campaign_id}.tar.gz"
        return [staged] if staged.is_file() else []

    def delete_campaign(self, campaign_id: str) -> ActionResult:
        """Delete one campaign (see interface): the guard, then :meth:`_delete_deletable`."""
        self._ensure_deletable(campaign_id)
        done = self._delete_deletable(campaign_id)
        return ActionResult(ok=done.ok, message=done.message)

    def delete_campaigns(self, request: DeleteCampaignsRequest) -> DeleteCampaignsResponse:
        """Delete each campaign through the same guard and removal as :meth:`delete_campaign`.

        The guard's refusals become that id's outcome rather than the call's: a
        ``ValueError`` is an id that is not a campaign id, a ``RuntimeError`` a campaign
        still running. A removal that fails outright is that id's outcome too -- a batch
        that raised would discard the outcomes of the ids already deleted, and the caller
        would have no record of what is gone. Sequential, so two ids never race for the
        same index connection or the transport lock, and the order of the results is the
        order asked for.
        """
        results: list[CampaignDeletion] = []
        for campaign_id in dict.fromkeys(request.campaign_ids):
            try:
                self._ensure_deletable(campaign_id)
            except ValueError as e:
                results.append(CampaignDeletion(campaign_id=campaign_id, outcome="invalid",
                                                ok=False, message=str(e)))
                continue
            except RuntimeError as e:
                results.append(CampaignDeletion(campaign_id=campaign_id, outcome="running",
                                                ok=False, message=str(e)))
                continue
            try:
                results.append(self._delete_deletable(campaign_id))
            except Exception as e:  # pylint: disable=broad-except
                logger.exception("could not delete %s", campaign_id)
                results.append(CampaignDeletion(
                    campaign_id=campaign_id, outcome="partial", ok=False,
                    message=(f"Campaign {campaign_id!r} was not fully deleted: {e}. "
                             f"Deleting it again removes whatever is left.")))
        return DeleteCampaignsResponse(results=results)

    def _delete_deletable(self, campaign_id: str) -> CampaignDeletion:
        """Remove a campaign :meth:`_ensure_deletable` has passed: its directory and its
        sibling files. An implementation that owns more than files extends this, so the
        single and the multi-campaign delete both reach it.

        Everything that can go is removed before anything is reported, so a second delete
        retries only what is left. Anything left behind makes the outcome ``partial``, whose
        message says what stopped it and names the path where there is one: the commonest
        cause is run output written by a container user other than the service's
        (``execution.run_as_user``), which the service cannot unlink, and a delete that
        answered "deleted" over it would leave the space taken with nothing saying so.
        """
        campaign_dir = self.campaign_dir(campaign_id)
        existed = campaign_dir.is_dir()
        failed: list[tuple[str, OSError]] = []

        def _record(_func, path, exc):
            if not isinstance(exc, FileNotFoundError):
                failed.append((path, exc))

        if existed:
            shutil.rmtree(campaign_dir, onexc=_record)

        removed_archives, archive_bytes = 0, 0
        for path in self._campaign_siblings(campaign_id):
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as e:
                failed.append((str(path), e))
            else:
                removed_archives += 1
                archive_bytes += size

        with self._lock:
            self._campaigns.pop(campaign_id, None)
            for cache in (self._started_at_cache, self._finished_at_cache,
                          self._results_bytes_cache, self._description_cache,
                          self._created_by_cache, self._origin_cache, self._summary_cache,
                          self._disk_status_cache, self._scene_identity_cache):
                cache.pop(campaign_id, None)

        if failed:
            path, exc = failed[0]
            return CampaignDeletion(
                campaign_id=campaign_id, outcome="partial", ok=False,
                message=(f"Campaign {campaign_id!r} was not fully deleted: {len(failed)} "
                         f"path(s) could not be removed, the first being {path} "
                         f"({exc.strerror or exc}). Files written by a container user other "
                         f"than the service's (execution.run_as_user) cannot be removed by "
                         f"the service; remove them as that user, then delete again."))
        if not existed and not removed_archives:
            return CampaignDeletion(
                campaign_id=campaign_id, outcome="not_found", ok=True,
                message=f"Campaign {campaign_id!r} had no local data; nothing to delete.")
        archives = (f", and {removed_archives} archive(s) of "
                    f"{archive_bytes / 1024 ** 3:.2f} GiB beside it" if removed_archives else "")
        return CampaignDeletion(campaign_id=campaign_id, outcome="deleted", ok=True,
                                message=f"Deleted campaign {campaign_id!r}{archives}.")

    # -- postprocessing -----------------------------------------------------

    def get_postprocessing(self, campaign_id: str):
        from robovast.service.interface import PostprocessingInfo
        from robovast.service.postprocessing_edit import get_postprocessing
        info = get_postprocessing(self.campaign_dir(campaign_id))
        return PostprocessingInfo(campaign_id=campaign_id, entries=info["entries"])

    def update_postprocessing(self, request):
        from robovast.service.interface import PostprocessingRevision
        from robovast.service.postprocessing_edit import update_postprocessing
        res = update_postprocessing(self.campaign_dir(request.campaign_id),
                                    request.entries)
        return PostprocessingRevision(campaign_id=request.campaign_id,
                                      entries=res["entries"])

    def get_postprocessing_source(self, campaign_id: str):
        from robovast.service.interface import PostprocessingSource
        from robovast.service.postprocessing_edit import get_postprocessing_source
        info = get_postprocessing_source(self.campaign_dir(campaign_id))
        return PostprocessingSource(campaign_id=campaign_id, content=info["content"])

    def update_postprocessing_source(self, request):
        from robovast.service.interface import PostprocessingSource
        from robovast.service.postprocessing_edit import update_postprocessing_source
        update_postprocessing_source(self.campaign_dir(request.campaign_id),
                                     request.content)
        return PostprocessingSource(campaign_id=request.campaign_id,
                                    content=request.content)

    def _archive_repeatable_sections(self, campaign_id: str) -> None:
        """Move every finished repeatable-phase log aside, before a new run writes one.

        A repeatable phase (postprocess, share) writes the same filename every time it
        runs. Left in place, the next run either replaces those bytes or appends to them,
        and either way the campaign log stops being append-only: a reader holds a cursor
        into each file, so a file that changes behind the position already read is rows
        nobody is ever shown -- and a shorter one makes the reader start it over. Archived
        under ``_execution/sections/<seq>-<phase>.log`` it is finished and immutable, the
        new run's file is the only one still growing,
        :func:`~robovast.common.campaign_logs.ordered_sections` puts it last, and the
        reader carries its cursor entry over to the archived name.

        **All** of them, not only the phase about to run, so at most one live base file
        exists and "the live one is last" has exactly one answer.

        Best-effort throughout: an operation must run even when the account of the
        previous one could not be moved. What a failure costs is a duplicated section
        until the new run writes over the base file, which is worth strictly less than
        the postprocess it would otherwise block.
        """
        from robovast.common.campaign_logs import (EXECUTION_DIR, REPEATABLE_PHASES,
                                                   disk_section_names, next_section_seq,
                                                   section_name)
        root = self.campaign_dir(campaign_id)
        seq = next_section_seq(disk_section_names(root))
        for base in REPEATABLE_PHASES:
            live = root / EXECUTION_DIR / base
            if not live.exists():
                continue
            target = root / EXECUTION_DIR / section_name(seq, base)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                live.replace(target)
            except OSError as e:
                logger.warning("Could not archive %s of %s: %s", base, campaign_id, e)
                continue
            # Consumed only when something actually moved: a phase that never ran would
            # otherwise burn a number and leave a gap in the campaign's order.
            seq += 1

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
        the campaign visible while its bytes are still arriving. So the reads below are
        allowed to find nothing and fall back — which is also the honest answer, since a
        campaign being imported has no earlier start time than now.
        """
        # The recorded outcome the installed entry must not blank. That entry answers for
        # the campaign in every listing for as long as the operation runs, and an empty
        # ControllerState answers with *no* errors: a campaign whose postprocessing failed
        # reads as error-free the moment somebody re-triggers its upload, and
        # `_derive_postprocessed` -- whose only guard against promoting `postprocessed`
        # over a failure is that error field -- then promotes it, so the web UI offers
        # Results views over a build that did not finish. Read before the lock: it is disk
        # (and on the cluster, store) I/O, and nothing about it needs the map held.
        prior = self._prior_outcome(campaign_id)
        # ...except the verdict this operation is here to REPLACE, which is both fields and
        # only for a postprocess. That message describes an attempt that has ended, and
        # carrying it makes the campaign report "postprocessing failed" for as long as the
        # run meant to fix it lasts -- naming a cause the attempt in flight has already
        # disproved. The flag goes with it: the previous run's provenance record is still on
        # disk, so a rebuild would otherwise report "results are ready" over the data it is
        # replacing, which is the state ``_derive_postprocessed`` refuses to promote *to* and
        # so must not be handed either. ``work`` writes both when it ends -- cleared on
        # success, replaced on failure -- and erring towards False meanwhile is the direction
        # ``campaign_has_derived_data`` already calls the recoverable one.
        #
        # A share carries both unchanged: it is not redoing the postprocess, so the verdict
        # it holds is still the current one.
        rerunning = phase == Phase.POSTPROCESSING
        carried_error = None if rerunning else prior.postprocessing_error
        carried_flag = False if rerunning else prior.postprocessed
        # Before the entry is installed, so the campaign is never visible as "running the
        # new phase" while the previous run's log is still the live file. An import has no
        # campaign directory yet and nothing to archive, which this reads as no files.
        self._archive_repeatable_sections(campaign_id)
        with self._lock:
            existing = self._campaigns.get(campaign_id)
            if existing is not None and not self._is_done(existing):
                # Named, not just "an operation": what the caller does next differs
                # entirely between waiting out a postprocessing run and waiting out the
                # sweep itself.
                busy_phase = existing.state.snapshot().phase
                return ActionResult(
                    ok=False,
                    message=f"campaign {campaign_id!r} is busy: it is {busy_phase!r} "
                            "— wait for that to finish")
            state = ControllerState()
            state.update(campaign_id=campaign_id,
                         postprocessed=carried_flag,
                         postprocessing_error=carried_error,
                         share_error=prior.share_error,
                         error=prior.error,
                         mode=prior.mode,
                         # Measured once when the campaign ended, and not by this
                         # operation: without it the entry lists the campaign as unmeasured
                         # for as long as it answers for it.
                         results_bytes=prior.results_bytes)
            state.set_phase(phase)
            entry = _TrackedCampaign(campaign_id, str(self._campaigns_root()), state)
            # The store's recorded start time, not now: re-running postprocessing or
            # sharing must not restamp (and so re-order) a finished campaign. Read
            # directly rather than via _started_at_for — we already hold self._lock,
            # which that helper takes.
            entry.created_at = (read_campaign_created_at(self.campaign_dir(campaign_id))
                                or entry.created_at)
            # ...but the FINISH time is restamped, and must be: this operation ends the
            # campaign again, later than last time. Dropping the cached value is what makes
            # the next listing re-read it; `_started_at_cache` needs no such thing because
            # a start time is written once and never edited.
            self._finished_at_cache.pop(campaign_id, None)
            self._results_bytes_cache.pop(campaign_id, None)
            # Likewise the description: a tracked entry answers for the campaign while
            # it is live, so leaving this empty would blank the description out of every
            # listing for the duration of a re-triggered postprocess/share.
            entry.description = (read_campaign_description(self.campaign_dir(campaign_id))
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

    def run_share(self, request) -> ActionResult:
        """(Re)trigger upload-to-share for one finished campaign, from disk.

        Dispatched as a tracked background op (works after a `vast serve` restart, no
        live entry needed). Local ``share_campaign`` writes the tar.gz to the archive
        dir; the durable ``share_error`` is cleared on success / set on failure.
        """
        campaign_dir = self.campaign_dir(request.campaign_id)

        def work(state):
            from robovast.client.logging_config import (add_campaign_log_handler,
                                                        remove_campaign_log_handler)
            from robovast.execution.backends import RunOptions, ShareStopped
            from robovast.execution.controller import (make_upload_progress_cb,
                                                       share_cancelled_detail)
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
            options = RunOptions(upload_to_share=True)
            try:
                logger.info("upload-to-share: %s", campaign_dir.name)
                backend.preflight_upload_to_share()
                backend.share_campaign(str(campaign_dir), options,
                                       progress_callback=make_upload_progress_cb(state))
                ok, message = True, "upload-to-share complete"
                logger.info("✓ %s", message)
            except ShareStopped as e:
                # An upload is a tracked campaign while it lasts, so ``stop_campaign``
                # reaches it. What comes back then is the operator's own doing, so it is
                # logged as a cancellation rather than an error -- and the partial is
                # discarded (or named) before anything is recorded. It still lands on
                # ``share_error``, because what a reader does next is the same as after a
                # failure: re-trigger the share.
                ok, message = False, share_cancelled_detail(backend, e)
                logger.info("⏹  upload-to-share cancelled: %s", message)
            except Exception as e:  # noqa: BLE001 - surfaced via status + share_error
                ok, message = False, failure_detail(e)
                logger.error("✗ upload-to-share failed: %s", message)
            finally:
                remove_campaign_log_handler(handler)
            status = record_step_outcome(campaign_dir, share=(ok, message))
            state.update(share_error=status.share_error)
            # The recorded phase, not `finished`: `record_step_outcome` preserves how the
            # campaign ended, and a live entry that disagreed with it would answer
            # differently until the next restart.
            state.set_phase(status.phase)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.SHARING, work=work)

    # -- validation / preview / authoring help (config editor) --------------

    def validate_project(self, workspace_id: str, path: str = "",
                         check_world: bool = True,
                         check_scenario: bool = True) -> ValidationReport:
        """See the interface.

        Composed inside the service's aux-runner context, and *held*, exactly as
        :meth:`preview_configurations` is: validation composes the file to count its cells, so
        it reaches whatever that composition asks for a container -- a variation's helper
        image, a generator's, the simulator's query for what a world is made of. Composing
        without one would refuse the campaign for a property of where it ran.
        Held rather than per-call because validating is an authoring loop, and it shares the
        tag with preview so the two reuse one warm container.
        """
        from robovast.common.config_validation import validate_project_file
        try:
            project = self._resolve_project(workspace_id, path)
            with self._aux_runner_context(_preview_tag(workspace_id, path), project,
                                          hold=True):
                result = validate_project_file(project.config_path)
        except Exception as e:  # noqa: BLE001 - editor sends in-progress YAML; never 500
            return ValidationReport(
                valid=False, world_checked=False if check_world else None,
                scenario_checked=False if check_scenario else None,
                problems=[ValidationProblem(stage="error", message=str(e))])
        # Only once the cheap checks pass. Compiling a world for a file with a schema
        # error spends a container to report something already in the reply, and the world
        # a broken file names is not necessarily the one it will name when it is fixed.
        if check_world and result.get("valid"):
            result = self._with_world_check(workspace_id, path, project, result)
        elif check_world:
            # Asked for and not performed, so it is False rather than None: the caller's
            # question was "and does the world load?", and this reply does not answer it.
            result = {**result, "world_checked": False}
        # Gated on the CHEAP checks, not on the world's verdict: a campaign whose world
        # does not load still wants to hear that its scenario does not parse either, and
        # both are fixed in the same edit.
        if check_scenario:
            result = self._with_scenario_check(workspace_id, path, project, result)
        return ValidationReport.model_validate(result)

    def _with_scenario_check(self, workspace_id: str, path: str, project,
                             result: dict) -> dict:
        """*result* plus the verdict on parsing and resolving the scenario in the image that
        runs it.

        The failure this catches is invisible to every cheap check and fatal to every
        trial: an ``import osc.<library>`` resolves against what is installed in the
        scenario image, so a scenario that parses on the service's host can die at its
        first line in the container -- once per run, after the pull and the schedule, with
        the campaign reporting finished. The same is true of a call the action's own
        signature refuses, which is why the check goes as far as RESOLVING the model and not
        merely building it.

        What it still cannot see is whatever the scenario does after its first parameter
        with no value, because the values are the configuration's and this check has none.
        Arguments are bound before a parameter's value is needed, so the invocations are
        reached; a defect that is only expressible in terms of a parameter's value is not.

        Like the world check, it is held (a repeat validation costs an exec, not a
        container start) and its own failure is an ``unchecked`` problem rather than a
        pass: ``valid`` covers it, so a scenario nobody could parse must not read as one
        that parses.
        """
        from robovast.common.common import load_config
        from robovast.service.scenario_query import scenario_problems
        try:
            declared = ((load_config(project.config_path) or {})
                        .get("execution") or {}).get("scenario_file")
            # Relative to the workspace ROOT, which is what the exec container mounts; the
            # .vast declares it relative to itself.
            scenario_path = os.path.normpath(
                os.path.join(os.path.dirname(path), str(declared))) if declared else ""
        except Exception as e:  # noqa: BLE001 - an unreadable .vast is already a problem
            logger.warning("could not resolve the scenario file to check: %s", e)
            scenario_path = ""
        if not scenario_path or scenario_path.startswith(".."):
            # No scenario named, or one outside the workspace: the cheap checks already
            # report that, and there is nothing here to parse.
            return {**result, "scenario_checked": None}
        problems = scenario_problems(
            self.exec_in_container,
            workspace_id=self.store.registry.require(workspace_id)["workspace_id"],
            config_path=path, scenario_path=scenario_path)
        if not problems:
            return {**result, "scenario_checked": True}
        unchecked = [p for p in problems if p.get("severity") == "unchecked"]
        return {**result,
                "scenario_checked": not unchecked,
                "valid": bool(result.get("valid")) and not problems,
                "problems": list(result.get("problems") or []) + problems}

    def _with_world_check(self, workspace_id: str, path: str, project,
                          result: dict) -> dict:
        """*result* plus any problem with the world(s) this campaign would load.

        The one check here that runs a container. It is worth it because the failure it
        catches is otherwise per-trial: a world that does not compile fails every run of
        the sweep, after the image pull and the schedule, with nothing earlier to say so.
        The container is *held* (see ``ExecRequest.query``), so a second validation of the
        same project costs an exec rather than a container start.

        A failure of the check itself is never a *defect in the campaign*, but it is not a
        pass either: it comes back as an ``unchecked`` problem naming what would settle it,
        and ``world_checked`` says which of the three happened. ``valid`` covers this check,
        so an unchecked world makes it false -- a caller that reads only the boolean, which
        is what a boolean is for, must not be told the file is good to run when the most
        expensive thing about it was never looked at.
        """
        from robovast.common.common import load_config
        from robovast.service.world_query import world_problems
        try:
            parameters = load_config(project.config_path) or {}
            problems = world_problems(
                self.exec_in_container,
                resolve_call=self.resolve_image,
                workspace_id=self.store.registry.require(workspace_id)["workspace_id"],
                # The workspace-relative path as the caller gave it; empty is fine and
                # means the sole .vast, which is what exec_in_container resolves too.
                config_path=path,
                vast_dir=str(Path(project.config_path).parent),
                parameters=parameters)
        except Exception as e:  # noqa: BLE001 - the check crashing is not a bad campaign
            # Reported, not logged and dropped. A caller cannot see this service's log, so
            # swallowing it returned a reply that had checked nothing and said so nowhere.
            logger.warning("the world check did not run: %s", e)
            problems = [{
                "stage": "world", "config": None, "severity": "unchecked",
                "field": "execution.containers.simulation.config",
                "message": ("this campaign's world was NOT checked: the check itself "
                            f"failed here ({e}). Next: nothing about the .vast changes "
                            "this -- it is a defect in the service, whose log carries the "
                            "traceback (`vast service log`).")}]
        if not problems:
            return {**result, "world_checked": True}
        unchecked = [p for p in problems if p.get("severity") == "unchecked"]
        binding = [p for p in problems if p.get("severity", "error") != "advice"]
        return {**result,
                "world_checked": not unchecked,
                "valid": bool(result.get("valid")) and not binding,
                "problems": list(result.get("problems") or []) + problems}

    def preview_configurations(
        self, workspace_id: str, max_configs: int = 0, path: str = ""
    ) -> PreviewResponse:
        from robovast.common.common import load_config
        from robovast.common.config_generation import generate_scenario_variations
        project = self._resolve_project(workspace_id, path)
        aux_containers: list = []
        # Both branches compose, so both need whatever the service uses to reach a variation's
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

    def list_config_names(self, workspace_id: str, path: str = "") -> ConfigNames:
        from robovast.common.common import load_config
        project = self._resolve_project(workspace_id, path)
        vast = project.config_path
        if (load_config(vast) or {}).get("search"):
            raise ValueError(
                "a search .vast draws its configurations while it runs, so there are no names "
                "to list, and a config filter does not apply to it")
        mtime = os.stat(vast).st_mtime_ns
        key = (workspace_id, vast)
        with self._config_names_lock:
            held = self._config_names.get(key)
            # A composition in flight is answered as it stands even if the file has moved on:
            # it cannot be stopped, and the first call after it lands composes again.
            if held is not None and (held[1].state == "composing" or held[0] == mtime):
                return held[1]
            started = ConfigNames(state="composing")
            self._config_names[key] = (mtime, started)
        threading.Thread(
            target=self._compose_config_names, args=(key, mtime, workspace_id, path, project),
            name=f"robovast-{_preview_tag(workspace_id, path)}-names", daemon=True).start()
        return started

    def _compose_config_names(self, key, mtime, workspace_id, path, project) -> None:
        """Compose *project*'s ``.vast`` as a preview does, publishing its step counter and then
        its names into ``_config_names[key]``."""
        from robovast.client.status import StepProgress
        from robovast.common.config_generation import (generate_scenario_variations,
                                                       parse_composition_step)

        def publish(result: ConfigNames) -> None:
            with self._config_names_lock:
                self._config_names[key] = (mtime, result)

        def on_line(line):
            step = parse_composition_step(line)
            if step is not None:
                publish(ConfigNames(state="composing",
                                    progress=StepProgress(done=step[0], total=step[1])))

        try:
            with self._aux_runner_context(_preview_tag(workspace_id, path), project, hold=True):
                campaign_data = generate_scenario_variations(
                    variation_file=project.config_path, progress_update_callback=on_line,
                    output_dir=None)
        except Exception as e:  # noqa: BLE001 - the failure is the result the caller polls for
            logger.info("Listing config names of %s failed: %s", project.config_path, e)
            publish(ConfigNames(state="failed", error=str(e)))
            return
        publish(ConfigNames(state="ready", names=[c["name"] for c in campaign_data["configs"]]))

    def describe_world(self, workspace_id: str, path: str = "", targets: str = "",
                       entities: bool = False) -> WorldDescription:
        import yaml

        from robovast.common.config_generation import WorldQueryUnavailable, describe_world_payload
        from robovast.common.errors import ActionableError
        from robovast.common.simulators import backend_name, campaign_sim_block
        project = self._resolve_project(workspace_id, path)
        with open(project.config_path, encoding="utf-8") as handle:
            parameters = yaml.safe_load(handle) or {}
        execution = parameters.get("execution", {}) or {}
        # The campaign DEFAULT block. A campaign that varies its world per configuration has
        # several; the answer names the world it described, so a caller can see which.
        block = campaign_sim_block(execution)
        started = time.monotonic()
        # Through the exec runner's held query container, never a `docker run` on the
        # service host: in a controller pod that would run on whatever host the service
        # happens to sit on -- a different image cache, or no docker at all -- with nothing
        # in the reply to say the answer did not come from the cluster. Outside a campaign's
        # composition the cluster has no other runner.
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
        except ActionableError as exc:
            # Same 400 as the refusal below, and for the same reason -- the caller asked for
            # a description that cannot be given. Its own arm because ActionableError is not
            # a RuntimeError: left to escape it is the one refusal here that reaches a client
            # as a bare 500, and the next step it carries has to travel in the detail to
            # survive the HTTP boundary at all.
            raise ValueError(
                f"{exc} Next: {exc.next_step}" if exc.next_step else str(exc)) from None
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

    def campaign_dir(self, campaign_id: str) -> Path:
        """Where this service holds *campaign_id*: one directory.

        Public because a caller outside this class reads its files through it -- a
        service-endpoint plugin is handed its ``data_dir`` from here
        (:mod:`robovast.service.endpoint_plugin`), so a plugin reads the same tree
        whichever service serves it.

        Campaigns all live under the shared results root (see :meth:`_campaigns_root`);
        an absolute id is honoured as-is, for analysis of an arbitrary folder.
        """
        if os.path.isabs(campaign_id):
            return Path(campaign_id)
        return self._campaigns_root() / campaign_id

    # -- results data query (eval viewer) -----------------------------------

    def describe_campaign_data(self, campaign_id: str) -> "DataDescribe":
        from robovast.results_processing.data_query import describe_data_db
        from robovast.service.interface import DataDescribe
        result = describe_data_db(self.campaign_dir(campaign_id),
                                  campaign_id=campaign_id)
        return DataDescribe(campaign_id=campaign_id, **result)

    def query_campaign_data_sql(
        self, campaign_id: str, sql: str, max_rows: int = 500,
        max_bytes: int | None = None, campaigns: list | None = None,
    ) -> "DataQueryResult":
        """Run a read-only ``SELECT`` against *campaign_id*, and only against it.

        The query sees that campaign's tables and no other's, so one that forgets
        ``WHERE campaign_id = ...`` answers about this campaign rather than a corpus.

        *campaigns* is the deliberate way out, for a comparison: name every campaign the
        query may see and it may see them.
        """
        from robovast.results_processing.data_query import query_data_db
        from robovast.service.interface import DataQueryResult
        result = query_data_db(self.campaign_dir(campaign_id), sql, max_rows,
                               max_bytes=max_bytes, campaigns=campaigns,
                               campaign_id=campaign_id)
        return DataQueryResult(campaign_id=campaign_id, **result)

    def stream_campaign_query_csv(self, campaign_id: str, sql: str):
        from robovast.results_processing.data_query import stream_query_csv
        return stream_query_csv(self.campaign_dir(campaign_id), sql,
                                campaign_id=campaign_id)

    def list_campaign_plots(self, campaign_id: str) -> "CampaignPlotsResponse":
        # Raw-load (not full validation) — reading declared plots must not depend on
        # the rest of the snapshot config being re-validatable.
        from robovast.common.config import visualization_block
        from robovast.common.config_validation import _safe_load
        from robovast.common.results_utils import vast_in_config_dir
        from robovast.service.interface import CampaignPlotsResponse
        config_dir = self.campaign_dir(campaign_id) / "_config"
        found = vast_in_config_dir(config_dir)
        plots = []
        if found is not None:
            cfg, _ = _safe_load(str(found))
            for p in (visualization_block(cfg, "results", "data_browser", "plots") or []):
                if isinstance(p, dict) and p.get("query"):
                    plots.append({"title": p.get("title", ""), "query": p["query"],
                                  "vega_lite": p.get("vega_lite") or {}})
        return CampaignPlotsResponse(campaign_id=campaign_id, plots=plots)

    def get_config_contribution(self, campaign_id: str,
                                config_name: str) -> "ServedContribution":
        from robovast.common.scene_markers import campaign_contribution
        from robovast.service.interface import ServedContribution
        return ServedContribution.model_validate(
            campaign_contribution(self.campaign_dir(campaign_id), config_name))

    def get_track_deviation(self, campaign_id: str, config_name: str, run_id: int,
                            source: str = "poses", frame: str = "base_link",
                            marker_label=None) -> "TrackDeviation":
        from robovast.results_processing.track_deviation import choose_path, track_deviation
        from robovast.service.interface import TrackDeviation
        contribution = self.get_config_contribution(campaign_id, config_name)
        markers = [m.model_dump() for m in contribution.markers]
        path = choose_path(markers, marker_label)
        return TrackDeviation(**track_deviation(
            self.campaign_dir(campaign_id), config_name, run_id, path=path, source=source,
            frame=frame))

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
        cfg, _ = _safe_load(str(campaign_vast(self.campaign_dir(campaign_id))))
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
        base = (self.campaign_dir(campaign_id) / "_config").resolve()
        target = (base / rel_path).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise ValueError("path escapes the campaign config directory")
        if not target.is_file():
            raise KeyError(f"panel asset not found: {rel_path}")
        return str(target)

    # -- on-demand 3D geometry ---------------------------------------------

    #: The run's ``sim_recording`` row. ``*`` rather than the three columns the identity reads:
    #: a campaign none of whose runs recorded anything has the table with its key columns alone,
    #: and naming a column it lacks answers a binder error where "no row" is the fact.
    _SCENE_RECORDING_SQL = "SELECT * FROM sim_recording WHERE config_name = ? AND run_id = ?"
    #: What the identity reads of that row: the world it was recorded from, the overrides it was
    #: built with, and the format that says what those mean.
    _SCENE_RECORDING_COLUMNS = ("world", "overrides_json", "format_version")

    def _scene_recording(self, campaign_id: str, config_name: str, run_id: str) -> dict:
        """The run's ``sim_recording`` row, which names the world it needs.

        Read through the campaign's tables rather than off a file: the recording is the
        simulator's own format, and the decoder is the one reader of it. A run still going has
        its row as soon as the simulator has written its provenance, which is before its first
        sample, so a preview resolves its world the same way a finished run does.

        Raises ``SceneUnavailable`` when the run has no row -- a run that recorded no simulator
        state has no motion to replay either, so there is nothing for geometry to serve -- or
        when the campaign's tables cannot be opened at all.
        """
        from robovast.results_processing.data_query import (  # pylint: disable=import-outside-toplevel
            DataQueryError, open_data_db)
        from robovast.service.scene_cache import \
            SceneUnavailable  # pylint: disable=import-outside-toplevel
        try:
            db = open_data_db(self.campaign_dir(campaign_id), campaign_id)
            cursor = db.execute(self._SCENE_RECORDING_SQL, (config_name, int(run_id)))
            row = cursor.fetchone()
        except (DataQueryError, ValueError) as err:
            raise SceneUnavailable(
                f"this run's recording could not be read from the campaign's tables: {err}") from err
        if row is None:
            raise SceneUnavailable(
                "this run has no sim_recording row, so there is nothing to replay and no world to "
                "build geometry from. The recording is the simulator backend's to write -- see its "
                "documentation for what enables one -- and a run that never reached its first "
                "sample leaves none.")
        record = dict(zip((d[0] for d in cursor.description), row))
        return {name: record.get(name) for name in self._SCENE_RECORDING_COLUMNS}

    def _run_recording_path(self, campaign_id: str, config_name: str, run_id: str):
        """Where this run's recording sits, or ``None`` for a campaign whose simulator records none
        or whose configuration cannot say."""
        from robovast.common.simulators import \
            run_state_filename  # pylint: disable=import-outside-toplevel
        try:
            filename = run_state_filename(self._campaign_execution(campaign_id))
        except Exception:  # noqa: BLE001 - an unreadable config is reported by the read itself
            return None
        if not filename:
            return None
        return self._run_state_path(campaign_id, config_name, run_id, filename)

    @abstractmethod
    @abstractmethod
    def _scene_runner_context(self, identity: dict, on_wait=None):
        """A zero-argument callable returning a context that yields the runner factory a scene
        build or capture of *identity*'s image runs through.
        """

    @abstractmethod
    def _resolve_image_digest(self, ref: str):
        """The digest an image reference resolves to on this service, or ``""``.
        """

    def _scene_identity(self, campaign_id, config_name, run_id):
        """``(identity, cache key)`` of the geometry this run needs, memoised at rest.

        Asked on every run switch in the run view, and not cheap: it queries the run's
        ``sim_recording`` row, parses the campaign's frozen ``.vast``, hashes every byte of each
        ``_config/`` tree a campaign-file world reads, and for a campaign that recorded only an
        image tag resolves the digest the tag names. None of that can change for a campaign
        nothing is driving, so the answer is kept against :meth:`_rest_key` plus the stat of the
        run's own recording -- the one input the campaign's record files do not cover. A refusal
        is not memoised: it is cheap to repeat, and its cause (a missing recording, an unpulled
        image) may be fixed.
        """
        from robovast.service import scene_cache
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        rest = self._rest_key(campaign_id, entry)
        memo_key = None
        if rest is not None:
            recording = self._run_recording_path(campaign_id, config_name, run_id)
            try:
                st = recording.stat() if recording is not None else None
            except OSError:
                st = None  # no recording: _scene_recording below raises the reason
            if st is not None:
                memo_key = (rest, st.st_mtime_ns, st.st_size)
        run = (config_name, str(run_id))
        if memo_key is not None:
            hit = self._scene_identity_cache.get(campaign_id, {}).get(run)
            if hit is not None and hit[0] == memo_key:
                return hit[1]
        recording_row = self._scene_recording(campaign_id, config_name, run_id)
        identity = scene_cache.world_identity(str(self.campaign_dir(campaign_id)), recording_row,
                                              resolve_digest=self._resolve_image_digest,
                                              config_name=config_name)
        answer = (identity, scene_cache.cache_key(identity))
        if memo_key is not None:
            self._scene_identity_cache.setdefault(campaign_id, {})[run] = (memo_key, answer)
        return answer

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
        stage, stage_detail = self._scene_stage(key) if running else ("", "")
        note = ""
        if cached:
            note = "geometry is cached; nothing will be built"
        elif running:
            note = "building this world's geometry (it is shared by every run that used it)"
            # The reason a wait is not ending belongs in the sentence a client repeats, not only in
            # the field a panel renders: a CLI or an agent reading this status has the note and
            # nothing else.
            note += f"; {stage}" + (f" ({stage_detail})" if stage_detail else "")
        elif failure:
            note = failure
        else:
            note = "geometry has not been built for this world yet"
        if not identity["overrides_known"]:
            note += ("; this run's recording carries no overrides, so geometry is compiled from "
                     "the bare world and may not reflect per-config world overrides")
        return base.model_copy(update={
            "cached": cached,
            "generation_required": not cached,
            "in_progress": running,
            "stage": stage,
            "stage_detail": stage_detail,
            "bytes": scene_cache.entry_bytes(key) if cached else 0,
            "url": (Routes.campaign_scene_asset(campaign_id, f"{key}/scene.json")
                    if cached else ""),
            "world": identity["world"],
            "overrides_known": identity["overrides_known"],
            "error": failure,
            "note": note,
        })

    @staticmethod
    def _scene_stage(key: str) -> "tuple[str, str]":
        """``(stage, detail)`` for a build in flight, as the build itself reported it.

        A read rather than a guess: whoever performs a step names it (see
        ``scene_cache.set_stage``), which is the only place that can tell a pull from a compile.
        The default covers the moment between a build taking the key's lock and naming its first
        step, where compiling is what is about to happen anyway.
        """
        from robovast.service import scene_cache
        stage, detail = scene_cache.current_stage(key)
        return stage or scene_cache.STAGE_COMPILING, detail

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

        runner_context = self._scene_runner_context(
            identity, on_wait=lambda stage, detail: scene_cache.set_stage(key, stage, detail))

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
        stage, stage_detail = self._scene_stage(key) if running else ("", "")
        return base.model_copy(update={
            "cached": cached,
            "generation_required": not cached,
            "in_progress": running,
            "stage": stage,
            "stage_detail": stage_detail,
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
        runner_context = self._scene_runner_context(
            identity, on_wait=lambda stage, detail: scene_cache.set_stage(key, stage, detail))

        def work():
            try:
                scene_cache.generate(identity, key, runner_context=runner_context)
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
        """Where this run's recording sits."""
        return (self.campaign_dir(campaign_id) / config_name / str(run_id)
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
            runner_context=self._scene_runner_context(identity)))

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
        info = get_visualization(self.campaign_dir(campaign_id))
        return PanelsSource(campaign_id=campaign_id, content=info["content"])

    def update_panels_source(self, request) -> "PanelsSource":
        from robovast.service.interface import PanelsSource
        from robovast.service.postprocessing_edit import update_visualization
        update_visualization(self.campaign_dir(request.campaign_id), request.content)
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
        from robovast.common.results_utils import vast_in_config_dir
        config_dir = self.campaign_dir(campaign_id) / "_config"
        found = vast_in_config_dir(config_dir)
        workloads: dict = {}
        if found is not None:
            cfg, _ = _safe_load(str(found))
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

        A seam, not a feature, at this level: the data is already under the results root,
        so the only cost is the cells themselves and there is nowhere to
        publish counts to. An implementation that reaches this render at the tail of a
        longer wait yields a reporter instead.
        """
        del campaign_id, workload
        yield None

    def _node_data_dir(self, campaign_id: str, level: str, config_name: str, run_id):
        """The ``DATA_DIR`` for a selected node — the campaign/config/run directory."""
        base = Path(self.campaign_dir(campaign_id))
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
    def _is_done(entry: _TrackedCampaign) -> bool:
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
    #: carries the run tallies and the mode; ``outcome.json`` the terminal outcome;
    #: ``_transient/postprocessing.yaml`` is what ``postprocessed`` is derived from (see
    #: :func:`~robovast.common.campaign_data.campaign_has_derived_data`).
    #:
    #: This is a stat fingerprint, so an entry that can never exist is worse than a missing
    #: one: it contributes nothing that can change, so postprocessing finishing would change
    #: nothing in the tuple, and a card could sit at "not postprocessed" over a campaign
    #: whose tables were all there, until some unrelated file's mtime moved.
    #:
    #: The SQLite sidecars are listed because a store in WAL mode commits into ``-wal`` and
    #: can leave the main file's mtime standing still -- no journal_mode is set today, so
    #: this is insurance against a later change silently freezing every card rather than a
    #: current requirement.
    _REST_FILES = ("campaign.db", "campaign.db-wal", "campaign.db-journal",
                   "_execution/outcome.json", "_transient/postprocessing.yaml")

    def _rest_dir(self, cid: str) -> Optional[Path]:
        """The campaign's directory **if it holds a record**, else ``None``.

        ``None`` means there is nothing to key a cached summary on -- a campaign whose
        store has not been written yet -- and the caller takes the full path.
        """
        local = self.campaign_dir(cid)
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

    def _prior_outcome(self, cid: str) -> Status:
        """The campaign's recorded Status, for an entry that is about to answer for it.

        The same read :meth:`_summary_for` falls back to when nothing is tracking the
        campaign — so a campaign reports the same errors while an operation runs on it as it
        did the moment before. A campaign with no record yet (an import, whose bytes are still arriving)
        reconstructs as ``unknown`` with every field empty, which seeds nothing and is
        the honest answer.

        Never raises: this decides what a listing *says*, and a status read that fails
        over an unreachable record dir would take the dispatch with it.
        """
        from robovast.execution.status_recovery import reconstruct_status_from_disk
        try:
            return reconstruct_status_from_disk(self.campaign_dir(cid))
        except OSError:
            return Status(phase=Phase.UNKNOWN, campaign_id=cid)

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
        campaign_dir = self.campaign_dir(cid)
        # One precedence rule, shared with get_status: a tracked campaign's live
        # ControllerState wins; otherwise reconstruct the Status from disk (the one
        # documented recovery path — it also derives `postprocessed` from the provenance
        # record).
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
            error=(snap.error or "").strip().splitlines()[0] if snap.error else "",
            # From the same snapshot as everything above, so a row cannot show a size that
            # belongs to a different reading of the campaign than its phase does.
            results_bytes=snap.results_bytes,
            # The queue's own answer, and only for a campaign it still holds: a finished
            # campaign has no standing with it, and reporting a rank for one would describe
            # something nothing can act on.
            **self._scheduling_for(cid, live=entry is not None))
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

    def _results_bytes_for(self, cid: str) -> Optional[int]:
        """The results size of *cid* in bytes, or None when none is recorded.

        What ``list_campaigns`` orders by under ``sort="size"``, read by the same precedence
        :meth:`_summary_for` reads the row's figure by -- the live snapshot of a tracked
        campaign, otherwise its durable record -- so the size a row shows and the size it was
        sorted by cannot disagree. Asked of every campaign before the page is cut, which is
        why it does not build a summary: it is one memoised read per campaign.

        Unlike :meth:`_campaign_fact` this caches ``None`` too, once a terminal record says
        it: a campaign that ended unmeasured stays unmeasured, and re-reading its record
        every second to learn that again is the per-tick I/O the listing must not do.
        """
        with self._lock:
            entry = self._campaigns.get(cid)
            if entry is None and cid in self._results_bytes_cache:
                return self._results_bytes_cache[cid]
        if entry is not None:
            return entry.state.snapshot().results_bytes
        settled, value = read_campaign_results_bytes(self.campaign_dir(cid))
        if settled:
            with self._lock:
                self._results_bytes_cache[cid] = value
        return value

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
        answer -- and reading it would cost a per-campaign glob on the listing's hot path.
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
        value = from_disk(self.campaign_dir(cid))
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
        status = reconstruct_status_from_disk(self.campaign_dir(campaign_id))
        if key is not None:
            self._disk_status_cache[campaign_id] = (key, status)
        return status


def _workspace_sha(spec) -> str:
    """Fingerprint of the workspace *spec* stages, or ``""`` when it stages none.

    Every file's relative path, size and inode timestamps, sorted -- deliberately NOT its
    bytes. This runs on every exec and every query, and reading a workspace carrying
    meshes would put a full tree read on the warm path this pool exists to keep warm.
    Stat answers the question that is actually being asked: has the tree changed since a
    container was staged from it?

    ``st_ctime_ns`` as well as ``st_mtime_ns`` because only the first is beyond a writer's
    reach: a tree restored by something that preserves mtime -- rsync -t, a tar extract --
    still moves ctime. Paths and sizes are in it so an added, removed or renamed file is a
    different tree whatever the clock did.

    The residual: a file rewritten to the SAME size within one filesystem timestamp tick
    fingerprints equal. That tick is 1 ms on ext4, measured rather than assumed, and it is
    the window in which a container would also have to be staged for a stale answer to
    reach anyone. Through the service's own API, the only writer, that means two different
    versions of one file written a millisecond apart with identical length. Content hashing
    is what closes it, at the cost this exists to avoid -- so if it ever bites, that is the
    trade to revisit, not this function's inputs.

    Empty for a campaign, which is frozen once it starts and is therefore identified by
    its id alone -- so this adds nothing to a campaign's identity and cannot make two
    equal campaigns look different.

    A tree that cannot be read fingerprints as unreadable rather than as empty: a
    workspace whose directory is missing is not the same tree as every other unreadable
    one, and returning "" would let it share a held container with a campaign.
    """
    workspace_dir = getattr(spec, "workspace_dir", "")
    if not workspace_dir:
        return ""
    root = Path(workspace_dir)
    digest = hashlib.sha256()
    try:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            stat = path.stat()
            digest.update(str(path.relative_to(root)).encode())
            digest.update(
                f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}".encode())
    except OSError as err:
        logger.debug("could not fingerprint workspace %s: %s", workspace_dir, err)
        return f"unreadable:{workspace_dir}"
    return digest.hexdigest()
