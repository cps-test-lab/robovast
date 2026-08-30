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

"""Shared data-gathering functions for campaign results.

These functions provide a common interface for reading campaign data,
used by both MCP plugins and the FAIR metadata generator.
"""

import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml


def read_execution_metadata(campaign_dir: Path) -> dict[str, Any]:
    """Read execution metadata from ``_execution/execution.yaml``.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.

    Returns:
        Dictionary with execution_time, robovast_version, runs,
        execution_type, image, cluster_info, etc.

    Raises:
        FileNotFoundError: If execution.yaml does not exist.
    """
    path = campaign_dir / "_execution" / "execution.yaml"
    if not path.exists():
        raise FileNotFoundError(f"execution.yaml not found in {campaign_dir}")
    with open(path, "r", encoding="utf-8") as f:
        # An empty/blank execution.yaml yields ``None`` from safe_load; callers
        # expect a mapping they can ``.get`` from, so normalize to an empty dict.
        return yaml.safe_load(f) or {}


class RoleImageUnavailable(ValueError):
    """No trustworthy image could be named for one of a campaign's container roles."""


def _is_digest(image: str) -> bool:
    """True for an image reference that names bytes rather than a moving tag.

    Two shapes count: a registry digest ``repo@sha256:…`` (what the cluster records) and a
    bare local image id ``sha256:…`` (what ``docker inspect --format={{.Id}}`` prints).
    """
    if not image:
        return False
    if "@sha256:" in image:
        return True
    return image.startswith("sha256:") and len(image) >= 20


