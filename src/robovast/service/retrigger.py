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

"""Relaunch a campaign from what the campaign itself recorded.

A retrigger reads a finished campaign's frozen ``_config/`` and its ``_execution/`` records,
and launches a **new** campaign from them. The source is never written to. It exists because
the workspace a campaign came from is not linked to it and may be gone: a campaign's
``_config/`` is already the single source of truth for its configuration (it is what the
postprocessing dialog edits in place), so it is also the honest thing to relaunch from.

**The image is pinned, never rebuilt.** A campaign's build context -- the wheels and sources
its ``build:`` section names -- is not archived in its results, and the built ref is a hash
over that context. So a retrigger reuses the exact image the campaign recorded
(:func:`~robovast.common.campaign_data.campaign_pinned_images`) and refuses when the campaign
never recorded one. Rebuilding is not a fallback that could be added later; the inputs are
gone.

**A retrigger replays the launch, but re-expands the campaign.** The recorded
``_execution/launch.yaml`` gives back the ``config_filter`` and the requested ``runs``, so
re-running a one-config pilot stays a one-config pilot. Everything downstream of that is
computed afresh, which is visible in one place: ``execution.generate`` generators re-run (their
cache is not archived), so a stochastic generator draws new samples.

This module is the pure half -- it takes a source directory and returns data. Ordering,
threads and lanes belong to the transport (``LocalTransport.retrigger_campaign``), which is a
thin orchestrator over :func:`prepare`. Same split as
:mod:`robovast.service.postprocessing_edit`.
"""

import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Where staged project trees live: one directory per retrigger, under the workspaces root.
#: Dot-prefixed deliberately -- ``_project_for_workspace`` skips dot components when it looks
#: for a pinned workspace's ``.vast``, so a staged copy can never be mistaken for a project the
#: service was told to run. It is also *not* under the results root, which is scanned with
#: ``is_campaign_dir``.
STAGING_DIRNAME = ".retriggers"


class RetriggerRefused(ValueError):
    """This campaign cannot be relaunched from its results, and no retry would help.

    Carries ``include_traceback = False``: every message names what was missing and what to do
    instead, and a stack would point at this module rather than at the campaign that is the
    subject. Same convention as the other self-contained launch failures.
    """

    include_traceback = False


@dataclass
class RetriggerPlan:
    """Everything the transport needs to launch the retriggered campaign.

    ``materialize`` / ``discard`` are the staged tree's lifecycle, handed to
    ``WorkspaceTarget`` so the launch path drives them at the two moments it knows about: the
    top of the campaign's worker, and every way that worker can end.
    """

    #: The staged project directory. Already exists, holding the ``.vast`` alone.
    staging_dir: Path
    #: The staged ``.vast`` -- what the campaign runs.
    config_path: str
    #: The replayed launch, ready for ``_launch_campaign``.
    request: object
    #: ``{container: image}`` for the containers that build; empty when none do.
    pinned_images: dict
    #: Finish staging the tree (called on the worker).
    materialize: Callable[[], None]
    #: Delete the staged tree. Idempotent, so the failure paths can call it freely.
    discard: Callable[[], None]


def staging_root(workspaces_root) -> Path:
    """The parent of every staged tree, given the workspaces root."""
    return Path(workspaces_root) / STAGING_DIRNAME


