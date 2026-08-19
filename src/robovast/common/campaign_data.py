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
from typing import Any

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
    because "the campaign's image" stopped being a single fact once the simulator, the
    system under test and the scenario got their own containers.

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


#: Manual-kill ledger — the jobs an operator stopped by hand, so a run that was cut short
#: deliberately can be told apart from one that failed on its own. Its own file in
#: ``_execution/`` because the *service* writes it (that is where a stop request lands) while
#: the run's own records are written by the run, and because it must survive: the run status
#: is re-derived from disk by ``campaign_index`` long after the process that did the killing
#: is gone, so an in-memory set would silently lose the distinction.
#:
#: Not a column on the ``job`` table, which is keyed by the same job dir and looks like the
#: natural home: the killer (the service) and the writer of ``campaign.db`` (the controller)
#: are different writers, and sharing one SQLite file between them is a race, not a design.
_KILLED_FILENAME = "killed_jobs.json"


def record_killed_job(campaign_root: Path, *, job_dir: str, job_name: str,
                      source: str, reason: str | None = None,
                      runs: "tuple[str, ...]" = ()) -> None:
    """Append a manually-stopped job to ``_execution/killed_jobs.json``.

    Args:
        campaign_root: The campaign's results directory.
        job_dir: The job's artifact dir, campaign-root-relative (``_jobs/batch-0/job-3``).
            The durable identity of what was stopped, and what
            :func:`killed_runs` resolves through the job-link manifest.
        job_name: The job as the caller named it — ``<config>/<run>`` locally, the
            Kubernetes Job name on the cluster. Recorded for the audit trail; it is
            lane-specific, so it is not what resolution keys on.
        source: Which surface requested it — ``"webui"``, ``"mcp"`` or ``"cli"``. Not a
            user identity: the service is unauthenticated (see ``service/app.py``'s
            ``serve``), so a name here would be invented rather than known.
        reason: The operator's optional explanation.
        runs: Run keys (``<config>/<run>``) the caller already knows this job was
            executing. The local lane knows exactly one and passes it; the cluster lane
            passes none and lets the manifest answer. It is a *hint*, never the only
            source — see :func:`killed_runs`.

    Appends rather than replaces: several jobs of one campaign may be stopped over its
    lifetime, and each is a separate event with its own reason.
    """
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    path = exec_dir / _KILLED_FILENAME
    entries = read_killed_jobs(campaign_root)
    entries.append({
        "job_dir": job_dir,
        "job_name": job_name,
        "runs": list(runs),
        "source": source,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def read_killed_jobs(campaign_dir: Path) -> list[dict[str, Any]]:
    """The campaign's manual-kill ledger; ``[]`` when nothing was ever killed.

    An empty list is the overwhelmingly common case — the file does not exist unless
    someone stopped a job — and every caller is on a path that otherwise behaves exactly
    as it did before this record existed.
    """
    path = Path(campaign_dir) / "_execution" / _KILLED_FILENAME
    if not path.is_file():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A truncated ledger must not take the whole result read down with it: the runs
        # are still readable and their verdicts still true, minus the kill annotation.
        return []
    return entries if isinstance(entries, list) else []


def killed_runs(campaign_dir: Path) -> dict[str, dict[str, Any]]:
    """Which runs a manual kill cut short: ``{"<config>/<run>": ledger entry}``.

    Resolves each ledger entry to the runs it covers from **two** sources, unioned:

    * the entry's own ``runs`` hint, which the local lane fills because there
      ``job_name`` *is* the run key; and
    * the job-link manifest, which maps every ``<config>/<run>`` to its job's artifact
      dir — the only way to answer it for a cluster Job, and the way that also covers a
      packed job's remaining runs without the killer having to enumerate them.

    The manifest, not the ``job`` symlink :func:`read_run_job` follows: that symlink is
    created when a job *finishes*, so it is missing for precisely the jobs this function
    is asked about. The manifest is written before the first job starts (see
    :func:`~robovast.common.execution.job_artifact_dir`).

    Returns ``{}`` without touching the manifest when the ledger is empty, which is the
    default for every campaign nobody intervened in.
    """
    entries = read_killed_jobs(campaign_dir)
    if not entries:
        return {}
    from robovast.common.execution import read_job_links
    by_job_dir = {e["job_dir"]: e for e in entries if e.get("job_dir")}
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for run_key in entry.get("runs") or ():
            out[run_key] = entry
    for link, target in read_job_links(campaign_dir).items():
        # ``<config>/<run>/job`` -> ``../../_jobs/<batch>/job-<idx>``; normalizing the
        # target against the link's own directory yields the campaign-relative job dir
        # the ledger records.
        run_key = link[:-len("/job")] if link.endswith("/job") else link
        entry = by_job_dir.get(os.path.normpath(os.path.join(run_key, target)))
        if entry is not None:
            out.setdefault(run_key, entry)
    return out


def killed_failure_message(entry: dict[str, Any]) -> str:
    """The ``failure_message`` a manually-stopped run carries.

    One phrasing, built here, so the reason a reader sees is the same in the ``run``
    table, the SQL views and the web UI rather than three near-identical strings.
    """
    reason = (entry.get("reason") or "").strip()
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
    if not resolved:
        # Nothing declared. An empty file would be indistinguishable from "recorded nothing",
        # and the absent-file case already means "no plugins" to every reader.
        return
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    with open(exec_dir / _PLUGINS_FILENAME, "w", encoding="utf-8") as f:
        yaml.dump(resolved, f, default_flow_style=False, sort_keys=True)


def read_plugins_record(campaign_dir) -> "dict | None":
    """Read ``_execution/plugins.yaml``; ``None`` when the campaign has none.

    ``None`` is not an error and does not mean "no plugins": campaigns recorded before this
    file existed declared plugins without resolving them anywhere, so a caller has to treat
    it as *unknown* rather than empty. Conflating the two would report a re-run as safely
    pinned when nothing about its plugins was ever captured.
    """
    path = Path(campaign_dir) / "_execution" / _PLUGINS_FILENAME
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or None


_LAUNCH_FILENAME = "launch.yaml"

#: Request fields that describe the *launch* rather than the workspace it came from.
#: ``workspace_id``/``config_path`` are deliberately absent: they are workspace-relative, and
#: campaigns are workspace-independent (see ``service/workspaces.py``), so recording them
#: would preserve a binding that means nothing once the campaign exists.
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
                     killed: "dict[str, dict[str, Any]] | None" = None) -> dict[str, Any]:
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

    Args:
        run_dir: The run's directory (``<campaign>/<config>/<run>``).
        campaign_root: The campaign root, which adds ``job_dir`` / ``sysinfo`` from
            :func:`read_run_job` — :meth:`~robovast.common.store.CampaignStore.record_runs`
            turns those into the ``job`` row and the run's ``job_id``.
        killed: A :func:`killed_runs` mapping, when the caller already built one.
            Passed in rather than read here so a whole config's runs share one read of
            the ledger; ``None`` means "look it up", and an empty mapping means "nothing
            was killed" — the default.

    Returns a dict keyed exactly like the ``run`` columns: ``run_id``, ``status``,
    ``passed`` (0/1), ``errors``, ``failures``, ``tests``, ``duration_s``,
    ``start_time``, ``failure_message``.
    """
    run_id = int(run_dir.name) if run_dir.name.isdigit() else -1
    job: dict[str, Any] = {}
    if campaign_root is not None:
        job_dir, sysinfo = read_run_job(run_dir, campaign_root)
        job = {"job_dir": job_dir, "sysinfo": sysinfo}
    try:
        tr = read_test_result(run_dir)
    except (OSError, ET.ParseError, ValueError):
        # A run that never wrote a ``test.xml``, or wrote a truncated/corrupt one
        # (crashed mid-run), still gets a row so it is counted — marked ``unknown``
        # rather than silently dropped. ``OSError`` covers the missing-file case
        # (``FileNotFoundError``); ``ET.ParseError``/``ValueError`` the malformed one.
        if killed is None and campaign_root is not None:
            killed = killed_runs(campaign_root)
        entry = (killed or {}).get(f"{run_dir.parent.name}/{run_dir.name}")
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
    status = "passed" if passed else "error" if errors else "failed"
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

    The manual-kill ledger is resolved **once** here and shared across the config's runs,
    rather than per run: with no ledger that is a single ``is_file`` miss for the whole
    config, and the outcomes are then identical to what this returned before the ledger
    existed.
    """
    killed = killed_runs(campaign_root) if campaign_root is not None else {}
    return [read_run_outcome(rd, campaign_root, killed)
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
        - total_duration_sec: float - Total execution time in seconds
        - execution_info: dict - Execution metadata (version, type, image, etc.)
        - configs: list[dict] - Per-configuration statistics

    Raises:
        FileNotFoundError: If required campaign files are missing.
    """
    # Get execution metadata
    exec_meta = read_execution_metadata(campaign_dir)

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
    total_duration = 0.0

    for config_dir in config_dirs:
        config_name = config_dir.name
        run_dirs = run_discovery_fn(config_dir)

        config_runs = len(run_dirs)
        config_passed = 0
        config_failed = 0
        config_errors = 0
        config_duration = 0.0

        for run_dir in run_dirs:
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
            "duration_sec": config_duration,
        })

        num_runs += config_runs
        num_passed += config_passed
        num_failed += config_failed
        num_errors += config_errors
        total_duration += config_duration

    return {
        "campaign_name": campaign_dir.name,
        "num_configs": len(config_dirs),
        "num_runs": num_runs,
        "num_passed": num_passed,
        "num_failed": num_failed,
        "num_errors": num_errors,
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
RESERVED_CAMPAIGN_DIRS = {"_config", "_execution", "_transient", "_jobs", "_control"}


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


def aggregate_run_status(run_dirs: list[Path]) -> str:
    """Aggregate per-run pass/fail (from each run's ``test.xml``) into one status.

    Returns ``passed`` (all runs passed), ``failed`` (none passed), ``mixed``
    (some of each), or ``no_runs`` (no runs present). A run missing ``test.xml``
    counts against the config.
    """
    passed = failed = 0
    for run_dir in run_dirs:
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
    if failed == 0:
        return "passed"
    if passed == 0:
        return "failed"
    return "mixed"
