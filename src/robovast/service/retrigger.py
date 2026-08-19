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
from typing import Any, Callable, Optional

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
    #: The replayed launch, ready for ``_launch_campaign``. Typed ``Any`` rather than
    #: ``object``: ``prepare`` is handed the model class as ``request_model`` so this module
    #: never imports the interface, and what lands here is that model's instance. ``object``
    #: said the opposite of what was meant -- that only base-class attributes exist -- and
    #: every read of a field on it was a true positive against the annotation.
    request: Any
    #: ``{container: image}`` for the containers that build; empty when none do.
    pinned_images: dict
    #: Finish staging the tree (called on the worker).
    materialize: Callable[[], None]
    #: Delete the staged tree. Idempotent, so the failure paths can call it freely.
    discard: Callable[[], None]
    #: ``{from, to, steps}`` when the source's config had to be migrated, else ``None``.
    #: Carried so the new campaign can record that it is a *migrated* re-run rather than a
    #: native one -- two runs of "the same campaign" that read different config versions are
    #: not the same experiment, and a reader comparing their results has to be able to see it.
    config_migration: "dict | None" = None


#: Per-axis verdicts a pre-flight can return. A campaign is re-runnable only if every axis
#: holds, and they fail independently -- which is why the report is per-axis rather than one
#: boolean. Reducing five independent answers to "no" throws away the only useful part.
AXIS_OK = "ok"                    # nothing to do
AXIS_UPGRADABLE = "upgradable"    # not current, but the ladder can carry it forward
AXIS_UNKNOWN = "unknown"          # predates the record; not a failure, and not a pass either
AXIS_BLOCKED = "blocked"          # cannot proceed, and the message says what to do

#: Axis verdicts that stop a retrigger. ``unknown`` deliberately does not: campaigns recorded
#: before a given field existed are the ones this exists to rescue, and refusing them for
#: lacking a record nobody wrote would defeat the purpose.
BLOCKING_VERDICTS = (AXIS_BLOCKED,)


def _axis(verdict: str, detail: str, **extra) -> dict:
    """One axis of a pre-flight report."""
    return {"verdict": verdict, "detail": detail, **extra}


def check(source_dir, source_id: str) -> dict:
    """Can this campaign be re-run? Answer without staging anything or spending compute.

    :func:`prepare` can only answer this by *doing* it -- it stages a tree, then raises
    :class:`RetriggerRefused` -- so "is this worth trying?" cost a staging directory and gave
    one reason at a time. This walks the same records and reports **every** axis at once, so a
    caller learns that the config needs migrating *and* that the image is gone, rather than
    fixing one to discover the other.

    Returns ``{campaign_id, runnable, blocking, axes: {...}}`` where each axis carries a
    verdict (:data:`AXIS_OK` / :data:`AXIS_UPGRADABLE` / :data:`AXIS_UNKNOWN` /
    :data:`AXIS_BLOCKED`) and a human-readable ``detail``. Every blocking detail must name the
    artifact and how to obtain it; a verdict a reader cannot act on is not worth returning.

    The five axes, which fail independently:

    ``config``
        Is the frozen ``.vast`` readable, and at which version.
    ``host``
        Does this robovast still speak the recorded image's container protocol.
    ``images``
        Can a new run start from the images the campaign recorded.
    ``plugins``
        Were third-party ``plugins:`` resolved to something re-installable.
    ``providers``
        Which asset-provider distributions supplied it, and can they be obtained.
    """
    source_dir = Path(source_dir)
    axes = {
        "config": _check_config(source_dir),
        "images": _check_images(source_dir),
        "plugins": _check_plugins(source_dir),
        "providers": _check_providers(source_dir),
    }
    axes["host"] = _check_host(source_dir, axes["images"])
    blocking = sorted(name for name, axis in axes.items()
                      if axis["verdict"] in BLOCKING_VERDICTS)
    return {"campaign_id": source_id, "runnable": not blocking,
            "blocking": blocking, "axes": axes}


def _check_config(source_dir: Path) -> dict:
    """Whether the frozen ``.vast`` can be brought to the current config version."""
    from robovast.common.migrations import (SUPPORTED_CONFIG_VERSION, ConfigVersionError,
                                            config_version, upgrade_config)
    from robovast.common.results_utils import campaign_vast

    try:
        vast_path = campaign_vast(source_dir)
    except ValueError as e:
        return _axis(AXIS_BLOCKED,
                     f"no frozen configuration under _config/, so there is nothing to "
                     f"reconstruct from ({e}). Launch it again from the workspace it came "
                     f"from.")
    try:
        raw = _read_vast(vast_path)
    except Exception as e:  # pylint: disable=broad-except
        return _axis(AXIS_BLOCKED, f"{vast_path.name} could not be read: {e}")

    version = config_version(raw)
    if version == SUPPORTED_CONFIG_VERSION:
        return _axis(AXIS_OK, f"config version {version} is current", version=version)
    try:
        _, applied = upgrade_config(raw)
    except ConfigVersionError as e:
        return _axis(AXIS_BLOCKED, str(e), version=version)
    return _axis(AXIS_UPGRADABLE,
                 f"config version {version} will be migrated to "
                 f"{SUPPORTED_CONFIG_VERSION} in the staging copy; the archived file is not "
                 f"modified",
                 version=version, steps=applied)