def campaign_asset_groups(campaign_dir) -> tuple:
    """The entry-point groups this campaign's simulator resolves asset providers through.

    Read from the frozen ``.vast`` and its backend, the same way
    :func:`campaign_container_plan` reads the container plan -- so a campaign carries the
    question its providers must be filtered by, and core still names no simulator.

    ``()`` when the backend cannot be resolved here, which a caller must treat as "cannot
    filter" rather than "no groups": recording an unfiltered record, or an empty one, would
    both claim something nobody checked.
    """
    from robovast.common.results_utils import \
        campaign_vast  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import (  # pylint: disable=import-outside-toplevel
        backend_name, resolve_backend)

    try:
        vast_path = campaign_vast(campaign_dir)
        with open(vast_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return ()
    execution = (raw or {}).get("execution") or {}
    if not isinstance(execution, dict):
        return ()
    try:
        name = backend_name(execution)
        if not name:
            return ()
        backend = resolve_backend(name, str(Path(vast_path).parent))
        return tuple(getattr(backend, "ASSET_ENTRY_POINT_GROUPS", ()) or ())
    except Exception:  # noqa: BLE001 - an unresolvable backend is "cannot filter"
        return ()


def campaign_container_plan(campaign_dir: Path):
    """The container plan of a campaign's frozen ``.vast``, or ``None`` if unreadable.

    The snapshot is a verbatim copy of what the author wrote, so a project that left the
    simulator's image to its backend has no image in it -- hence ``apply_backend`` first,
    the same order ``image_build.extract_build_specs`` uses. That needs the backend package
    importable *here*, in the service; when it is not, the declared image simply stays
    unresolved and the caller reports that rather than inventing a default.

    This is the only correct answer to "which containers did this campaign run": the raw
    ``execution.containers`` keys are not, because a ``simulation`` block with neither image
    nor command is folded into the scenario container by ``plan_containers`` -- so a campaign
    with two keys can have run one container, and expecting an artifact per key then reports
    a container that never existed as having gone missing.
    """
    from robovast.common.containers import \
        plan_containers  # pylint: disable=import-outside-toplevel
    from robovast.common.results_utils import \
        campaign_vast  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import apply_backend  # pylint: disable=import-outside-toplevel

    try:
        vast_path = campaign_vast(campaign_dir)
        with open(vast_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return None
    execution = (raw or {}).get("execution") or {}
    if not isinstance(execution, dict):
        return None
    try:
        execution = apply_backend(dict(execution), str(Path(vast_path).parent))
    except Exception:  # noqa: BLE001 - a missing/incompatible backend must not hide the plan
        pass
    try:
        return plan_containers(execution)
    except Exception:  # noqa: BLE001 - a malformed snapshot is "no plan", not a crash
        return None


def campaign_role_image(campaign_dir, role: str, *, resolve_digest=None) -> str:
    """The image that holds a campaign's *role*, as a reference naming immutable bytes.

    Distinct from ``postprocess_job.campaign_execution_image``, which answers the same
    question for the scenario container and is *right* to fall back to a mutable tag: it
    resolves an image to **run**. This one's answer keys a cache, so a tag would let the
    same key serve artifacts built from bytes that no longer exist -- and it is role-aware,
    because "the campaign's image" is not a single fact: the simulator, the system under
    test and the scenario each have their own container.

    Sources, in order of how well they describe what actually happened:

    1. ``image_revisions`` for the role, or for the container backing it -- recorded at run
       time. The contract; the only path a campaign recorded by a current RoboVAST takes.
    2. The campaign's own ``image_revision``, when *role* has **no container of its own** --
       a stepped simulator IS the scenario container, and a campaign that declares no
       containers at all has exactly one, so there the campaign-level image is this role's.
    3. What the campaign *declared* -- ``images``, else the frozen ``.vast`` -- which is a
       tag, so *resolve_digest* has to turn it into bytes. This is what rescues campaigns
       recorded before (1) existed on their lane.

    **The campaign-level image is never substituted for a role that owns a container.** That
    substitution is the bug this function exists to prevent. It is refused only on *positive*
    knowledge, though: when the snapshot cannot be read there is no evidence of a separate
    container, and (2) still applies rather than withholding geometry from a shape we simply
    could not inspect.

    Args:
        campaign_dir: the ``campaign-<id>`` directory.
        role: a container role, e.g. ``simulation``.
        resolve_digest: ``ref -> digest | None``, supplied by the lane that can answer it
            (locally ``docker inspect``). Omitted when the lane cannot, which turns source
            (3) into an explicit refusal instead of a wrong answer.

    Raises:
        RoleImageUnavailable: when no source names bytes. The message lists every source
            tried and what was in it, because it is shown to whoever opened the view.
    """
    meta = read_execution_metadata(Path(campaign_dir))
    revisions = meta.get("image_revisions") or {}
    declared_images = meta.get("images") or {}
    tried = []

    plan = campaign_container_plan(Path(campaign_dir))
    backing = None
    if plan is not None:
        try:
            backing = plan.by_name(role)
        except KeyError:
            tried.append(f"the campaign's .vast declares no {role!r} container or role")
    # The container whose image is wanted: the role's own, or the one it folds onto.
    target = backing.name if backing is not None else None
    # `images` is keyed by declared role, `image_revisions` by container name on the cluster
    # lane and by role on the local one -- so look under both rather than assuming they agree.
    keys = [role] if target in (None, role) else [role, target]

    # 1. what ran, per role.
    for key in keys:
        recorded = revisions.get(key)
        if recorded and _is_digest(str(recorded)):
            return str(recorded)
        if recorded:
            tried.append(f"execution.yaml image_revisions[{key!r}]={recorded!r}, "
                         f"which does not name a digest")
    if not any(revisions.get(k) for k in keys):
        tried.append(f"execution.yaml records no image_revisions for {' or '.join(map(repr, keys))}")

    # 2. the campaign's single image -- only when this role has no container of its own.
    #    Checked before the declared tag below because a recorded digest describes what ran,
    #    where a tag only describes what was asked for.
    own_container = backing is not None and backing.name == role
    if not own_container:
        revision = meta.get("image_revision")
        if revision and _is_digest(str(revision)):
            return str(revision)
        tried.append(f"the campaign's image_revision is {revision!r}")

    # 3. what was declared, resolved to bytes.
    resolved_from = None
    for key in keys:
        if declared_images.get(key):
            resolved_from = (declared_images[key], f"execution.yaml images[{key!r}]")
            break
    if resolved_from is None and backing is not None and backing.image:
        resolved_from = (backing.image,
                         f"the campaign's .vast (execution.containers.{target}.image)")
    if resolved_from is not None:
        declared, source = resolved_from
        if _is_digest(str(declared)):
            return str(declared)
        resolved = resolve_digest(str(declared)) if resolve_digest else None
        if resolved and _is_digest(str(resolved)):
            return str(resolved)
        tried.append(
            f"{source}={declared!r} is a mutable tag, and " + (
                "it could not be resolved to a digest here"
                if resolve_digest else
                "this lane cannot resolve a tag to a digest"))

    raise RoleImageUnavailable(
        f"cannot tell which image holds this campaign's {role!r} container, so an "
        f"artifact built from it could not be attributed to the bytes that produced it. "
        f"Tried: " + "; ".join(tried) + ". Re-run the campaign to record a per-role "
        f"digest, or build the artifact with an execution.generate entry instead.")


class CampaignImageUnpinnable(ValueError):
    """A campaign's built image cannot be named as something a new run could start."""


def campaign_pinned_images(campaign_dir) -> dict[str, str]:
    """``{container: image}`` a new run of this campaign can start from.

    A third image resolver beside the two above, because it answers a third question and the
    other two give the wrong answer to it:

    * :func:`campaign_role_image` keys a **cache**, so it accepts a bare local ``sha256:<id>``
      as a perfectly good identity. A re-run cannot: compose parses ``sha256:<hex>`` as
      ``name:tag`` and tries to pull ``docker.io/library/sha256``.
    * :func:`~robovast.execution.cluster_execution.postprocess_job.campaign_execution_image`
      resolves an image to **run**, but only the scenario container's, and it is right to fall
      back to a mutable tag — postprocessing wants *a* working image, where a re-run wants the
      bytes the campaign was built from.

    The two lanes record different things under the same keys, so this discriminates on
    ``execution_type`` rather than trying one order everywhere:

    * ``image_revisions`` is per-container and pullable **on the cluster** (pod container
      statuses via ``pullable_digest``) but a bare local id **locally** (``docker inspect
      .Id``). So a digest is taken only when it names bytes something can pull.
    * ``images`` is the plan-resolved built ref **locally** (the plan was built *with*
      ``built_images``), but on the cluster it is whatever the ``.vast`` *declared* — the base
      image, or for a ``build:`` project the symbolic ``build:<tag>`` ref itself. Pinning that
      would run the base image without the campaign's own code, so it is local-only.
    * the cluster's per-container keys are **pod** container names, where the main one is
      ``robovast`` rather than ``scenario`` — hence the remap.

    **Which containers to pin comes from the record, not from the ``.vast``.** ``images`` is
    written from the execution mapping *after* ``apply_backend``, so its keys are the containers
    that actually ran — the fold already applied. Re-deriving that from the declaration would
    need the campaign's ``plugins:`` installed only to learn that a stepped simulator's
    ``simulation`` block is really the scenario container, and getting it wrong either refuses a
    campaign that recorded everything (harmless but wrong) or pins a separate container to the
    scenario's image (runs the wrong bytes). The campaign already computed the answer; this reads
    it. Infrastructure sidecars are excluded for free: ``s3-init`` appears in
    ``image_revisions`` but never in ``images``.

    Args:
        campaign_dir: the campaign directory (its ``_execution/execution.yaml`` is read).

    Returns:
        A pin per recorded container. Empty when the campaign recorded no containers at all,
        which the caller has to interpret: harmless if nothing was built, fatal if something was
        (see ``retrigger.prepare``), because only the caller can read the ``.vast``.

    Raises:
        CampaignImageUnpinnable: a recorded container has no ref a new run could start. The
            message names it and every source tried, because a campaign's build context is not
            archived in its results, so this is unrecoverable rather than a retry.
    """
    # Inline, like every other import in this module: it stays dependency-light so it loads
    # cleanly in the pod, the driver and the service alike.
    from robovast.common.config import SCENARIO_CONTAINER  # pylint: disable=import-outside-toplevel

    try:
        meta = read_execution_metadata(Path(campaign_dir))
    except FileNotFoundError:
        # No execution.yaml at all -- a campaign that failed before its first batch, which on
        # the cluster lane is the *usual* shape of a failed one. "Recorded nothing" is one of
        # the answers this function is for, so it belongs in the refusal below with every
        # source named, not as a bare FileNotFoundError from a reader.
        meta = {}
    is_local = meta.get("execution_type") == "local"
    revisions = meta.get("image_revisions") or {}
    declared = meta.get("images") or {}
    pins, unpinnable = {}, []
    # The containers that ran, post-fold, as the campaign itself recorded them.
    container_names = sorted(k for k, v in declared.items() if v)

    for name in container_names:
        # The cluster records the scenario container under its POD name, and a single-container
        # campaign records only the campaign-level revision — so both are the scenario's here.
        # Nothing else falls back to the campaign-level image: handing it to a container that
        # owns its own is the substitution this function exists to prevent, and it would run the
        # wrong bytes rather than fail. A container that merely FOLDS onto the scenario one is
        # the caller's business to name as `scenario` (see retrigger._declared_build_containers).
        keys = [name, "robovast"] if name == SCENARIO_CONTAINER else [name]
        candidates = [revisions.get(k) for k in keys]
        if name == SCENARIO_CONTAINER:
            candidates.append(meta.get("image_revision"))
        pin = next((str(c) for c in candidates if c and _is_pullable(str(c))), None)
        # Local `images` only: on the cluster this field is what was declared, not what ran.
        if pin is None and is_local and declared.get(name):
            pin = str(declared[name])
        if pin is not None:
            pins[name] = pin
            continue
        # Name every source and what was in it: the reader has to be able to tell "recorded
        # nothing" from "recorded a tag/local id that cannot be started elsewhere".
        tried = ", ".join(f"image_revisions[{k!r}]={revisions.get(k)!r}" for k in keys)
        unpinnable.append(
            f"{name!r} (execution.yaml {tried}, image_revision="
            f"{meta.get('image_revision')!r}, images[{name!r}]={declared.get(name)!r}, "
            f"execution_type={meta.get('execution_type')!r})")

    if unpinnable:
        raise CampaignImageUnpinnable(
            "this campaign never recorded a usable image for " + "; ".join(unpinnable) +
            ". A campaign's build context (wheels, sources) is not archived in its results, "
            "so the image cannot be rebuilt from them either.")
    return pins


def _is_pullable(image: str) -> bool:
    """True for a reference a *new run* can start from — bytes something can obtain.

    Stricter than :func:`_is_digest` on purpose: that one accepts a bare local ``sha256:<id>``
    because it only has to *identify* bytes for a cache key. Starting a container from one
    works on the host that built it and nowhere else, and compose reads it as ``name:tag``.
    """
    return bool(image) and "@sha256:" in image


#: Campaign terminal-outcome record — the final ``Status`` (phase/error/…) serialized
#: beside ``controller.log`` in ``_execution/``. Written on terminal exit so a controller
#: crash that never builds ``data.db`` still leaves a durable, queryable reason.
_OUTCOME_FILENAME = "outcome.json"


#: SQLite keeps a WAL database's committed-but-uncheckpointed pages in ``<db>-wal`` and its
#: shared index in ``<db>-shm``, and removes both when the last connection closes; a
#: rollback-journal database uses ``<db>-journal`` the same way. Their presence therefore
#: means a writer still holds the database open — which is exactly the difference between
#: "the results are built" and "the build has started".
_DATA_DB_SIDECARS = ("-wal", "-shm", "-journal")


def campaign_has_derived_data(campaign_dir) -> bool:
    """Does this campaign hold a **finished** ``_execution/data.db``?

    The single evidence test behind ``Status.postprocessed``, shared by the disk-recovery
    path and the live-snapshot one so that the same bytes cannot be given two answers.

    Existence alone is not that evidence, and reading it as such is a false positive that
    reads as a clean bill of health. ``build_data_db`` unlinks any previous database and then
    connects, so the file appears at 0%: a 9 GB build across 1870 runs reported
    ``postprocessed: true`` for the twenty minutes it was being written, and a *re*-postprocess
    reports it right through the window where the previous results have already been deleted
    and the new ones do not exist yet. The sidecars are what tell those apart, and they are
    deliberate rather than incidental — ``build_data_db`` sets ``journal_mode=WAL`` explicitly
    and closes the connection from a ``finally``.

    Errs towards ``False``: a build killed outright leaves its sidecars behind, so a truncated
    database reads as "no data" rather than as results. That is the recoverable direction —
    ``run_postprocessing`` rebuilds it from the run directories, which are kept — where the
    other one hands a reader half a database and calls it the campaign's results.
    """
    data_db = Path(campaign_dir) / "_execution" / "data.db"
    try:
        if not data_db.is_file():
            return False
        return not any(data_db.with_name(data_db.name + suffix).exists()
                       for suffix in _DATA_DB_SIDECARS)
    except OSError:
        return False      # an unreadable record dir is not evidence of results


def write_execution_outcome(campaign_root: Path, status) -> None:
    """Persist the campaign's terminal outcome to ``_execution/outcome.json``.

    ``status`` is a :class:`robovast.execution.control_server.Status`; it is stored
    verbatim (``model_dump_json``) so the reader gets the same model back. Used by
    both the local worker and the in-pod controller, so a failed campaign leaves the
    record at the **same** campaign-relative path regardless of backend.
    """
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / _OUTCOME_FILENAME).write_text(status.model_dump_json(), encoding="utf-8")


def read_execution_outcome(campaign_dir: Path):
    """Read ``_execution/outcome.json`` back into a ``Status``; ``None`` if absent.

    Returns a :class:`robovast.execution.control_server.Status`.
    """
    from robovast.client.status import Status
    path = Path(campaign_dir) / "_execution" / _OUTCOME_FILENAME
    if not path.exists():
        return None
    return Status.model_validate_json(path.read_text(encoding="utf-8"))


def read_campaign_finished_at(campaign_dir: Path) -> Optional[str]:
    """When the campaign reached its terminal phase, as an ISO-8601 UTC string, or ``None``.

    Read from the same ``outcome.json`` above: its ``phase_since`` is when the recorded phase
    began, and for a terminal record that is the moment the campaign ended. The controller
    writes it after share and postprocessing on every path, failures included, so this is a
    recorded time rather than a derived one -- there is deliberately no fallback to a
    directory mtime, for the reason :func:`~robovast.common.store.read_campaign_created_at`
    gives about guessed start times.

    ``None`` when the record is absent, unreadable, or **not terminal**: a record written
    mid-campaign (the local lane writes one non-terminally when its tail does not own the
    ending -- see ``run_campaign``) describes a campaign that is not over, and reading its
    ``phase_since`` as a finish time would date the campaign to the middle of its own run.
    """
    try:
        status = read_execution_outcome(Path(campaign_dir))
    except Exception:  # noqa: BLE001 - a corrupt record is "unknown", never an error here
        return None
    if status is None:
        return None
    # Lazily imported for the same reason `read_execution_outcome` imports `Status` lazily:
    # `robovast.common` must not depend on `robovast.execution` at module scope.
    from robovast.execution.control_server import is_terminal  # pylint: disable=import-outside-toplevel
    if not is_terminal(status.phase) or not status.phase_since:
        return None
    return datetime.fromtimestamp(status.phase_since, tz=timezone.utc).isoformat()


#: Intervention ledger — what a human did to a campaign *while it ran*. One file for every
#: kind, because "what was done to this run?" is one question and answering it should not mean
#: knowing to ask twice: a kill and a probe are the same sort of fact (a person reached into
#: reproducible compute) and differ only in what followed. Its own file in
#: ``_execution/`` because the *service* writes it (that is where a stop request lands) while
#: the run's own records are written by the run, and because it must survive: the run status
#: is re-derived from disk by ``campaign_index`` long after the process that did the killing
#: is gone, so an in-memory set would silently lose the distinction.
#:
#: Not a column on the ``job`` table, which is keyed by the same job dir and looks like the
#: natural home: the killer (the service) and the writer of ``campaign.db`` (the controller)
#: are different writers, and sharing one SQLite file between them is a race, not a design.
_INTERVENTIONS_FILENAME = "interventions.json"

#: The kinds recorded. ``killed`` ends a run; ``probed`` only observed one; ``invalid`` throws
#: one away. All three mean the run is no longer untouched, which is why they share a file
#: rather than a status.
#:
#: ``invalid`` is the one whose actor is not a person, which is what the one-file-with-a-kind
#: shape is for: a non-human actor needs no new file, no new reader and no new doc section.
#: ``source`` names who acted -- ``"webui"`` / ``"mcp"`` / ``"cli"`` for a person, ``"runner"``
#: for the campaign itself.
KIND_KILLED = "killed"
KIND_PROBED = "probed"
KIND_INVALID = "invalid"


def record_intervention(campaign_root: Path, *, kind: str, job_dir: str, job_name: str,
                        source: str, detail: str | None = None,
                        runs: "tuple[str, ...]" = ()) -> None:
    """Append one human intervention to ``_execution/interventions.json``.

    Args:
        kind: :data:`KIND_KILLED` or :data:`KIND_PROBED`. One file rather than one per kind, so a
            reader asking "what was done to this run?" makes one call -- and so a kind added later
            needs no new file, no new reader and no new doc section.
        campaign_root: The campaign's results directory.
        job_dir: The job's artifact dir, campaign-root-relative (``_jobs/batch-0/job-3``).
            The durable identity of what was touched, and what :func:`intervened_runs` resolves
            through the job-link manifest.
        job_name: The job as the caller named it -- ``<config>/<run>`` locally, the Kubernetes Job
            name on the cluster. Recorded for the audit trail; it is lane-specific, so it is not
            what resolution keys on.
        source: Which surface did it -- ``"webui"``, ``"mcp"``, ``"cli"``, or ``"runner"`` when
            the campaign invalidated its own trial. Not a user identity:
            the service is unauthenticated (see ``service/app.py``'s ``serve``), so a name here
            would be invented rather than known.
        detail: The operator's optional explanation, or what was run.
        runs: Run keys (``<config>/<run>``) the caller already knows this job was executing. The
            local lane knows exactly one and passes it; the cluster lane passes none and lets the
            manifest answer. A *hint*, never the only source -- see :func:`intervened_runs`.

    Appends rather than replaces: several things may be done to one campaign over its lifetime,
    and each is a separate event with its own reason.
    """
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    path = exec_dir / _INTERVENTIONS_FILENAME
    entries = read_interventions(campaign_root)
    entries.append({
        "kind": kind,
        "job_dir": job_dir,
        "job_name": job_name,
        "runs": list(runs),
        "source": source,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def read_interventions(campaign_dir: Path, kind: str = "") -> list[dict[str, Any]]:
    """The campaign's intervention ledger, optionally only one *kind*; ``[]`` when there is none.

    An empty list is the overwhelmingly common case -- the file does not exist unless someone
    reached into a campaign -- and every caller is on a path that otherwise behaves exactly as it
    did before this record existed.
    """
    path = Path(campaign_dir) / "_execution" / _INTERVENTIONS_FILENAME
    if not path.is_file():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A truncated ledger must not take the whole result read down with it: the runs are still
        # readable and their verdicts still true, minus the annotation.
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if not kind or e.get("kind") == kind]


def intervened_runs(campaign_dir: Path, kind: str = "") -> dict[str, dict[str, Any]]:
    """Which runs an intervention touched: ``{"<config>/<run>": ledger entry}``.

    Resolves each ledger entry to the runs it covers from **two** sources, unioned:

    * the entry's own ``runs`` hint, which the local lane fills because there ``job_name`` *is*
      the run key; and
    * the job-link manifest, which maps every ``<config>/<run>`` to its job's artifact dir -- the
      only way to answer it for a cluster Job, and the way that also covers a packed job's
      remaining runs without the caller having to enumerate them.

    The manifest, not the ``job`` symlink :func:`read_run_job` follows: that symlink is created
    when a job *finishes*, so it is missing for precisely the jobs this is asked about. The
    manifest is written before the first job starts (see
    :func:`~robovast.common.execution.job_artifact_dir`).

    Returns ``{}`` without touching the manifest when the ledger is empty, which is the default
    for every campaign nobody intervened in.
    """
    entries = read_interventions(campaign_dir, kind)
    if not entries:
        return {}
    from robovast.common.execution import read_job_links
    by_job_dir = {e["job_dir"]: e for e in entries if e.get("job_dir")}
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for run_key in entry.get("runs") or ():
            out[run_key] = entry
    for link, target in read_job_links(campaign_dir).items():
        # ``<config>/<run>/job`` -> ``../../_jobs/<batch>/job-<idx>``; normalizing the target
        # against the link's own directory yields the campaign-relative job dir the ledger records.
        run_key = link[:-len("/job")] if link.endswith("/job") else link
        entry = by_job_dir.get(os.path.normpath(os.path.join(run_key, target)))
        if entry is not None:
            out.setdefault(run_key, entry)
    return out


def killed_runs(campaign_dir: Path) -> dict[str, dict[str, Any]]:
    """Which runs a manual kill cut short. :func:`intervened_runs`, filtered to kills.

    Its own name because the *consequence* differs by kind and its callers act on that: a killed
    run gets a status, a probed one keeps whatever verdict it reached. Sharing the resolution and
    splitting the meaning is the whole point of one ledger.
    """
    return intervened_runs(campaign_dir, KIND_KILLED)


def probed_runs(campaign_dir: Path) -> dict[str, dict[str, Any]]:
    """Which runs were read from while they ran. :func:`intervened_runs`, filtered to probes.

    Orthogonal to a run's outcome, unlike :func:`killed_runs`: a probed run can still pass, and
    folding this into ``status`` would put a human's action into the campaign's measured result --
    the same reason ``killed`` is kept out of ``num_failed``.
    """
    return intervened_runs(campaign_dir, KIND_PROBED)


def invalid_runs(campaign_dir: Path) -> dict[str, dict[str, Any]]:
    """Which runs the runner threw away. :func:`intervened_runs`, filtered to invalidations.

    The consequence differs from both siblings, and in the direction that matters: an invalidated
    run's ``test.xml`` is **overridden**, where a killed run's is kept. See
    :func:`read_run_outcome` for why the inversion is deliberate.
    """
    return intervened_runs(campaign_dir, KIND_INVALID)


def invalid_failure_message(entry: dict[str, Any],
                            superseded: "str | None" = None) -> str:
    """The ``failure_message`` a run the runner threw away carries.

    Names what was discarded as well as why. The trial may have written a verdict, and a
    reader who sees ``invalid`` where they remember a ``passed`` deserves to be told so in
    the same sentence rather than left to go and find the ledger. *superseded* is that
    verdict, supplied by :func:`read_run_outcome`, which is the only caller that can see it.
    """
    reason = (entry.get("detail") or "").strip()
    text = f"trial invalidated by the runner: {reason}" if reason else (
        "trial invalidated by the runner")
    if superseded:
        text += f" (discarded verdict: {superseded})"
    return text


def _verdict_word(result: dict[str, Any]) -> str:
    """``passed`` / ``error`` / ``failed`` for a parsed ``test.xml``. One mapping, twice used."""
    if result.get("success"):
        return "passed"
    return "error" if int(result.get("errors", 0)) else "failed"


#: What a container DIED of, when the kubelet restarted it under a running trial. Its own
#: file beside the intervention ledger, and for the same reasons: the *runner* writes it
#: while the run's own records are written by the run, and it must survive a campaign that
#: never postprocesses -- which is exactly the campaign that needs it.
#:
#: Separate from ``interventions.json`` rather than a fatter entry in it because the two
#: answer different questions and are read by different callers. The ledger answers "was
#: this run touched?" and every status derivation asks it on every campaign; this answers
#: "what happened to the container?" and only a post-mortem asks it. Folding a 400-line log
#: tail into the file that :func:`intervened_runs` reads for every config would make the
#: common path pay for the rare one.
_CONTAINER_FAILURES_FILENAME = "container_failures.json"


def record_container_failures(campaign_root: Path,
                              entries: "list[dict[str, Any]]") -> None:
    """Append container-death records to ``_execution/container_failures.json``.

    Appends rather than replaces: a campaign may lose several containers over its life, and
    each is its own event with its own evidence.
    """
    if not entries:
        return
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    path = exec_dir / _CONTAINER_FAILURES_FILENAME
    existing = read_container_failures(campaign_root)
    existing.extend(entries)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def read_container_failures(campaign_dir: Path) -> list[dict[str, Any]]:
    """The campaign's container-death records; ``[]`` when there are none.

    Degrades to ``[]`` on a corrupt file rather than raising, like the intervention ledger:
    this is evidence *about* a failure, and a reader that dies on it turns one lost trial
    into a lost campaign -- the shape of the very bug it was written to record.
    """
    path = Path(campaign_dir) / "_execution" / _CONTAINER_FAILURES_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def container_failure_message(record: dict[str, Any]) -> str:
    """One sentence naming what died and how, built once so every reader says the same.

    Prefers the signal name over the raw code: ``SIGBUS`` is a diagnosis and ``135`` is a
    number that has to be looked up, and the looking-up is what did not happen last time.
    """
    container = record.get("container") or "a container"
    reason = record.get("reason")
    code = record.get("exit_code")
    signal_name = record.get("signal_name")
    how = signal_name or (f"exit {code}" if code is not None else "an unreported cause")
    node = record.get("node_label")
    text = f"{container} died of {how}"
    if reason:
        text += f" ({reason})"
    if node:
        text += f" on {node}"
    return text


def killed_failure_message(entry: dict[str, Any]) -> str:
    """The ``failure_message`` a manually-stopped run carries.

    One phrasing, built here, so the reason a reader sees is the same in the ``run``
    table, the SQL views and the web UI rather than three near-identical strings.
    """
    reason = (entry.get("detail") or "").strip()
    where = entry.get("source") or "unknown surface"
    return (f"manually stopped via {where}: {reason}" if reason
            else f"manually stopped via {where}")


#: Campaign launch record — the request the campaign was *asked for*, beside the records
#: describing what then happened. Its own file, and not fields on ``execution.yaml``, for a
#: timing reason: ``execution.yaml`` is written **by the run** (locally by the generated run
#: script at the start of the first batch, in-cluster after ``run_batch_in_pod`` returns) and
#: both writers create it from scratch. The launch is known by the *service*, much earlier —
#: so sharing one file would either have the run truncate what the service wrote, or leave a
#: campaign that failed before its first batch with no launch record at all. That campaign is
#: exactly the one someone wants to relaunch. Same argument, mirrored, is why the terminal
#: outcome is its own file: it can only exist *after*.
#:
#: Readers wanting one document get it from ``metadata.yaml``, which nests this under
#: ``execution.launch``.
_BUILD_MANIFEST_DIRNAME = "build_manifest"


def write_build_manifests(campaign_root, manifests: dict) -> None:
    """Copy each image's build lock out of the image and into ``_execution/build_manifest/``.

    The lock is baked into the image, which is the right place for it -- it travels if the image
    is copied and it is there for anyone holding it. But it is *only* there, so the moment the
    image is deleted the lock goes with it, and "rebuild from the lock" becomes impossible exactly
    when it is needed. A campaign's own record is what survives, so the lock is copied here too.

    One file per role, holding what the image reported: the resolved apt versions, ``pip freeze``,
    and which commit each floating git ref became.

    Raises rather than swallowing, like the other writers here; the caller decides this is
    best-effort.
    """
    if not manifests:
        return
    target = Path(campaign_root) / "_execution" / _BUILD_MANIFEST_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    for role, manifest in sorted(manifests.items()):
        if not manifest:
            continue
        with open(target / f"{role}.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)


def read_build_manifests(campaign_dir) -> dict:
    """``{role: {apt: {...}, pip: {...}, vcs: {...}}}`` from ``_execution/build_manifest/``.

    Empty means unknown -- either the campaign predates this record or its images carried no lock
    -- and a caller must not read that as "installed nothing", which would make a rebuild install
    an empty set rather than the author's intent.
    """
    source = Path(campaign_dir) / "_execution" / _BUILD_MANIFEST_DIRNAME
    if not source.is_dir():
        return {}
    out = {}
    for path in sorted(source.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                out[path.stem] = json.load(f)
        except (OSError, ValueError):
            # A record that cannot be parsed is not a record. Skipped rather than raised: this is
            # read on paths that must not fail over provenance.
            continue
    return out


_PLUGINS_FILENAME = "plugins.yaml"


def write_plugins_record(campaign_root, resolved: dict) -> None:
    """Persist what the campaign's ``plugins:`` specs resolved to, to ``_execution/plugins.yaml``.

    Its own record rather than a key in ``execution.yaml`` because it is known at a different
    time and by different code: plugins resolve while the campaign is being *composed*, where
    the ``.vast`` directory and its ``.robovast_plugins/`` install dir are in hand, whereas
    execution.yaml is written later -- by a generated shell script on the local lane -- from a
    place that has neither. Threading the directory through two lanes to reach that file would
    couple them to composition for one field. ``_execution/`` already holds several records
    for exactly this reason (launch, outcome, killed).

    Raises rather than swallowing, like the other writers in this module -- the *caller*
    decides that a record is best-effort and says so, exactly as ``local_transport`` does for
    launch.yaml. Keeping that decision here would hide a real failure from the one place that
    knows whether it matters.
    """
    # Written even when empty. An empty record is a positive statement -- "this campaign was asked
    # and declared none" -- and it is the ONLY thing that distinguishes that from a campaign
    # recorded before this file existed, whose plugins are genuinely unknown. Skipping the write
    # made the two identical, and since the publication gate treats unknown as opaque, every
    # campaign without plugins became unpublishable with no way for its author to fix it.
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    with open(exec_dir / _PLUGINS_FILENAME, "w", encoding="utf-8") as f:
        yaml.dump(resolved, f, default_flow_style=False, sort_keys=True)


_PROVIDERS_FILENAME = "providers.yaml"


def write_providers_record(campaign_root, providers: dict) -> None:
    """Persist which asset providers supplied this campaign, to ``_execution/providers.yaml``.

    Separate from plugins.yaml because they answer different questions and one can be known
    without the other: ``plugins:`` is what the *campaign author declared*, while these are
    distributions the *simulator backend* resolves by entry point -- a campaign that declares
    no plugins at all still depends on whichever asset packages supplied its world.

    Raises rather than swallowing; the caller decides this is best-effort.
    """
    # Written even when empty, for the reason write_plugins_record gives: an empty record is what
    # separates "asked, none" from a campaign predating the file, whose providers are unknown.
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    with open(exec_dir / _PROVIDERS_FILENAME, "w", encoding="utf-8") as f:
        yaml.dump(providers, f, default_flow_style=False, sort_keys=True)


def read_providers_record(campaign_dir) -> "dict | None":
    """Read ``_execution/providers.yaml``; ``None`` when the campaign has none.

    ``None`` means the file is **absent**, so *unknown* -- not "no providers". An empty file
    returns ``{}``: asked, none. See :func:`read_plugins_record` for why the two must not collapse.
    """
    path = Path(campaign_dir) / "_execution" / _PROVIDERS_FILENAME
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return {} if loaded is None else loaded


def read_plugins_record(campaign_dir) -> "dict | None":
    """Read ``_execution/plugins.yaml``; ``None`` when the campaign has none.

    ``None`` means the file is **absent** -- and that is not an error and does not mean "no
    plugins": campaigns recorded before this file existed declared plugins without resolving them
    anywhere, so a caller has to treat it as *unknown*. Conflating it with empty would report a
    re-run as safely pinned when nothing about its plugins was ever captured.

    An **empty** file is the opposite answer and returns ``{}``: the campaign was asked and
    declared none. ``or None`` here collapsed the two, which is what made every campaign without
    plugins read as unknown.
    """
    path = Path(campaign_dir) / "_execution" / _PLUGINS_FILENAME
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return {} if loaded is None else loaded


_LAUNCH_FILENAME = "launch.yaml"

#: Request fields a **retrigger replays**. ``workspace_id``/``config_path`` are deliberately
#: absent, and stay absent: a retrigger runs from the campaign's own frozen ``_config/``, so
#: replaying a workspace binding would point it at a tree that may have moved on or be gone
#: (see ``service/retrigger.py``).
#:
#: That is not the same as the fact being unrecorded. Where the configuration *came from* is
#: kept on the campaign row as ``origin_*`` (see ``common/store.py``) -- a record of the past,
#: which nothing reads back to run anything. This file is the replay; that is the provenance.
_LAUNCH_FIELDS = ("config_filter", "campaign_name", "runs", "postprocess",
                  "upload_to_share", "show_gui")


def write_launch_record(campaign_root: Path, request) -> None:
    """Persist how the campaign was **asked for** to ``_execution/launch.yaml``.

    ``request`` is a :class:`robovast.service.interface.CreateCampaignRequest`. ``runs`` is
    stored **as requested**, so ``0`` keeps meaning "take the ``.vast``'s ``execution.runs``"
    — the pair with ``execution.yaml``'s *effective* count is what makes an override legible
    ("3 because the ``.vast`` says 3" vs "3 because someone overrode a ``.vast`` saying 25").
    Neither number answers that alone.

    Best-effort by the same reasoning as :func:`write_execution_outcome`'s caller: a campaign
    must not fail because a record could not be written.
    """
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    record = {field: getattr(request, field) for field in _LAUNCH_FIELDS}
    with open(exec_dir / _LAUNCH_FILENAME, "w", encoding="utf-8") as f:
        yaml.dump(record, f, default_flow_style=False, sort_keys=False)


def read_launch_record(campaign_dir: Path) -> dict[str, Any] | None:
    """Read ``_execution/launch.yaml``; ``None`` when the campaign has none.

    ``None`` is not an error: campaigns recorded before this file existed have no launch
    record, and a caller has to decide what to do about each field it wanted (the retrigger
    falls back to ``execution.yaml``'s effective ``runs``, and reports the filter as unknown
    rather than silently running a full sweep in place of a pilot).
    """
    path = Path(campaign_dir) / "_execution" / _LAUNCH_FILENAME
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        # A blank file parses to None; callers expect a mapping or None-meaning-absent, and
        # an empty record is indistinguishable from absent for every field they read.
        return yaml.safe_load(f) or None


def read_scenario_config(config_dir: Path) -> dict[str, Any]:
    """Read scenario configuration from ``_config/scenario.config``.

    Unwraps the single-key wrapper (scenario name) that wraps the
    actual parameter values.

    Args:
        config_dir: Path to the configuration directory
            (e.g. ``campaign-<id>/<config-name>``).

    Returns:
        Dictionary of resolved parameter key-value pairs.

    Raises:
        FileNotFoundError: If scenario.config does not exist.
    """
    path = config_dir / "_config" / "scenario.config"
    if not path.exists():
        raise FileNotFoundError(f"scenario.config not found in {config_dir}")
    with open(path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)

    # Unwrap single-key wrapper (e.g. {test_scenario: {param: val}} → {param: val})
    if isinstance(content, dict) and len(content) == 1:
        content = next(iter(content.values()))

    return content


def read_test_result(run_dir: Path) -> dict[str, Any]:
    """Parse JUnit test result from ``test.xml``.

    Args:
        run_dir: Path to the run directory (e.g. ``campaign-<id>/<config>/0``).

    Returns:
        Dictionary with keys: passed (bool), duration_sec (float),
        start_time (ISO string), errors (int), failures (int), tests (int).

    Raises:
        FileNotFoundError: If test.xml does not exist.
    """
    path = run_dir / "test.xml"
    if not path.exists():
        raise FileNotFoundError(f"test.xml not found in {run_dir}")

    tree = ET.parse(path)
    root = tree.getroot()

    errors = int(root.get("errors", "0"))
    failures = int(root.get("failures", "0"))
    tests = int(root.get("tests", "0"))

    testcase = root.find("testcase")
    duration = float(testcase.get("time", "0")) if testcase is not None else 0.0

    # Extract start_time from properties. Kept in both forms: the ISO string every reader
    # already uses, and the raw epoch seconds, because the wall window (start .. start +
    # duration) is how a job's container log is attributed to the run that produced it, and
    # re-parsing the ISO string to get back a number it was made from is a needless round
    # trip that also loses nothing gracefully when the format changes.
    start_time_iso = None
    start_epoch = None
    if testcase is not None:
        properties = testcase.find("properties")
        if properties is not None:
            for prop in properties.findall("property"):
                if prop.get("name") == "start_time":
                    ts = float(prop.get("value", "0"))
                    start_epoch = ts
                    start_time_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    break

    # Extract failure message if present
    failure_message = None
    if testcase is not None:
        failure_elem = testcase.find("failure")
        if failure_elem is not None:
            failure_message = failure_elem.get("message") or failure_elem.text

    return {
        "success": errors == 0 and failures == 0,
        "duration_sec": duration,
        "start_time": start_time_iso,
        "start_epoch": start_epoch,
        "errors": errors,
        "failures": failures,
        "tests": tests,
        "failure_message": failure_message,
    }


def read_run_job(run_dir: Path, campaign_root: Path) -> tuple[str, dict[str, Any] | None]:
    """The execution job a run belonged to: ``(job_dir, sysinfo)``.

    ``job_dir`` is the job's directory relative to *campaign_root* (e.g.
    ``_jobs/batch-0/job-3``), resolved through the run dir's ``job`` symlink. It is the
    identity of the *host record*, not of the run: a packed multi-config job executes
    several (config, run) pairs, and every one of them resolves to the same job dir. That
    sharing is the point — it is what makes "did these runs land on one machine?"
    answerable — so the job is recorded once and runs point at it.

    Without a ``job`` symlink — an older layout that wrote ``sysinfo.yaml`` into the run
    dir or its ``logs/``, or a run whose job dir was pruned — the run *is* its own unit of
    host information, so ``job_dir`` is the run's own directory. That keeps the host record
    reachable (dropping it would lose data :func:`read_sysinfo` can still find) while
    saying something true: one host record, no shared job known.

    ``(job_dir, None)`` when no ``sysinfo.yaml`` exists in any of the accepted locations,
    which is not an error — *which* job a run belonged to is worth recording regardless.
    """
    campaign_root = Path(campaign_root).resolve()

    def _relative(path: Path) -> str:
        """*path* relative to the campaign root, or "" if it escapes it."""
        try:
            rel = os.path.relpath(path, campaign_root)
        except (OSError, ValueError):
            return ""
        # Outside the campaign: not this campaign's job, so do not record it.
        return "" if rel.startswith("..") else rel

    job_dir = ""
    try:
        job_path = (run_dir / "job").resolve()
        if job_path.is_dir():
            job_dir = _relative(job_path)
    except (OSError, ValueError):
        job_dir = ""
    if not job_dir:
        job_dir = _relative(run_dir.resolve())
    try:
        sysinfo = read_sysinfo(run_dir)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        sysinfo = None
    return job_dir, sysinfo


def read_run_outcome(run_dir: Path,
                     campaign_root: Path | None = None,
                     killed: "dict[str, dict[str, Any]] | None" = None,
                     invalid: "dict[str, dict[str, Any]] | None" = None) -> dict[str, Any]:
    """Per-run outcome for the ``run`` table, derived from ``test.xml``.

    The single place the JUnit result is mapped to a normalized ``status`` —
    ``passed`` (no errors, no failures), ``error`` (errors present), ``failed``
    (failures only), ``killed`` (an operator stopped its job by hand) or ``unknown``
    (``test.xml`` missing/unparseable). The controller records these live,
    :mod:`campaign_index` backfills them from disk, and postprocessing reads them back,
    so the mapping must live once here.

    ``killed`` replaces ``unknown`` and **only** ``unknown``. A run whose job was killed
    but which wrote a valid ``test.xml`` finished *before* the kill landed — its verdict
    is real measurement, and overwriting it would destroy data that a packed job
    (``runs_per_job > 1``) routinely produces. So a manual kill can only ever annotate a
    run that delivered nothing, which is what makes this whole distinction additive:
    no run that ever produced a verdict changes status.

    ``invalid`` is the exact inverse, and it is the ONLY status that overrides a written
    verdict. The inversion is the whole reason it is a separate kind rather than another
    ``killed``: a killed run's ``test.xml`` was written *before* the intervention, so it is
    measurement; an invalidated run's was written *by* the trial the intervention says was
    broken. A container the trial depended on crashed under it and came back with no memory
    of the run, and the scenario carried on — so the file records what a simulator that had
    lost its state produced. That is at its most dangerous when it says ``passed``, because
    nothing else about the run looks wrong. Trusting it is the data loss; discarding it is
    not: the ``test.xml`` stays on disk, and the verdict being overridden is named in the
    run's ``failure_message`` ("discarded verdict: passed"), so the override is auditable
    and reversible rather than merely silent.

    Args:
        run_dir: The run's directory (``<campaign>/<config>/<run>``).
        campaign_root: The campaign root, which adds ``job_dir`` / ``sysinfo`` from
            :func:`read_run_job` — :meth:`~robovast.common.store.CampaignStore.record_runs`
            turns those into the ``job`` row and the run's ``job_id``.
        killed: A :func:`killed_runs` mapping, when the caller already built one.
            Passed in rather than read here so a whole config's runs share one read of
            the ledger; ``None`` means "look it up", and an empty mapping means "nothing
            was killed" — the default.
        invalid: An :func:`invalid_runs` mapping, on the same terms.

    Returns a dict keyed exactly like the ``run`` columns: ``run_id``, ``status``,
    ``passed`` (0/1), ``errors``, ``failures``, ``tests``, ``duration_s``,
    ``start_time``, ``failure_message``.
    """
    run_id = int(run_dir.name) if run_dir.name.isdigit() else -1
    run_key = f"{run_dir.parent.name}/{run_dir.name}"
    job: dict[str, Any] = {}
    if campaign_root is not None:
        job_dir, sysinfo = read_run_job(run_dir, campaign_root)
        job = {"job_dir": job_dir, "sysinfo": sysinfo}
    # Checked BEFORE the verdict is read, not in the handler for a missing one: this is the
    # status that overrides what the trial wrote, so reading it first is the point.
    if invalid is None and campaign_root is not None:
        invalid = invalid_runs(campaign_root)
    invalid_entry = (invalid or {}).get(run_key)
    if invalid_entry is not None:
        # Read the verdict being discarded, and name it in the message. The runner could
        # not: at the moment it invalidates a job the trial may not have written one yet,
        # and it has no access to the run directory. Here it is known, and saying "the
        # discarded verdict was passed" out loud is what keeps the override auditable
        # instead of merely silent.
        try:
            superseded = _verdict_word(read_test_result(run_dir))
        except (OSError, ET.ParseError, ValueError):
            superseded = None
        return {"run_id": run_id, "status": "invalid", "passed": 0,
                "errors": 0, "failures": 0, "tests": 0,
                "duration_s": None, "start_time": None,
                "failure_message": invalid_failure_message(invalid_entry, superseded),
                **job}
    try:
        tr = read_test_result(run_dir)
    except (OSError, ET.ParseError, ValueError):
        # A run that never wrote a ``test.xml``, or wrote a truncated/corrupt one
        # (crashed mid-run), still gets a row so it is counted — marked ``unknown``
        # rather than silently dropped. ``OSError`` covers the missing-file case
        # (``FileNotFoundError``); ``ET.ParseError``/``ValueError`` the malformed one.
        if killed is None and campaign_root is not None:
            killed = killed_runs(campaign_root)
        entry = (killed or {}).get(run_key)
        if entry is not None:
            # Deliberately stopped, not lost: ``unknown`` would file this with the runs
            # whose result went missing for reasons nobody chose.
            return {"run_id": run_id, "status": "killed", "passed": 0,
                    "errors": 0, "failures": 0, "tests": 0,
                    "duration_s": None, "start_time": None,
                    "failure_message": killed_failure_message(entry), **job}
        return {"run_id": run_id, "status": "unknown", "passed": 0,
                "errors": 0, "failures": 0, "tests": 0,
                "duration_s": None, "start_time": None, "failure_message": None,
                **job}
    passed = 1 if tr.get("success") else 0
    errors = int(tr.get("errors", 0))
    failures = int(tr.get("failures", 0))
    status = _verdict_word(tr)
    return {
        "run_id": run_id, "status": status, "passed": passed,
        "errors": errors, "failures": failures, "tests": int(tr.get("tests", 0)),
        "duration_s": tr.get("duration_sec"), "start_time": tr.get("start_time"),
        "failure_message": tr.get("failure_message"),
        **job,
    }


def read_run_outcomes(config_dir: Path,
                      campaign_root: Path | None = None) -> list[dict[str, Any]]:
    """:func:`read_run_outcome` for every numeric run dir under *config_dir*.

    The intervention ledger is resolved **once** here and shared across the config's runs,
    rather than per run: with no ledger that is a single ``is_file`` miss for the whole
    config, and the outcomes are then identical to what this returned before the ledger
    existed.

    Read unfiltered and split by kind locally rather than calling :func:`killed_runs` and
    :func:`invalid_runs` in turn — two calls would be two file reads and two manifest reads,
    which is exactly the per-config cost this promises not to pay.
    """
    killed: dict[str, dict[str, Any]] = {}
    invalid: dict[str, dict[str, Any]] = {}
    if campaign_root is not None:
        for run_key, entry in intervened_runs(campaign_root).items():
            if entry.get("kind") == KIND_KILLED:
                killed[run_key] = entry
            elif entry.get("kind") == KIND_INVALID:
                invalid[run_key] = entry
    return [read_run_outcome(rd, campaign_root, killed, invalid)
            for rd in list_run_dirs(config_dir)]


def read_sysinfo(run_dir: Path) -> dict[str, Any]:
    """Read system information from ``sysinfo.yaml``.

    ``collect_sysinfo.py`` writes it into the **job** directory, which each run
    dir exposes as its ``job`` symlink (``_jobs/batch-<n>/job-<m>/sysinfo.yaml``) —
    on both backends. Older/other layouts kept it in the run dir or its ``logs/``,
    so all three locations are accepted.

    Args:
        run_dir: Path to the run directory.

    Returns:
        Dictionary with platform, CPU, memory, etc.

    Raises:
        FileNotFoundError: If sysinfo.yaml does not exist.
    """
    candidates = [run_dir / "job" / "sysinfo.yaml",
                  run_dir / "sysinfo.yaml",
                  run_dir / "logs" / "sysinfo.yaml"]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        raise FileNotFoundError(f"sysinfo.yaml not found in {run_dir}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_resolved_configurations(campaign_dir: Path) -> dict[str, Any]:
    """Read fully resolved configurations from ``_transient/configurations.yaml``.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.

    Returns:
        Dictionary with configs list, execution info, run_files, etc.

    Raises:
        FileNotFoundError: If configurations.yaml does not exist.
    """
    path = campaign_dir / "_transient" / "configurations.yaml"
    if not path.exists():
        raise FileNotFoundError(f"configurations.yaml not found in {campaign_dir}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_vast_configuration_info(
    campaign_dir: Path,
    config_dirs: list[Path] | None = None,
    list_runs_fn=None,
) -> dict[str, Any]:
    """Gather important statistics about a VAST campaign configuration.

    This function collects key metrics from a campaign, including the number
    of jobs/configurations, runs, test results, and execution details.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.
        config_dirs: Optional list of configuration directory paths. If not
            provided, they will be discovered by excluding reserved directories.
        list_runs_fn: Optional callback function that takes a config_dir Path
            and returns a list of run directory Paths. If not provided, run
            directories are discovered by looking for numeric subdirectories.

    Returns:
        Dictionary containing:
        - campaign_name: str - Name of the campaign directory
        - num_configs: int - Number of job configurations
        - num_runs: int - Total number of runs across all configs
        - num_passed: int - Number of passed tests
        - num_failed: int - Number of failed tests
        - num_errors: int - Number of errors
        - num_invalid: int - Runs the runner discarded (a container restarted under them)
        - total_duration_sec: float - Total execution time in seconds
        - execution_info: dict - Execution metadata (version, type, image, etc.)
        - configs: list[dict] - Per-configuration statistics

    Raises:
        FileNotFoundError: If required campaign files are missing.
    """
    # Get execution metadata
    exec_meta = read_execution_metadata(campaign_dir)
    # This is the ``test.xml`` walk used when ``campaign.db`` is absent, so it must consult
    # the ledger for itself: an invalidated run's ``test.xml`` may say ``passed``, and a
    # recovery path that believes it would report the very verdict the runner discarded.
    invalidated = set(invalid_runs(campaign_dir))

    # Discover config directories if not provided
    if config_dirs is None:
        reserved = {"_config", "_execution", "_transient"}
        config_dirs = [
            d for d in campaign_dir.iterdir()
            if d.is_dir() and d.name not in reserved and not d.name.startswith(".")
        ]
        config_dirs = sorted(config_dirs)

    # Default run directory discovery
    def default_list_runs(cfg_dir: Path) -> list[Path]:
        return sorted(
            [d for d in cfg_dir.iterdir() if d.is_dir() and d.name.isdigit()],
            key=lambda x: int(x.name)
        )

    run_discovery_fn = list_runs_fn or default_list_runs

    # Count configs and gather per-config stats
    configs_info = []
    num_runs = 0
    num_passed = 0
    num_failed = 0
    num_errors = 0
    num_invalid = 0
    total_duration = 0.0

    for config_dir in config_dirs:
        config_name = config_dir.name
        run_dirs = run_discovery_fn(config_dir)

        config_runs = len(run_dirs)
        config_passed = 0
        config_failed = 0
        config_errors = 0
        config_invalid = 0
        config_duration = 0.0

        for run_dir in run_dirs:
            if f"{config_name}/{run_dir.name}" in invalidated:
                config_invalid += 1
                continue
            try:
                result = read_test_result(run_dir)
                if result["success"]:
                    config_passed += 1
                else:
                    if result["errors"] > 0:
                        config_errors += 1
                    if result["failures"] > 0:
                        config_failed += 1
                config_duration += result.get("duration_sec", 0.0)
            except FileNotFoundError:
                # Run may not have completed
                pass

        configs_info.append({
            "name": config_name,
            "num_runs": config_runs,
            "passed": config_passed,
            "failed": config_failed,
            "errors": config_errors,
            **({"invalid": config_invalid} if config_invalid else {}),
            "duration_sec": config_duration,
        })

        num_runs += config_runs
        num_passed += config_passed
        num_failed += config_failed
        num_errors += config_errors
        num_invalid += config_invalid
        total_duration += config_duration

    return {
        "campaign_name": campaign_dir.name,
        "num_configs": len(config_dirs),
        "num_runs": num_runs,
        "num_passed": num_passed,
        "num_failed": num_failed,
        "num_errors": num_errors,
        "num_invalid": num_invalid,
        "total_duration_sec": total_duration,
        "execution_info": {
            "execution_time": exec_meta.get("execution_time"),
            "robovast_version": exec_meta.get("robovast_version"),
            "execution_type": exec_meta.get("execution_type"),
            "image": exec_meta.get("image"),
            "cluster_info": exec_meta.get("cluster_info"),
        },
        "configs": configs_info,
    }


# Campaign-level directories that are not configuration directories.
#: Directories under a campaign that are NOT configurations, and so hold no runs.
#:
#: ``_calibration`` is where a per-node calibration probe writes. That is the whole mechanism
#: by which a probe is not a campaign run: it is never *added* rather than added and then
#: removed. Deleting real run data to correct an allocation would be a far more dangerous
#: design -- a bug in it costs results that cannot be recovered -- and this needs no deletion
#: at all, because nothing walks a reserved name looking for runs.
#: Where a node-calibration probe writes. Named here rather than beside the calibration
#: code because both lanes' postprocessing must keep it out of the bag scan, and neither may
#: import the cluster package. A probe is deliberately not a run, so its bag is not campaign
#: data -- and an interrupted probe's unfinalized bag fails the whole conversion step.
PROBE_DIR = "_calibration"

RESERVED_CAMPAIGN_DIRS = {"_config", "_execution", "_transient", "_jobs", "_control",
                          PROBE_DIR}


def list_config_dirs(campaign_dir: Path) -> list[Path]:
    """Configuration directories directly under a campaign dir (sorted)."""
    return sorted(
        d for d in Path(campaign_dir).iterdir()
        if d.is_dir() and d.name not in RESERVED_CAMPAIGN_DIRS and not d.name.startswith(".")
    )


def list_run_dirs(config_dir: Path) -> list[Path]:
    """Numeric run directories under a config dir, ascending."""
    try:
        return sorted(
            (d for d in Path(config_dir).iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: int(d.name),
        )
    except (OSError, ValueError):
        return []


def aggregate_run_status(run_dirs: list[Path], *,
                         invalid: "set[str] | None" = None) -> str:
    """Aggregate per-run pass/fail (from each run's ``test.xml``) into one status.

    Returns ``passed`` (all runs passed), ``failed`` (none passed), ``mixed``
    (some of each), ``no_sample`` (runs present but every one invalidated), or
    ``no_runs`` (no runs present). A run missing ``test.xml`` counts against the config.

    *invalid* is a set of ``"<config>/<run>"`` keys (see :func:`invalid_runs`). Those runs
    are skipped in **both** tallies: a trial the runner threw away is neither a pass nor a
    strike against the configuration, and counting it as a failure would put an
    infrastructure fault into the measured result.

    A config whose every run was invalidated returns ``no_sample`` rather than ``failed`` or
    ``no_runs``. That word already exists in ``unit.status`` with exactly this meaning --
    it ran and produced nothing measurable -- so reusing it adds no vocabulary and keeps
    "we measured nothing" apart from "we measured nothing good".
    """
    invalid = invalid or set()
    passed = failed = counted = 0
    for run_dir in run_dirs:
        if f"{run_dir.parent.name}/{run_dir.name}" in invalid:
            continue
        counted += 1
        try:
            result = read_test_result(run_dir)
        except FileNotFoundError:
            failed += 1
            continue
        if result["success"]:
            passed += 1
        else:
            failed += 1
    if not run_dirs:
        return "no_runs"
    if not counted:
        return "no_sample"
    if failed == 0:
        return "passed"
    if passed == 0:
        return "failed"
    return "mixed"