def prepare(source_dir, source_id: str, *, workspaces_root, description_limit: int,
            request_model) -> RetriggerPlan:
    """Read a campaign's records and stage what a new run of it needs.

    Everything here is synchronous and cheap: one small ``.vast`` read, three record reads, and
    a directory. The bulk copy is deferred to :attr:`RetriggerPlan.materialize` so the POST
    still returns as soon as the campaign is named. Every reason a campaign *cannot* be
    relaunched is decided here, though, so the caller answers with the reason instead of
    launching a campaign that will fail.

    Args:
        source_dir: the source campaign's directory, already materialised for this lane.
        source_id: its campaign id -- used in the new campaign's description.
        workspaces_root: where :data:`STAGING_DIRNAME` is created.
        description_limit: ``DESCRIPTION_MAX_LEN``, passed in so this module needs no import
            from the service interface beyond *request_model*.
        request_model: the ``CreateCampaignRequest`` class, injected for the same reason.

    Raises:
        RetriggerRefused: the campaign froze no config, or runs a built image it never
            recorded.
    """
    from robovast.common.campaign_data import (CampaignImageUnpinnable,
                                               campaign_pinned_images,
                                               read_execution_metadata,
                                               read_launch_record)
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    from robovast.common.results_utils import campaign_vast

    source_dir = Path(source_dir)
    try:
        vast_path = campaign_vast(source_dir)
    except ValueError as e:
        raise RetriggerRefused(
            f"cannot retrigger {source_id!r}: it has no frozen configuration under "
            f"_config/, so there is nothing to reconstruct from ({e}). A campaign that "
            f"failed before its configuration was frozen has to be launched again from the "
            f"workspace it came from.") from e

    campaign_config = validate_config(load_config(str(vast_path)))

    # The images first: it is the refusal most likely to fire, and it needs no directory.
    try:
        pinned = campaign_pinned_images(source_dir)
    except CampaignImageUnpinnable as e:
        raise RetriggerRefused(
            f"cannot retrigger {source_id!r}: {e} Launch it again from the workspace it came "
            f"from, which still has the sources the image is built out of.") from e
    if not pinned and _builds_an_image(campaign_config):
        # It built something and recorded no container image — the shape of a campaign that died
        # before its first batch. There is nothing to reuse and nothing to rebuild from.
        raise RetriggerRefused(
            f"cannot retrigger {source_id!r}: it builds its own image and recorded none "
            f"(no usable _execution/execution.yaml — it failed before its first batch "
            f"finished). A campaign's build context (wheels, sources) is not archived in its "
            f"results, so the image cannot be rebuilt from them either. Launch it again from "
            f"the workspace it came from.")

    request = _replay_request(source_dir, source_id, request_model=request_model,
                              description_limit=description_limit,
                              read_launch_record=read_launch_record,
                              read_execution_metadata=read_execution_metadata)

    staging_dir = _make_staging_dir(workspaces_root, source_id)
    staged_vast = staging_dir / vast_path.name
    shutil.copy2(vast_path, staged_vast)

    def _discard() -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return RetriggerPlan(
        staging_dir=staging_dir,
        config_path=str(staged_vast),
        request=request,
        pinned_images=pinned,
        materialize=lambda: stage_project(source_dir, staging_dir, campaign_config),
        discard=_discard,
    )


def _builds_an_image(campaign_config) -> bool:
    """Whether any container declares packages, i.e. whether this campaign builds at all.

    A boolean rather than the container names on purpose. Naming them would mean resolving
    *which* container each declaration ends up on, and that fold is only known after
    ``apply_backend``, which loads the simulator plugin — not installed in this process, so for
    any plugin-backed project (every campaign naming a ``backend``) it raises "Unknown
    robovast.simulators plugin". The build path gets away with it because ``_build_specs_for``
    installs the
    plugins first; this runs in the request handler, where a pip install does not belong.

    The fold is not needed here anyway: the campaign already recorded which containers it ran
    (``campaign_pinned_images``), so all this has to answer is whether an empty pin set means
    "nothing to pin" or "the record is missing something it should have had".
    """
    execution = getattr(campaign_config, "execution", None)
    containers = getattr(execution, "containers", None) or {}
    for block in containers.values():
        block = block if isinstance(block, dict) else block.model_dump()
        if block.get("system_packages") or block.get("python_packages"):
            return True
    return False