def _read_vast(vast_path: Path) -> dict:
    """The first YAML document of a frozen ``.vast``, unvalidated.

    Read directly rather than through ``load_config``: this is a *diagnosis* of a file that may
    well be too old to validate, and the strict reader would raise before the report could say
    so -- turning the answer into the failure it was asked about.
    """
    import yaml

    with open(vast_path, "r", encoding="utf-8") as handle:
        documents = list(yaml.safe_load_all(handle))
    return (documents[0] if documents else None) or {}


def _check_images(source_dir: Path) -> dict:
    """Whether a new run can start from the images this campaign recorded."""
    from robovast.common.campaign_data import CampaignImageUnpinnable, campaign_pinned_images

    try:
        pinned = campaign_pinned_images(source_dir)
    except CampaignImageUnpinnable as e:
        return _axis(AXIS_BLOCKED,
                     f"{e} Launch it again from the workspace it came from, which still has "
                     f"the sources the image is built out of.")
    if not pinned:
        return _axis(AXIS_UNKNOWN,
                     "no container image recorded (no usable _execution/execution.yaml). If "
                     "the campaign builds its own image there is nothing to reuse; otherwise "
                     "the backend supplies one at launch.")
    return _axis(AXIS_OK, f"{len(pinned)} image(s) recorded and pinnable"
                          + _lock_note(pinned), images=dict(pinned),
                 locks=_available_locks(pinned))


def _available_locks(pinned: dict) -> dict:
    """``{role: {apt: n, pip: n}}`` for every recorded image whose build lock can be read.

    Reported because it answers a question the digest cannot: *if this image is gone, would a
    rebuild install the same software?* With the lock, yes -- the author's loose specs can be
    replaced by the versions that actually ran. Without it, a rebuild re-resolves them and gets
    whatever is current, which is a different experiment wearing the same name.

    Only images already present locally can be asked, so an empty answer means "cannot tell here",
    not "no lock" -- the same rule every probe in this pre-flight follows.
    """
    from robovast.service.image_build import read_image_build_manifest

    out = {}
    for role, image in sorted((pinned or {}).items()):
        lock = read_image_build_manifest(image)
        if lock:
            out[role] = {kind: len(entries) for kind, entries in sorted(lock.items())}
    return out


def _lock_note(pinned: dict) -> str:
    locks = _available_locks(pinned)
    if not locks:
        return ""
    return (f"; {len(locks)} carry a build lock, so a rebuild could install the same "
            f"package versions")


def _check_host(source_dir: Path, images_axis: dict) -> dict:
    """Whether this robovast still speaks the recorded image's container protocol.

    Depends on the images axis rather than re-deriving the refs: if the images are not pinnable
    there is nothing to ask about, and reporting a protocol verdict for an image nobody can
    obtain would be noise on top of the real problem.
    """
    from robovast.common.execution import (COMPAT_VERSION, MIN_IMAGE_COMPAT, check_image_compat,
                                           image_compat_version)

    window = f"{MIN_IMAGE_COMPAT}..{COMPAT_VERSION}"
    images = images_axis.get("images") or {}
    if not images:
        return _axis(AXIS_UNKNOWN,
                     f"no recorded image to check against; this host speaks {window}")

    reports = {}
    for role, image in sorted(images.items()):
        version, source = image_compat_version(image)
        problem = check_image_compat(image, version=version, source=source)
        reports[role] = {"image": image, "protocol": version, "source": source,
                         "problem": problem}
    blocked = {role: r for role, r in reports.items() if r["problem"] and r["protocol"] is not None}
    if blocked:
        return _axis(AXIS_BLOCKED,
                     " ".join(r["problem"] for r in blocked.values()), roles=reports)
    unknown = [role for role, r in reports.items() if r["protocol"] is None]
    if unknown:
        return _axis(AXIS_UNKNOWN,
                     f"could not read the container protocol of {', '.join(sorted(unknown))} "
                     f"-- the image is not available locally, or predates the marker. This "
                     f"host speaks {window}.", roles=reports)
    return _axis(AXIS_OK, f"every recorded image is within {window}", roles=reports)