def _make_staging_dir(workspaces_root, source_id: str) -> Path:
    """A fresh staged-tree directory named after the campaign it came from.

    Named after the **source** rather than the new campaign so that no id has to be minted
    before the tree exists -- the id comes from the ``.vast`` this directory is about to hold.
    The random suffix keeps concurrent retriggers of one campaign apart.
    """
    root = staging_root(workspaces_root)
    root.mkdir(parents=True, exist_ok=True)
    staging_dir = root / f"{source_id}-{secrets.token_hex(3)}"
    staging_dir.mkdir()
    return staging_dir


def _replay_request(source_dir: Path, source_id: str, *, request_model, description_limit,
                    read_launch_record, read_execution_metadata):
    """Rebuild the launch request from what the campaign recorded.

    ``launch.yaml`` is the answer when it is there. When it is not -- a campaign from before
    that record existed -- ``runs`` still comes from ``execution.yaml``'s effective count, but
    the ``config_filter`` is simply unrecoverable, and this says so in the description rather
    than quietly turning a one-config pilot into a full sweep. Whoever reads the new campaign
    can then see which it was.
    """
    from robovast.common.store import read_campaign_description

    launch = read_launch_record(source_dir) or {}
    try:
        meta = read_execution_metadata(source_dir)
    except FileNotFoundError:
        # A campaign that failed before its first batch. Not fatal: `runs` falls back to the
        # .vast's own execution.runs, which is what runs=0 means downstream.
        meta = {}

    note = ""
    if launch:
        runs = int(launch.get("runs") or 0)
        config_filter = str(launch.get("config_filter") or "")
    else:
        runs = int(meta.get("runs") or 0)
        config_filter = ""
        note = " [no launch record: running every configuration]"

    # The description lives in campaign.db, not in the launch record -- it is not a launch
    # parameter, and it is already what listings show.
    description = f"retrigger of {source_id}"
    original = (read_campaign_description(source_dir) or "").strip()
    if original:
        description = f"{description}: {original}"
    description = (description + note)[:description_limit]

    return request_model(
        workspace_id="",
        config_filter=config_filter,
        # Replayed so the new id keeps the same name stem and sorts beside the source's.
        campaign_name=str(launch.get("campaign_name") or ""),
        runs=runs,
        # A retrigger is nobody sitting at a screen, whatever the original launch asked for.
        show_gui=False,
        postprocess=bool(launch.get("postprocess", True)),
        upload_to_share=bool(launch.get("upload_to_share", False)),
        description=description,
    )


def stage_project(source_dir, staging_dir, campaign_config) -> None:
    """Copy the campaign's frozen config into *staging_dir* and check it is complete.

    Runs on the campaign's worker thread: ``_config/`` is small but it is a per-object fetch on
    the cluster lane, and a slow or failing read has to become an inspectable failed campaign
    rather than a hung request.
    """
    source_dir, staging_dir = Path(source_dir), Path(staging_dir)
    reconstruct_project(source_dir, staging_dir, campaign_config)
    missing = missing_run_files(source_dir, staging_dir)
    if missing:
        raise RetriggerRefused(
            "the campaign's frozen _config/ is missing "
            f"{len(missing)} file(s) its own run recorded using: {', '.join(sorted(missing))}. "
            "Relaunching from it would run a different configuration, so this refuses instead. "
            "Launch it again from the workspace it came from.")