def _check_plugins(source_dir: Path) -> dict:
    """Whether third-party ``plugins:`` resolved to something re-installable."""
    from robovast.common.campaign_data import read_plugins_record

    record = read_plugins_record(source_dir)
    if record is None:
        return _axis(AXIS_UNKNOWN,
                     "no plugin resolution recorded. If the campaign declared plugins, a "
                     "re-run resolves its specs afresh -- a floating ref such as '@main' will "
                     "install different code than the campaign used.")
    floating = sorted(name for name, info in record.items()
                      if not info.get("version") and not info.get("commit"))
    if floating:
        return _axis(AXIS_UNKNOWN,
                     f"declared but not resolved here: {', '.join(floating)} -- these were "
                     f"already importable from elsewhere, so the code that ran came from a "
                     f"location this record cannot name.", plugins=record)
    return _axis(AXIS_OK, f"{len(record)} plugin(s) recorded with resolved versions",
                 plugins=record)


def _check_providers(source_dir: Path) -> dict:
    """Which asset-provider distributions supplied the campaign."""
    from robovast.common.campaign_data import read_providers_record

    record = read_providers_record(source_dir)
    if record is None:
        return _axis(AXIS_UNKNOWN,
                     "no asset providers recorded; a campaign from before this was captured "
                     "cannot say which world and model packages supplied it.")
    return _axis(AXIS_OK, f"{len(record)} asset provider(s) recorded", providers=record)


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
    from robovast.common.campaign_data import (CampaignImageUnpinnable, campaign_pinned_images,
                                               read_execution_metadata, read_launch_record)
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    from robovast.common.migrations import UnmigratableConfig
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

    # `upgrade=True`, and BEFORE validate_config -- this is correctness, not convenience.
    # Everything below reads the loaded config: `_builds_an_image` inspects
    # execution.containers, and stage_project/reconstruct_project walk it. A version-1 config
    # has no execution.containers at all (it carried a top-level `build:`), so reading it with
    # post-v1 expectations silently answers "builds nothing" and the retrigger takes the wrong
    # branch. Strict loading would instead refuse outright, making every campaign older than
    # the current version un-retriggerable -- which is the case this exists for.
    try:
        campaign_config = validate_config(load_config(str(vast_path), upgrade=True))
    except UnmigratableConfig as e:
        # Not a dead end: the ladder got part of the way and marked what it could not carry, so
        # the useful move is to hand that to a person. The message names the command that does it
        # rather than describing the situation and stopping.
        raise RetriggerRefused(
            f"cannot retrigger {source_id!r}: its config cannot be brought forward "
            f"automatically. {e}\n"
            f"  Materialise it as a workspace to finish by hand:\n"
            f"      vast exec retrigger {source_id} --to-workspace <name>") from e
    config_migration = _config_migration_of(vast_path)

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
    # Copy first, then migrate the COPY. The archived _config/*.vast is the record of what its
    # author wrote and is never rewritten; copy2 keeps their comments, and upgrading the staged
    # file in place keeps them through the migration too -- so if anyone opens the staged
    # config, the notes explaining it are still there.
    shutil.copy2(vast_path, staged_vast)
    if config_migration:
        from robovast.common.migrations import upgrade_config_file
        upgrade_config_file(staged_vast, write=True)
        logger.info("retrigger of %s: migrated its config %s -> %s (%s); the archived copy is "
                    "unchanged", source_id, config_migration["from"], config_migration["to"],
                    ", ".join(config_migration["steps"]))

    def _discard() -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return RetriggerPlan(
        staging_dir=staging_dir,
        config_path=str(staged_vast),
        request=request,
        pinned_images=pinned,
        config_migration=config_migration,
        materialize=lambda: stage_project(source_dir, staging_dir, campaign_config),
        discard=_discard,
    )


def _config_migration_of(vast_path: Path) -> "dict | None":
    """``{from, to, steps}`` when the frozen config needs migrating, else ``None``.

    Reported so the retriggered campaign can record that it was *migrated* rather than native.
    Without it two runs of "the same campaign" are indistinguishable from two runs of the same
    config, which is exactly the kind of difference a reader comparing their results has to be
    able to see.
    """
    from robovast.common.migrations import (SUPPORTED_CONFIG_VERSION, config_version,
                                            needs_upgrade, upgrade_config)

    raw = _read_vast(vast_path)
    if not needs_upgrade(raw):
        return None
    _, applied = upgrade_config(raw)
    return {"from": config_version(raw), "to": SUPPORTED_CONFIG_VERSION, "steps": applied}


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