def reconstruct_project(source_dir, staging_dir, campaign_config) -> None:
    """Rebuild a runnable project tree from a campaign's frozen ``_config/``.

    The reconstruction alone, without the completeness check: shared with the service's
    "create a workspace from this campaign's config", which reports an incomplete snapshot in
    its own words because nothing is being relaunched there.
    """
    source_dir, staging_dir = Path(source_dir), Path(staging_dir)
    config_dir = source_dir / "_config"
    for src in sorted(config_dir.rglob("*")):
        if src.is_dir():
            continue
        dst = staging_dir / src.relative_to(config_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():          # the .vast is already there, copied by prepare()
            shutil.copy2(src, dst)

    _place_scenario(staging_dir, campaign_config)


def _place_scenario(staging_dir: Path, campaign_config) -> None:
    """Put the scenario where the ``.vast`` says it is.

    ``_config/`` stores the scenario at its **basename**, but ``execution.scenario_file`` may
    name a subdirectory, and config generation requires the file at the declared relative path.
    So the basename is copied into place. The ``.vast`` is deliberately *not* rewritten to point
    at the flat copy: its config block feeds ``compute_config_identifier``, and changing it
    would make the retriggered campaign's configs incomparable with the source's.
    """
    declared = getattr(getattr(campaign_config, "execution", None), "scenario_file", "") or ""
    if not declared or not os.path.dirname(declared):
        return                        # already flat: the staged basename is the declared path
    target = staging_dir / declared
    if target.exists():
        return
    flat = staging_dir / os.path.basename(declared)
    if not flat.is_file():
        raise RetriggerRefused(
            f"the campaign's frozen _config/ has no scenario file {os.path.basename(declared)!r} "
            f"to place at the {declared!r} its .vast declares.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(flat, target)


def missing_run_files(source_dir, staging_dir) -> list:
    """Files the source campaign ran with that the staged tree does not have.

    Worth checking because the failure it prevents is silent: ``execution.run_files`` is a list
    of globs, and a glob that matches nothing is only a warning during config generation. A
    ``_config/`` that never archived a params file would therefore produce a campaign that runs,
    and runs *differently*, with nothing in its log saying so.

    ``_transient/configurations.yaml`` records the expanded file list from the original run, so
    it is the thing to compare against; an absent record makes no coverage claim and yields no
    findings. ``_input_files`` are only warned about -- the original staging also skipped a
    missing one, so reporting them would condemn campaigns that were already short an analysis
    notebook when they ran.
    """
    import yaml
    source_dir, staging_dir = Path(source_dir), Path(staging_dir)
    recorded = source_dir / "_transient" / "configurations.yaml"
    if not recorded.is_file():
        return []                     # nothing to compare against; the coverage claim is unmade
    with open(recorded, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    absent_inputs = [rel for rel in (data.get("_input_files") or [])
                     if not (staging_dir / rel).is_file()]
    if absent_inputs:
        logger.warning(
            "Campaign config snapshot archived no %s; the original run skipped them too, so a "
            "project rebuilt from it is short the same files.", ", ".join(sorted(absent_inputs)))

    return [rel for rel in (data.get("_run_files") or [])
            if not (staging_dir / rel).is_file()]


def sweep_orphans(workspaces_root, campaigns_root, is_live: Optional[Callable[[str], bool]] = None) -> int:
    """Delete staged trees left behind by a killed service; return how many.

    The worker's ``finally`` covers every way a campaign can end, so this only ever collects
    what a SIGKILL orphaned. It is deliberately conservative: two ``vast serve`` processes can
    share one workspaces root -- the registry takes a file lock for exactly that reason -- so a
    tree is removed only when its campaign is *provably* finished (a terminal outcome on disk)
    or *provably* gone (no campaign directory at all). Anything that still looks live is left
    for the next start.

    *is_live* lets the caller exclude campaigns this process is currently driving, whose
    directories exist and have no outcome yet.
    """
    root = staging_root(workspaces_root)
    if not root.is_dir():
        return 0
    removed = 0
    for staged in sorted(root.iterdir()):
        if not staged.is_dir():
            continue
        # "<source-campaign-id>-<hex>" -- the source is what the name records, and the new
        # campaign it fed is not knowable from here. What matters is only whether anything is
        # still using this tree, and after a restart nothing is.
        if is_live is not None and is_live(staged.name):
            continue
        campaign_dir = Path(campaigns_root) / staged.name.rsplit("-", 1)[0]
        outcome = campaign_dir / "_execution" / "outcome.json"
        if campaign_dir.is_dir() and not outcome.is_file():
            logger.debug("Leaving staged tree %s: its campaign has no terminal outcome yet",
                         staged.name)
            continue
        shutil.rmtree(staged, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("Removed %d orphaned staged project tree(s) under %s", removed, root)
    return removed
