#!/usr/bin/env python3
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

"""Collect-all validation for ``.vast`` project files.

The normal load/generate path is fail-fast — it raises on the first problem, and a
YAML error can take the process with it. That is hostile to a program (e.g. the MCP
server) that validates configs an LLM produced: the LLM sees one error at a time, or
the server dies.

``validate_project_file`` runs the same pipeline as a *linter*: it accumulates
**every** problem it can find in one pass, each tagged with a stage, the config
block it belongs to, and the offending field — so a caller gets the full list at
once and can fix a ``.vast`` in far fewer iterations. It reuses the existing
validation helpers rather than duplicating their logic.

It also resolves and interface-checks the ``.vast``'s plugin references — the
variation types, the ``results_processing``/``search`` postprocessing commands,
and the search ``strategy`` and ``extract.plugin`` — whether they are installed
entry-point names or local ``./path.py:Class`` file refs. Those non-variation
plugins are otherwise only resolved when a campaign runs, so a broken local
plugin would surface as a cryptic controller-pod log; here it is caught up front.
This function is the shared core behind both the ``validate_config`` MCP tool
and the ``vast configuration validate`` CLI command.
"""

import inspect
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import yaml
from pydantic import ValidationError

from robovast.common.config import PINNED_REF
from robovast.common.containers import ros_repo_name

logger = logging.getLogger(__name__)

#: Logger whose WARNING records are surfaced in the validation result. Config
#: generation emits non-fatal advisories here (e.g. an ``execution.run_files``
#: pattern that matched nothing) that a caller such as the MCP server would
#: otherwise never see.
_GENERATION_LOGGER = "robovast.common.config_generation"


class _WarningCollector(logging.Handler):
    """Log handler that records the formatted message of every WARNING+ record."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            self.messages.append(record.getMessage())


@contextmanager
def _collect_warnings(logger_name):
    """Yield a list that collects WARNING+ messages logged under ``logger_name``.

    A handler is attached for the duration so the warnings are captured even
    though the underlying logger's own handlers/level are left untouched (they
    still emit to the console as before).
    """
    target = logging.getLogger(logger_name)
    handler = _WarningCollector()
    target.addHandler(handler)
    try:
        yield handler.messages
    finally:
        target.removeHandler(handler)


def _build_context_advisories(config_path):
    """Advise when the build context this project would stage is unexpectedly large.

    An **advisory**, never an error: a project may legitimately be large, and this cannot
    tell the difference. But the context is copied, uploaded and mirrored back down once
    per built container on *every* build, and nothing in BuildKit's output names it — so a
    project that grew one by accident just gets slow builds with no reason given. Reported
    here because a pre-flight check is the one place the cost is still avoidable: after
    this, the next thing that happens is the compute.

    Cheap by construction -- ``stat`` only, and the ignored names (and campaign outputs,
    which are the usual cause) are skipped before anything is measured.
    """
    from robovast.common.build_context import BUILD_CONTEXT_IGNORE, campaign_outputs_in

    # Same threshold the staging path warns at, imported lazily so core does not depend
    # on the cluster package.
    warn_bytes = 50 * 1024 * 1024
    root = Path(config_path).parent
    try:
        campaigns = set(campaign_outputs_in(root))
    except OSError:
        return []
    sizes = {}
    total = 0
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in BUILD_CONTEXT_IGNORE for part in rel.parts):
            continue
        if any(parent in campaigns for parent in (rel, *rel.parents)):
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        sizes[rel.parts[0]] = sizes.get(rel.parts[0], 0) + size
    if total < warn_bytes:
        return []
    biggest = ", ".join(
        f"{name} ({size / 1e6:.0f} MB)"
        for size, name in sorted(((v, k) for k, v in sizes.items()), reverse=True)[:3])
    return [_problem(
        "build-context",
        f"This project would stage a {total / 1e6:.0f} MB build context, copied and "
        f"transferred once per built container on every build. The largest entries are: "
        f"{biggest}. Campaign outputs and the standard ignored names are already "
        f"excluded, so anything left is going into the image build on purpose or by "
        f"accident -- if by accident, move it out of the project directory.")]


def _resource_advisories(config_path):
    """Advise when a container declares ``resources.cpu`` and no ``resources.memory``.

    An **advisory**, never an error: a campaign may legitimately leave memory
    unconstrained, and this cannot tell an intentional choice from an oversight. What it
    can see is the *asymmetry* -- a file that sized the CPU and not the memory reads as one
    where the sizing was done, and it is the half that is missing which bites.

    Two concrete consequences, and neither announces itself. ``AVAILABLE_MEM`` comes from
    the downward API's ``limits.memory``, so with no limit the run is told it has the whole
    node's memory as its budget. And the pod's ``/dev/shm`` is a memory-backed ``emptyDir``
    shared by every container of the run; with no limits to size it from, it too is sized
    from the node. A container that overruns shared memory dies of SIGBUS -- ``exit 135``,
    not ``OOMKilled`` -- so the death arrives with no reason attached to it at all.

    Deliberately NOT triggered when neither is declared: that is the unconstrained default
    a quick local run legitimately uses, and warning about it would be noise on every
    example in the repository.

    Lane-agnostic, because a ``.vast`` does not carry a lane -- and the local lane has the
    same gap from the other side, falling back to ``/proc/meminfo`` for the same reason.
    """
    raw, problem = _safe_load(config_path)
    if problem or not isinstance(raw, dict):
        return []
    containers = ((raw.get("execution") or {}).get("containers") or {})
    if not isinstance(containers, dict):
        return []
    bare = []
    for name, block in containers.items():
        if not isinstance(block, dict):
            continue
        resources = block.get("resources")
        if not isinstance(resources, dict):
            continue
        if resources.get("cpu") is not None and resources.get("memory") is None:
            bare.append(name)
    if not bare:
        return []
    named = ", ".join(f"execution.containers.{n}" for n in sorted(bare))
    return [_problem(
        "resources",
        f"{named} declare resources.cpu but no resources.memory. Without a memory limit "
        "the run's AVAILABLE_MEM (downward API limits.memory) reports the NODE's memory "
        "as its budget, so a process sizing itself from it will size itself to the node. "
        "Declare resources.memory for every container that declares resources.cpu. "
        "get_campaign_summary on a comparable finished campaign reports what it used.")]


def _calibration_role_advisories(config_path):
    """Advise when a calibrated campaign has containers not named after a role.

    A role comes from the container's NAME -- ``scenario``, ``simulation``, ``sut`` -- and
    cannot be declared. Under ``execution.sizing: calibrated`` a container named anything
    else takes the ad-hoc bootstrap rather than its role's, and is sized on its SUSTAINED
    use rather than its peak once measured.

    An **advisory**, not an error: an ad-hoc container is a legitimate thing to have, and
    for one that is genuinely auxiliary both defaults are right. It matters when the
    container is the system under test under another name, because the peak rule exists so
    the thing under test never throttles mid-plan -- and that failure is quiet, looking like
    the stack rather than the allocation.

    Only reported for a campaign that would be calibrated. With a declared figure the name
    decides nothing, which is why this is silent for every ``.vast`` written so far.
    """
    raw, problem = _safe_load(config_path)
    if problem or not isinstance(raw, dict):
        return []
    execution = raw.get("execution") or {}
    containers = execution.get("containers") or {}
    if not isinstance(containers, dict) or not containers:
        return []
    declares = any(
        isinstance(b, dict) and isinstance(b.get("resources"), dict)
        and any(b["resources"].get(k) is not None
                for k in ("cpu", "cpu_limit", "memory", "memory_limit"))
        for b in containers.values())
    sizing = execution.get("sizing") or ("fixed" if declares else "calibrated")
    if sizing != "calibrated":
        return []
    roles = ("scenario", "simulation", "sut")
    adhoc = sorted(n for n in containers if n not in roles)
    if not adhoc:
        return []
    named = ", ".join(f"execution.containers.{n}" for n in adhoc)
    return [_problem(
        "resources",
        f"{named} {'is' if len(adhoc) == 1 else 'are'} not named after a role "
        f"({'/'.join(roles)}), and execution.sizing is calibrated -- so each takes the "
        "ad-hoc bootstrap and is sized on its sustained use rather than its peak. If one of "
        "them is the system under test, rename it to 'sut': the peak rule exists so the "
        "thing under test never throttles mid-plan, and it keys on the name. The name is "
        "also what a scenario's remote(\"ipc:///ipc/<name>\") and exec_in_container use, so "
        "rename those with it.",
        field="execution.containers")]


def _liveness_advisories(config_path):
    """Advise when the project declares no ``execution.timeout``.

    An **advisory**, never an error: a campaign runs perfectly well without one. What it
    cannot do is be *judged*. ``stalled`` is asserted only against a declared per-run
    budget (see :func:`~robovast.common.config.declared_per_run_seconds`), so with none
    the verdict is ``null`` forever -- a wedged run and a slow one stay the same picture,
    and ``vast campaign wait`` has nothing to exit 4 on.

    Said here rather than shown in a reader, because a reader can only repeat it on every
    poll for the life of the campaign, and by then the fix costs a re-run. Here it costs a
    line in the file, before any compute.
    """
    raw, problem = _safe_load(config_path)
    if problem or not isinstance(raw, dict):
        return []
    if (raw.get("execution") or {}).get("timeout"):
        return []
    return [_problem(
        "liveness",
        "execution.timeout is not declared, so a wedged run is never reported: stalled "
        "stays null and `vast campaign wait` cannot exit 4 on it. The enforcement backstop is "
        "deliberately not used to judge -- it would call a dead run healthy for an hour. "
        "Set execution.timeout to the longest a single run should legitimately take; "
        "get_campaign_summary on a comparable finished campaign reports what its runs "
        "took.",
        field="execution.timeout")]


def _problem(stage, message, config=None, field=None):
    """Build one structured problem entry."""
    return {"stage": stage, "config": config, "field": field, "message": message}


def _safe_load(config_path):
    """Parse the first YAML document of a ``.vast`` file. Returns ``(raw, problem)``.

    Never raises for content problems — a parse/read error is returned as a
    structured problem so the caller can report it instead of crashing.
    """
    if not config_path or not os.path.exists(config_path):
        return None, _problem("file", f"Config file not found: {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            documents = list(yaml.safe_load_all(f))
    except yaml.YAMLError as e:
        return None, _problem("parse", f"YAML parse error: {e}")
    except OSError as e:
        return None, _problem("file", f"Could not read {config_path}: {e}")
    if not documents or documents[0] is None:
        return None, _problem("parse", "No documents found in the .vast file.")
    if not isinstance(documents[0], dict):
        return None, _problem("parse", "Top-level .vast content is not a mapping.")
    return documents[0], None


def _config_name_from_loc(raw, loc):
    """Best-effort: map a pydantic error location to a configuration block name."""
    if len(loc) >= 2 and loc[0] == "configuration" and isinstance(loc[1], int):
        blocks = raw.get("configuration") or []
        if isinstance(blocks, list) and 0 <= loc[1] < len(blocks):
            block = blocks[loc[1]]
            if isinstance(block, dict):
                return block.get("name", f"configuration[{loc[1]}]")
        return f"configuration[{loc[1]}]"
    return None


def _schema_problems(raw):
    """Run the pydantic schema and return structured field problems (collect-all)."""
    from robovast.common.config import ConfigV1  # pylint: disable=import-outside-toplevel

    from robovast.common.migrations import (  # pylint: disable=import-outside-toplevel
        BASELINE_CONFIG_VERSION, SUPPORTED_CONFIG_VERSION)

    problems = []
    version = raw.get("version")
    if isinstance(version, int) and BASELINE_CONFIG_VERSION <= version < SUPPORTED_CONFIG_VERSION:
        # Name the one-command fix, then explain the restructuring. This is the collect-all
        # validator -- the surface an agent or the web editor hits first -- so the actionable
        # line has to come before the wall of detail.
        from robovast.common.config import _V1_MIGRATION  # pylint: disable=import-outside-toplevel
        problems.append(_problem(
            "version",
            f"config version {version} is not the current version "
            f"({SUPPORTED_CONFIG_VERSION}). Upgrade it with 'vast configuration upgrade'. "
            f"An archived campaign is migrated automatically when read, so this applies "
            f"only to a file being authored.\n\n" + _V1_MIGRATION,
            field="version"))
    elif version != SUPPORTED_CONFIG_VERSION:
        problems.append(_problem(
            "version",
            f"Unsupported config version: {version!r} (expected {SUPPORTED_CONFIG_VERSION}).",
            field="version"))
    try:
        ConfigV1(**raw)
    except ValidationError as e:
        # Pydantic collects field errors AND @model_validator errors in one pass.
        for err in e.errors():
            loc = err.get("loc", ())
            problems.append(_problem(
                "schema",
                err.get("msg", "invalid value"),
                config=_config_name_from_loc(raw, loc),
                field=".".join(str(part) for part in loc) or None))
    except Exception as e:  # noqa: BLE001 - non-pydantic construction error
        problems.append(_problem("schema", str(e)))
    return problems


def _migration_marker_problems(raw):
    """One problem per unresolved migration marker.

    Reported individually rather than as a single "this file is a work order", because each marker
    is a separate decision somebody has to make and they are usually in different places. A count
    would make them go looking.
    """
    from robovast.common.migrations import \
        find_migration_markers  # pylint: disable=import-outside-toplevel

    return [_problem("migration", f"unresolved migration marker: {reason}", field=where)
            for where, reason in find_migration_markers(raw)]


def _unresolvable_image_problems(raw):
    """Refuse a container a launch could not find an image for.

    The schema deliberately lets a known role omit ``image``, which is right for ``scenario`` and
    for ``simulation`` behind a backend -- but a launch resolves the framework fallback for the
    *main* container only, so ``sut`` without one validated and then failed at launch. Sharing the
    launch's own rule is what stops the two disagreeing; the alternative, a fallback for every
    role, would run something nobody named as the system under test.
    """
    from robovast.common.containers import \
        containers_without_a_resolvable_image  # pylint: disable=import-outside-toplevel

    try:
        unresolvable = containers_without_a_resolvable_image(raw.get("execution") or {})
    except Exception:  # noqa: BLE001 - a malformed execution block is another check's problem
        return []
    return [_problem("image", why, config=name, field=f"execution.containers.{name}.image")
            for name, why in unresolvable]


def _image_provenance_problems(raw):
    """Refuse a container whose image nobody could later identify.

    Reported here, in the collect-all validator, so an author learns it beside every other problem
    and before any compute is spent -- rather than at launch, or worse, a year later when someone
    tries to re-run the campaign and the image is a name with no meaning.

    Deliberately *not* a pydantic validator on the model: that would run on every load, including
    reading an ARCHIVED campaign, and would make exactly the historic campaigns this must keep
    readable unreadable instead. The rule is a property of **authoring**, so it lives on the
    authoring path.
    """
    from robovast.common.execution import \
        opaque_image_containers  # pylint: disable=import-outside-toplevel

    return [_problem("image-provenance", why, config=name,
                     field=f"execution.containers.{name}.provenance")
            for name, why in opaque_image_containers(raw.get("execution") or {})]


def _scenario_file_problems(raw, vast_dir):
    """Validate ``execution.scenario_file``. Returns ``(problems, abs_scenario_file)``."""
    from robovast.common.config_generation import \
        _validate_relative_path  # pylint: disable=import-outside-toplevel

    problems = []
    execution = raw.get("execution") or {}
    name = execution.get("scenario_file") if isinstance(execution, dict) else None
    if not name:
        problems.append(_problem(
            "scenario_file",
            "No scenario_file specified. Add 'scenario_file' to the execution section.",
            field="execution.scenario_file"))
        return problems, None
    try:
        _validate_relative_path(name, "execution.scenario_file")
    except ValueError as e:
        problems.append(_problem("scenario_file", str(e), field="execution.scenario_file"))
        return problems, None
    scenario_file = os.path.join(vast_dir, name)
    if not os.path.exists(scenario_file):
        problems.append(_problem(
            "scenario_file", f"Scenario file does not exist: {scenario_file}",
            field="execution.scenario_file"))
        return problems, None
    return problems, scenario_file


#: Where a run-view panel lives in the config, as the reader sees it. Used to build the
#: ``field`` of every panel problem, so a report points at the key the author actually wrote.
RUN_VIEW_PANELS = "visualization.results.run_view.panels"

#: ...and where an Explorer notebook lives, for the same reason.
EXPLORER_NOTEBOOKS = "visualization.results.explorer.notebooks"


def _run_view_panels(raw):
    """The raw ``visualization.results.run_view.panels`` list, or ``[]``.

    Every panel check walks the same path, and inlining it in each is one copy per check of
    a key that can be renamed.
    """
    from robovast.common.config import \
        visualization_block  # pylint: disable=import-outside-toplevel
    panels = visualization_block(raw, "results", "run_view", "panels")
    return panels if isinstance(panels, list) else []


def _panel_entry(entry):
    """Extract ``(type, fields)`` from a raw run-view panels entry, accepting the
    key-as-type shorthand (``- costmap: {...}`` / bare ``- playback``) as well as the
    flattened ``{type, ...}`` form. Returns ``(None, {})`` for an unrecognized shape."""
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict):
        if "type" in entry:
            return entry.get("type"), entry
        if len(entry) == 1:
            (ptype, props), = entry.items()
            return ptype, (props or {})
    return None, {}


def _generator_problems(raw, vast_dir):
    """Validate ``execution.generate``: entry shape, ``out``, and generator resolution.

    Resolution is checked here rather than left to composition because the failure it
    catches — a generator that is not installed *in the process that composes* — is
    otherwise discovered after the user has already committed to a run.
    """
    from robovast.common.input_generation import (  # pylint: disable=import-outside-toplevel
        load_input_generators, parse_generate_entry, resolve_input_generator, resolve_out_dir)

    problems = []
    entries = ((raw.get("execution") or {}).get("generate")) or []
    if not isinstance(entries, list):
        return [_problem("generate", "execution.generate must be a list of generator "
                                     "entries", field="execution.generate")]
    plugins = load_input_generators()
    claimed = {}
    for i, entry in enumerate(entries):
        field = f"execution.generate[{i}]"
        try:
            name, params = parse_generate_entry(entry, i)
        except Exception as e:  # noqa: BLE001 - reported as a validation problem
            problems.append(_problem("generate", str(e), field=field))
            continue
        try:
            resolve_out_dir(params.get("out"), vast_dir, f"{field}.{name}")
        except Exception as e:  # noqa: BLE001
            problems.append(_problem("generate", str(e), field=f"{field}.out"))
            continue
        out = params.get("out")
        if out in claimed:
            problems.append(_problem(
                "generate",
                f"out {out!r} is already written by execution.generate[{claimed[out]}]; "
                f"two generators cannot share an output directory.",
                field=f"{field}.out"))
            continue
        claimed[out] = i
        try:
            resolve_input_generator(name, vast_dir, plugins)
        except Exception as e:  # noqa: BLE001
            problems.append(_problem("generate", str(e), field=f"{field}.{name}"))
        _warn_escaping_inputs(params.get("inputs"), vast_dir, f"{field}.{name}")
    return problems


def _warn_escaping_inputs(inputs, vast_dir, field):
    """Advise when a declared input lives outside the project directory.

    An advisory, not a problem: composing from the project tree in place, such a path
    resolves fine, and a campaign deliberately reading a sibling checkout is a legitimate
    arrangement. But only the project directory is copied into a service workspace, so the
    same ``.vast`` started from a workspace (as the cluster lane does) composes against a
    path that is not there — a difference invisible until the lane changes, and one that
    then reads as a lane fault rather than a config one.
    """
    vast_dir = os.path.abspath(vast_dir)
    for path in inputs or []:
        if not isinstance(path, str):
            continue
        resolved = os.path.abspath(os.path.join(vast_dir, path))
        if os.path.commonpath([resolved, vast_dir]) == vast_dir:
            continue
        logger.warning(
            "%s: input %r resolves outside the project directory (%s). It composes from "
            "this tree in place, but only the project directory is copied into a service "
            "workspace, so started from one (as the cluster lane does) this generator "
            "would not find it.", field, path, resolved)


def _generated_out_dirs(raw):
    """The ``out`` directories ``execution.generate`` declares (normalised, no slash)."""
    outs = []
    for entry in ((raw.get("execution") or {}).get("generate")) or []:
        params = entry.get(next(iter(entry))) if isinstance(entry, dict) and len(entry) == 1 else None
        out = (params or {}).get("out") if isinstance(params, dict) else None
        if isinstance(out, str) and out:
            outs.append(out.strip("/"))
    return outs


def _scene_descriptor_problems(raw, vast_dir):
    """A campaign-scope ``scene3d`` descriptor must be produced by *something*.

    This is the check whose absence was the original bug: the descriptor was built by a
    side script and delivered through a ``run_files`` glob, so forgetting the script left
    a campaign that ran, passed, and only 404'd in the browser afterwards. Nothing in the
    campaign declared the dependency, so nothing could complain.

    Only campaign-scope paths under ``_config/`` are checkable here — a ``scope: run``
    descriptor is written by the simulation at run time and cannot exist yet.
    """
    from robovast.common.config_generation import \
        matches_patterns  # pylint: disable=import-outside-toplevel

    problems = []
    patterns = ((raw.get("execution") or {}).get("run_files")) or []
    generated = _generated_out_dirs(raw)
    for i, entry in enumerate(_run_view_panels(raw)):
        ptype, props = _panel_entry(entry)
        if ptype != "scene3d" or not isinstance(props, dict):
            continue
        scene = props.get("scene")
        if not isinstance(scene, dict):
            continue
        path = scene.get("path")
        if not isinstance(path, str) or str(scene.get("scope", "run")) != "campaign":
            continue
        if not path.startswith("_config/"):
            continue
        # Campaign-relative '_config/<rel>' addresses the same '<rel>' that run_files
        # and generators write, relative to the .vast.
        rel = path[len("_config/"):]
        field = f"{RUN_VIEW_PANELS}[{i}].scene.path"
        if any(rel == out or rel.startswith(out + "/") for out in generated):
            continue
        abs_path = os.path.join(vast_dir, rel)
        if os.path.isfile(abs_path) and matches_patterns(abs_path, patterns, vast_dir):
            continue
        problems.append(_problem(
            "panel",
            f"{path!r} is not produced by anything in this campaign — no "
            f"execution.run_files pattern matches it and no execution.generate entry "
            f"writes it. The panel would 404 at view time. Add a generator that builds "
            f"the descriptor, or a run_files pattern covering an existing one.",
            field=field))
    return problems


def _env_names(raw):
    """Names declared in ``execution.env``, accepting both shapes it is written in.

    The documented form is a list of single-key mappings (``- MY_VAR: "..."``); a plain
    mapping is accepted too.
    """
    env = (raw.get("execution") or {}).get("env")
    names = set()
    if isinstance(env, dict):
        names.update(str(k) for k in env)
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, dict):
                names.update(str(k) for k in item)
            elif isinstance(item, str):
                names.add(item.split("=", 1)[0])
    return names


def _run_capture_problems(raw):
    """A ``scene3d`` panel replays a **run capture**, so the runs have to produce one.

    Nothing else in the campaign declares that dependency, so without this check a campaign
    runs, passes, and shows a motionless world whenever someone finally opens it. The capture
    is written per run at simulation time, so unlike a campaign-scope descriptor it cannot be
    looked for on disk -- what *can* be established is whether the configured simulator
    produces one at all, which is the mistake actually made.

    Asked of the **backend**, not inferred from the campaign's wheel names. The old check
    pattern-matched ``roqsim`` in the simulation ref and the installed packages, which was the
    only signal available before a simulator was a first-class thing -- and which now finds
    nothing at all in the shape where the simulator runs from its own image and the campaign
    installs no simulator packages whatsoever.

    A campaign with no backend is not second-guessed: nothing here could tell where its
    capture would come from.
    """
    problems = []
    panels = [(i, entry) for i, entry in enumerate(_run_view_panels(raw))
              if _panel_entry(entry)[0] == "scene3d"]
    if not panels:
        return problems

    from robovast.common.simulators import backend_name  # pylint: disable=import-outside-toplevel
    from robovast.common.simulators import resolve_backend
    execution = raw.get("execution") or {}
    name = backend_name(execution)
    if not name:
        return problems
    try:
        backend = resolve_backend(name)
        cfg = (execution.get("containers") or {}).get("simulation") or {}
        if backend.produces_run_capture(cfg, execution):
            return problems
    except Exception:  # noqa: BLE001 - an unresolvable backend is reported by the schema
        return problems

    return [_problem(
        "panel",
        f"the scene3d panel replays a run capture, but the '{name}' simulator does not "
        f"produce one. The campaign would run and pass, and the 3D view would show a world "
        f"that never moves.",
        field=f"{RUN_VIEW_PANELS}[{i}]") for i, _entry in panels]


def _camera_panel_problems(raw):
    """A ``camera`` panel plays a video some step has to *produce*.

    Same failure mode as the ``scene3d`` check above, one layer down: nothing else in the
    campaign says the panel needs a video, so without this the campaign runs, passes, and
    shows "this run registered no video" to whoever finally opens it — after the compute is
    spent.

    Only the ``videos``-table path is checked. A panel naming ``source.path`` points at a
    file it takes responsibility for (the escape hatch for a video no producer registered),
    and second-guessing that would reject the one case the hatch exists for.

    Deliberately shallow: this asks whether *a* video producer is declared, not whether it
    names the right topic. Which topics a scenario records is in the ``.osc``, which is not
    parsed here — and a wrong topic already fails loudly at postprocessing time with
    "topic not in bag".
    """
    from robovast.results_processing.postprocessing import \
        VIDEO_PRODUCER_COMMANDS  # pylint: disable=import-outside-toplevel

    problems = []
    panels = [(i, props) for i, entry in enumerate(_run_view_panels(raw))
              for ptype, props in [_panel_entry(entry)] if ptype == "camera"]
    if not panels:
        return problems

    entries = ((raw.get("results_processing") or {}).get("postprocessing") or [])
    declared = {c if isinstance(c, str) else next(iter(c), None)
                for c in entries if isinstance(c, (str, dict))}
    if VIDEO_PRODUCER_COMMANDS & declared:
        return problems

    return [_problem(
        "panel",
        "a camera panel plays a video listed in the `videos` table, but this campaign "
        "declares no step that produces one. Add `rosbags_to_webm` (naming the image topic "
        "the scenario records) to results_processing.postprocessing, or point the panel at a "
        "file directly with `source: {path: ..., t0: ...}`.",
        field=f"{RUN_VIEW_PANELS}[{i}]")
        for i, props in panels if not ((props or {}).get("source") or {}).get("path")]


def _vega_panel_problems(i, props):
    """The ``vega`` panel's bindings, as a collect-all check.

    Mirrors ``RunViewPanelConfig._vega_needs_bindings``; the duplication is the point — the schema
    raises on the first bad panel, this reports every one of them in a single report."""
    from robovast.common.config import \
        panel_source_problems  # pylint: disable=import-outside-toplevel

    problems = []
    prefix = f"{RUN_VIEW_PANELS}[{i}]"
    spec = props.get("vega_lite")
    if not isinstance(spec, dict) or not spec:
        problems.append(_problem(
            "panel",
            "a 'vega' panel must set 'vega_lite' to a non-empty Vega-Lite spec (mark/encoding, "
            "or layer/vconcat); the panel binds the rows as its data, so the spec declares no "
            "'data' block",
            field=f"{prefix}.vega_lite"))
    source = props.get("source")
    if not isinstance(source, dict) or not source.get("table"):
        problems.append(_problem(
            "panel",
            "a 'vega' panel must set 'source' to a data.db table, e.g. "
            "source: {table: poses, filter: {frame: base_link}}",
            field=f"{prefix}.source"))
    for field, message in panel_source_problems(props):
        problems.append(_problem("panel", message, field=f"{prefix}.{field}"))
    return problems


def _panel_problems(raw, vast_dir):
    """Validate the run-view panels beyond the schema: a ``custom`` panel's built
    bundle must exist next to the ``.vast``, and a ``vega`` panel carries the bindings it
    needs. Package panels (entry-point types) are only name-checked by the schema — their
    built assets ship with the plugin and may be absent in a source checkout, so they are
    not required here."""
    from robovast.common.config import (  # pylint: disable=import-outside-toplevel
        CUSTOM_PANEL_TYPE, VEGA_PANEL_TYPE)
    from robovast.common.config_generation import \
        _validate_relative_path  # pylint: disable=import-outside-toplevel

    problems = []
    for i, entry in enumerate(_run_view_panels(raw)):
        ptype, props = _panel_entry(entry)
        if ptype == VEGA_PANEL_TYPE:
            problems.extend(_vega_panel_problems(i, props if isinstance(props, dict) else {}))
            continue
        if ptype != CUSTOM_PANEL_TYPE:
            continue
        remote = props.get("remote") if isinstance(props, dict) else None
        if not remote:
            continue  # schema already flags a custom panel missing 'remote'
        field = f"{RUN_VIEW_PANELS}[{i}].remote"
        try:
            _validate_relative_path(remote, field)
        except ValueError as e:
            problems.append(_problem("panel", str(e), field=field))
            continue
        path = os.path.join(vast_dir, remote)
        entry_js = path if path.endswith(".js") else os.path.join(path, "remoteEntry.js")
        if not os.path.exists(entry_js):
            problems.append(_problem(
                "panel",
                f"custom panel bundle not found: {entry_js} (build the panel and place its "
                f"remoteEntry.js under {remote!r} relative to the .vast)",
                field=field))
    return problems


def _explorer_notebook_problems(raw, vast_dir):
    """Every notebook the Explorer declares must exist next to the ``.vast``.

    A declared notebook that is not in the project is skipped at staging with a warning in
    the controller log, and the campaign then runs to completion with an Explorer tab that
    cannot render -- the failure arrives when someone opens the results, days later, and the
    only record of the cause is a log line nobody is reading by then. It is also the exact
    shape of a project pushed without its ``analysis/`` directory, which is easy to do and
    which nothing else reports.
    """
    from robovast.common.config import \
        visualization_block  # pylint: disable=import-outside-toplevel
    from robovast.common.config_generation import \
        _validate_relative_path  # pylint: disable=import-outside-toplevel

    views = visualization_block(raw, "results", "explorer", "notebooks")
    if not isinstance(views, list):
        return []

    problems = []
    for i, view in enumerate(views):
        if not isinstance(view, dict):
            continue
        for workload, scopes in view.items():
            if not isinstance(scopes, dict):
                continue
            for scope, rel in scopes.items():
                if not isinstance(rel, str) or not rel:
                    continue
                field = f"{EXPLORER_NOTEBOOKS}[{i}].{workload}.{scope}"
                try:
                    _validate_relative_path(rel, field)
                except ValueError as e:
                    problems.append(_problem("notebook", str(e), field=field))
                    continue
                if not os.path.isfile(os.path.join(vast_dir, rel)):
                    problems.append(_problem(
                        "notebook",
                        f"analysis notebook not found: {rel} (declared for the {scope!r} "
                        f"scope of workload {workload!r}; the path is relative to the "
                        f".vast, and the file must be in the project that is pushed)",
                        field=field))
    return problems


def _scenario_parameter_names(scenario_file):
    """Return the parameter names declared by the scenario, or None if unreadable."""
    from robovast.common.common import \
        get_scenario_parameters  # pylint: disable=import-outside-toplevel

    try:
        param_dict = get_scenario_parameters(scenario_file)
    except Exception as e:  # noqa: BLE001 - scenario parse failures are surfaced by caller
        logger.debug("Could not read scenario parameters from %s: %s", scenario_file, e)
        return None
    declared = next(iter(param_dict.values())) if param_dict else []
    return [p.get("name") for p in declared
            if isinstance(p, dict) and "name" in p]


def _unresolved_variation_problem(exc, vast_dir, declared_plugins, config_name):
    """Turn an unresolved variation name into the *true* statement about it, or ``None``.

    Three different situations reach here, and reporting them identically makes a correct
    ``.vast`` look broken:

    * **A staged plugin registers the name.** The project's ``.robovast_plugins/`` already
      holds the package (``vast workspace update``, or any earlier composition), so the name
      is real and composition will resolve it. This is not a problem with the file, so none
      is reported. It is deliberately not *confirmed* either: knowing the class behind the
      name is a valid ``Variation`` needs the import, and validation must not import plugin
      code into this long-lived process (see ``config_plugins._prepend_sys_path``).
    * **``plugins:`` declares specs that are not staged yet.** The name cannot be resolved
      *here* -- and the generic message's advice, "declare that package in ``plugins:``", is
      advice the author has already taken, so it reads as a wrong declaration or a missing
      credential. It cost two re-uploads of a plugin that was correct all along. Say what is
      actually true instead, and name what settles it.
    * **Nothing declares it, or a staged project does not provide it.** The generic message is
      right -- and for the second case it is better than the one above, since a staged project
      has already composed and the name really is unknown. It also lists what does exist,
      which is what a typo needs. Pass it through.
    """
    from robovast.common.config_plugins import \
        staged_variation_type_names  # pylint: disable=import-outside-toplevel

    staged = staged_variation_type_names(vast_dir)
    if exc.class_name and exc.class_name in staged:
        return None
    # Only while nothing is staged. A project that *has* a plugin dir has already composed,
    # so "this resolves once you compose" would be false there -- and a name that dir does
    # not provide is genuinely unknown (a typo, most often), which the generic message
    # serves better: it lists the names that do exist.
    if declared_plugins and not staged:
        return _problem(
            "variation",
            f"Variation class '{exc.class_name}' did not resolve here. The .vast declares "
            "'plugins:', and declared specs are installed during config *generation*, not by "
            "this check -- so this is expected until the project has composed once, and is "
            "not evidence that the declaration is wrong. What settles it: "
            "preview_configurations(limit=1) composes, installing the specs into "
            ".robovast_plugins/ first, and then either expands the sweep or fails with the "
            "plugin's own reason. Seconds, and no compute.",
            config=config_name, field="variations")
    return _problem("variation", str(exc), config=config_name, field="variations")


def _config_block_problems(config, vast_dir, valid_param_names, declared_plugins=()):
    """Accumulate problems for a single configuration block.

    *declared_plugins* is the ``.vast``'s top-level ``plugins:`` list, needed only to say
    something true about an unresolved variation name -- see the handler below.
    """
    from robovast.common.config import \
        get_validated_config  # pylint: disable=import-outside-toplevel
    from robovast.common.config_generation import (  # pylint: disable=import-outside-toplevel
        UnknownVariationClass, _get_variation_classes)

    problems = []
    name = config.get("name", "<unnamed>") if isinstance(config, dict) else "<unnamed>"

    # Variation-type resolution (unknown type / invalid local plugin).
    variation_classes = []
    try:
        variation_classes = _get_variation_classes(config, vast_dir)
    except UnknownVariationClass as e:
        problem = _unresolved_variation_problem(e, vast_dir, declared_plugins, name)
        if problem is not None:
            problems.append(problem)
    except ValueError as e:
        problems.append(_problem("variation", str(e), config=name, field="variations"))

    # Per-variation parameter schema (each plugin's optional CONFIG_CLASS).
    for variation_class, variation_params in variation_classes:
        config_class = getattr(variation_class, "CONFIG_CLASS", None)
        if config_class is not None and isinstance(variation_params, dict):
            try:
                get_validated_config(variation_params, config_class)
            except ValueError as e:
                problems.append(_problem(
                    "variation-params", str(e), config=name,
                    field=f"variations.{variation_class.__name__}"))

    # Scenario-parameter references (only checkable if the scenario was readable).
    if valid_param_names is not None:
        config_dict = {}
        for param in config.get("parameters", []) or []:
            if isinstance(param, dict):
                config_dict.update(param)
        unknown = [p for p in config_dict if p not in valid_param_names]
        if unknown:
            problems.append(_problem(
                "parameters",
                f"Unknown scenario parameter(s): {', '.join(unknown)}. "
                f"Declared by the scenario: {', '.join(valid_param_names) or '(none)'}.",
                config=name, field="parameters"))
    return problems


def _postprocessing_problems(entries, vast_dir, field_prefix):
    """Resolve every postprocessing command (entry-point name or local file ref).

    Uses the same resolver the runtime uses (``resolve_postprocessing_plugin``),
    so a broken local ``./path.py:Class`` — unknown name, import error, missing
    class, not a ``BasePostprocessingPlugin`` — is caught here instead of in a
    controller-pod log after launch. Collect-all: never raises.

    The ``rosbags_*`` command names in ``ROSBAG_BATCH_NAMES`` are not entry points
    but are transparently rewritten into a batched ``rosbags_process`` call at
    runtime, so they are accepted here just as the runtime accepts them —
    otherwise validation would reject configs that actually execute fine.
    """
    from robovast.results_processing.postprocessing import (  # pylint: disable=import-outside-toplevel
        ROSBAG_BATCH_NAMES, resolve_postprocessing_plugin)

    problems = []
    for i, command in enumerate(entries or []):
        if isinstance(command, str):
            name = command
        elif isinstance(command, dict) and len(command) == 1:
            name = next(iter(command))
        else:
            problems.append(_problem(
                "postprocessing",
                f"Invalid postprocessing entry (expected a name or single-key "
                f"mapping): {command!r}",
                field=f"{field_prefix}[{i}]"))
            continue
        if name in ROSBAG_BATCH_NAMES:
            continue  # rewritten to rosbags_process at runtime; not an entry point
        try:
            resolve_postprocessing_plugin(name, vast_dir)
        except Exception as e:  # noqa: BLE001 - surface any resolution error
            problems.append(_problem(
                "postprocessing", str(e), field=f"{field_prefix}[{i}]"))
    return problems


def _search_problems(search, vast_dir):
    """Resolve and interface-check the search strategy and extractor plugins.

    Both are referenced like every other plugin (entry-point name or local
    ``./path.py:Class`` file ref) and are otherwise only resolved when a search
    actually runs. Collect-all: never raises.
    """
    from robovast.common.config import \
        get_validated_config  # pylint: disable=import-outside-toplevel
    from robovast.search.extractor import Extractor  # pylint: disable=import-outside-toplevel
    from robovast.search.plugins import EXTRACTOR_GROUP  # pylint: disable=import-outside-toplevel
    from robovast.search.plugins import STRATEGY_GROUP, load_ref
    from robovast.search.strategy import SearchStrategy  # pylint: disable=import-outside-toplevel

    problems = []

    # Both refs below may be local `./file.py:Class` modules whose third-party imports the
    # `.vast` declares in `plugins:`. Lead sys.path with them first, or validation reports a
    # ModuleNotFoundError as a config problem for a file that is in fact correctly declared --
    # which is what it did, refusing campaigns the launcher then accepted and ran.
    if vast_dir:
        from robovast.common.config_plugins import \
            ensure_plugins_importable  # pylint: disable=import-outside-toplevel
        ensure_plugins_importable(vast_dir)

    # -- strategy (+ its optional PARAMS_MODEL) ------------------------------
    strategy = search.get("strategy")
    if isinstance(strategy, str) and strategy:
        try:
            strategy_cls = load_ref(strategy, STRATEGY_GROUP, vast_dir)
        except Exception as e:  # noqa: BLE001 - surface any resolution error
            problems.append(_problem("search-strategy", str(e), field="search.strategy"))
        else:
            if not (inspect.isclass(strategy_cls)
                    and issubclass(strategy_cls, SearchStrategy)):
                problems.append(_problem(
                    "search-strategy",
                    f"'{strategy}' is not a subclass of SearchStrategy.",
                    field="search.strategy"))
            else:
                params_model = getattr(strategy_cls, "PARAMS_MODEL", None)
                params = search.get("strategy_parameters") or {}
                if params_model is not None and isinstance(params, dict):
                    try:
                        get_validated_config(params, params_model)
                    except ValueError as e:
                        problems.append(_problem(
                            "search-strategy-params", str(e),
                            field="search.strategy_parameters"))

    # -- extractor ----------------------------------------------------------
    extract = search.get("extract")
    if isinstance(extract, dict):
        plugin = extract.get("plugin")
        if isinstance(plugin, str) and plugin:
            try:
                extractor_cls = load_ref(plugin, EXTRACTOR_GROUP, vast_dir)
            except Exception as e:  # noqa: BLE001 - surface any resolution error
                problems.append(_problem(
                    "search-extractor", str(e), field="search.extract.plugin"))
            else:
                if not (inspect.isclass(extractor_cls)
                        and issubclass(extractor_cls, Extractor)):
                    problems.append(_problem(
                        "search-extractor",
                        f"'{plugin}' is not a subclass of Extractor.",
                        field="search.extract.plugin"))
                elif getattr(extractor_cls, "extract", None) is Extractor.extract:
                    problems.append(_problem(
                        "search-extractor",
                        f"'{plugin}' does not override the 'extract' method.",
                        field="search.extract.plugin"))

    return problems


def _plugin_ref_problems(raw, vast_dir):
    """Resolve & interface-check every non-variation plugin ref in the ``.vast``.

    Variation plugins are already checked per config block; this covers the other
    plugin-carrying sections — ``results_processing.postprocessing``,
    ``search.postprocessing``, ``search.strategy`` and ``search.extract.plugin``
    — reusing the runtime resolvers so validation matches execution. Collect-all:
    never raises.
    """
    problems = []

    results = raw.get("results_processing")
    if isinstance(results, dict):
        problems.extend(_postprocessing_problems(
            results.get("postprocessing"), vast_dir,
            "results_processing.postprocessing"))

    search = raw.get("search")
    if isinstance(search, dict):
        problems.extend(_postprocessing_problems(
            search.get("postprocessing"), vast_dir, "search.postprocessing"))
        problems.extend(_search_problems(search, vast_dir))

    return problems


def _build_problems(raw, vast_dir):
    """Fail-fast checks on a ``build:`` section's workspace-path references.

    A ``build.python_packages`` entry that is a workspace path (a source dir or a
    ``.whl``) must actually exist in the project; index pins / git URLs are pip
    specs and are not checked here (not resolvable offline). Tag shape and the
    ``execution.image`` <-> ``build.tag`` consistency are enforced by the schema.

    Entries may be grouped (a list of specs installed in one pip pass), so this
    walks one level in: a path is a path in either form.
    """
    problems = []
    build = raw.get("build")
    if not isinstance(build, dict):
        return problems
    authored = build.get("python_packages", []) or []
    entries = [spec for e in authored
               for spec in (e if isinstance(e, list) else [e])]
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        if _missing_workspace_path(entry, vast_dir):
            problems.append(_problem(
                "build",
                f"'{entry}' looks like a workspace path but no such directory/wheel "
                "exists in the project",
                field="build.python_packages"))
    return problems


def ros_packages_problems(entries, where: str) -> list:
    """What can be said about a container's ``ros_packages`` **without the network**.

    Every check here is a statement about the declaration, never about the repository: nothing
    is fetched or resolved, for the same reason a pip spec is not (see
    :func:`_missing_workspace_path`) -- a check that needs the network fails a campaign because
    of where it was validated from, and validation runs on machines a private clone URL is not
    reachable from.

    *where* is the field path the problems are reported under, so the same rules serve the
    config-file pass and the service-side pre-build check with one message each.

    Deliberately absent: any check that an entry is a *ROS* package. A repo is cloned into the
    workspace and colcon decides what it contains -- a plain CMake project with a ``colcon.pkg``
    and no ``package.xml`` is as buildable there as an ament package -- so a rosdistro or
    ``package.xml`` rule would refuse builds that work.
    """
    problems = []
    seen = {}
    for i, entry in enumerate(entries or []):
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        if not isinstance(entry, dict):
            problems.append(f"{where}[{i}]: expected a mapping with 'git' and 'ref'")
            continue
        url = str(entry.get("git") or "").strip()
        ref = str(entry.get("ref") or "").strip()
        if not url:
            problems.append(f"{where}[{i}]: no 'git'; every entry names the repository to clone")
        if not ref:
            problems.append(
                f"{where}[{i}]: no 'ref'. It is required and must pin -- a layer's cache key is "
                f"its command text, so a branch would be read once and served forever")
        elif not PINNED_REF.match(ref):
            problems.append(
                f"{where}[{i}]: ref '{ref}' looks like a branch, not a pin; give a commit sha "
                f"or a release tag")
        packages = entry.get("packages")
        if packages is not None:
            if not isinstance(packages, list) or not packages:
                problems.append(
                    f"{where}[{i}]: 'packages' must be a non-empty list of package names; omit "
                    f"it to build every package the repository contains")
            else:
                for name in packages:
                    if not isinstance(name, str) or not name.strip():
                        problems.append(
                            f"{where}[{i}]: a 'packages' entry is blank; expected a package name")
        if url:
            # Two repos with the same basename would be cloned into one `src/` directory: the
            # second clone fails on a non-empty path, deep in a build log, saying nothing about
            # the two lines of YAML that caused it.
            name = ros_repo_name(url)
            if name in seen and seen[name] != url:
                problems.append(
                    f"{where}[{i}]: '{url}' and '{seen[name]}' both clone into src/{name}; "
                    f"they cannot share one workspace directory")
            elif name in seen:
                problems.append(f"{where}[{i}]: '{url}' is declared twice")
            seen[name] = url
    return problems


def _ros_packages_problems(raw):
    """``ros_packages`` on every container of ``execution.containers``."""
    problems = []
    execution = raw.get("execution")
    if not isinstance(execution, dict):
        return problems
    containers = execution.get("containers")
    if not isinstance(containers, dict):
        return problems
    for name, block in containers.items():
        if not isinstance(block, dict) or not block.get("ros_packages"):
            continue
        field = f"execution.containers.{name}.ros_packages"
        for message in ros_packages_problems(block.get("ros_packages"), field):
            problems.append(_problem("build", message, field=field))
    return problems


def _missing_workspace_path(entry: str, vast_dir: str) -> bool:
    """Whether *entry* reads as a workspace path that is not in the project.

    Shared by ``build.python_packages`` and the top-level ``plugins:`` list: both are pip
    requirement specs where a *path* form is resolved against the project, so one rule and
    one message for both. Index pins and git URLs are not checked --- they are not
    resolvable offline, and guessing would fail a spec that is simply remote.
    """
    if not isinstance(entry, str) or not entry.strip():
        return False
    entry = entry.strip()
    p = os.path.abspath(os.path.join(vast_dir, entry))
    if os.path.isdir(p) or (entry.endswith(".whl") and os.path.isfile(p)):
        return False
    is_pip_url = ("git+" in entry or "://" in entry or " @ " in entry)
    return (entry.startswith((".", "/")) or entry.endswith(".whl")
            or ("/" in entry and not is_pip_url))


def _plugins_problems(raw, vast_dir):
    """Checks on the top-level ``plugins:`` list that cost nothing to run.

    Declared specs are installed during config *generation*, not by validation, so a spec
    is deliberately never resolved or fetched here. Two things can still be said without
    the network:

    * a **workspace path** entry that is not in the project --- the same check
      ``build.python_packages`` already gets, which top-level ``plugins:`` never had, so a
      wheel named with a typo surfaced only when a campaign tried to install it;
    * a plugin **already installed** in this workspace whose metadata declares a
      dependency on robovast itself. Harmless while the install resolves against the host,
      but it is what would otherwise put a second robovast in the workspace, and only the
      plugin's author can remove it.
    """
    problems = []
    specs = raw.get("plugins")
    if not isinstance(specs, list):
        return problems
    for entry in specs:
        if _missing_workspace_path(entry, vast_dir):
            problems.append(_problem(
                "plugins",
                f"'{entry}' looks like a workspace path but no such directory/wheel "
                "exists in the project",
                field="plugins"))
    try:
        from robovast.common.config_plugins import (  # pylint: disable=import-outside-toplevel
            host_dependent_plugins)
        for name, req in host_dependent_plugins(vast_dir).items():
            problems.append(_problem(
                "plugins",
                f"installed plugin '{name}' declares '{req}'. A plugin is loaded into "
                "robovast's process, not installed beside it, so the host always provides "
                "it; drop the dependency from the plugin's metadata",
                field="plugins"))
    except Exception:  # noqa: BLE001 - a metadata read must never fail validation
        logger.debug("could not check plugin host dependencies in %s", vast_dir)
    return problems


def validate_project_file(config_path):
    """Validate a ``.vast`` project file, collecting *all* problems at once.

    Args:
        config_path: Path to the ``.vast`` file.

    Returns:
        ``{valid, problems, configs, runs_per_config, total_trials}`` where
        ``problems`` is a list of ``{stage, config, field, message}``. When
        ``valid`` is True the counts mirror ``vast config info``.
    """
    raw, parse_problem = _safe_load(config_path)
    if parse_problem is not None:
        return {"valid": False, "problems": [parse_problem],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}

    problems = _schema_problems(raw)

    vast_dir = os.path.abspath(os.path.dirname(config_path))
    scenario_problems, scenario_file = _scenario_file_problems(raw, vast_dir)
    problems.extend(scenario_problems)

    valid_param_names = _scenario_parameter_names(scenario_file) if scenario_file else None

    for config in raw.get("configuration", []) or []:
        problems.extend(_config_block_problems(
            config, vast_dir, valid_param_names, raw.get("plugins") or ()))

    # Top-level plugin refs (postprocessing / search strategy / extractor).
    problems.extend(_plugin_ref_problems(raw, vast_dir))

    # Derived campaign inputs: the generator must resolve *in this process*, and its
    # 'out' must be a sane project-relative directory — both before compute is spent.
    problems.extend(_generator_problems(raw, vast_dir))

    # Custom run-view panel bundles must exist next to the .vast.
    problems.extend(_panel_problems(raw, vast_dir))

    # ...and so must every notebook the Explorer declares, or its tab cannot render.
    problems.extend(_explorer_notebook_problems(raw, vast_dir))

    # A campaign-scope 3D scene descriptor must be produced by a generator or matched by
    # a run_files pattern — otherwise the panel 404s only once someone opens the run.
    problems.extend(_scene_descriptor_problems(raw, vast_dir))
    problems.extend(_run_capture_problems(raw))
    problems.extend(_image_provenance_problems(raw))
    problems.extend(_unresolvable_image_problems(raw))
    problems.extend(_migration_marker_problems(raw))
    # ...and a camera panel needs a step that produces the video it plays.
    problems.extend(_camera_panel_problems(raw))

    # A build: section's workspace-path python_packages must exist (fail-fast at
    # submit, before any image build runs). Schema-level checks (tag shape, the
    # execution.image <-> build.tag consistency) are already covered by the config
    # model in _schema_problems.
    problems.extend(_build_problems(raw, vast_dir))
    # ...and a container's source-built ROS packages must be declared in a form a build could
    # act on: a repository, a ref that pins, and no two repos landing in one src/ directory.
    problems.extend(_ros_packages_problems(raw))
    problems.extend(_plugins_problems(raw, vast_dir))

    if problems:
        return {"valid": False, "problems": problems,
                "configs": 0, "runs_per_config": 0, "total_trials": 0}

    # No problems — compute the same counts as ``vast config info``.
    if raw.get("search"):
        return _search_composition_report(config_path)
    return _batch_composition_report(config_path)


def _message_with_next_step(exc):
    """The exception's message, with an :class:`ActionableError`'s next step kept.

    A problem is a flat ``{stage, config, field, message}``, so an exception folded into one
    with ``str(exc)`` loses any ``next_step`` riding on it -- and the refusals worth carrying
    one (a variation needing an auxiliary container no runner can provide) are exactly the
    ones where the message alone leaves the reader stuck. Appending keeps the reply shape
    unchanged, which is why it is done here rather than by widening the problem dict.
    """
    message = str(exc)
    step = getattr(exc, "next_step", "")
    return f"{message} Next: {step}" if step else message


def _batch_composition_report(config_path):
    """Compose a batch-mode ``.vast`` and report its counts.

    Composition stops at the first failure by design: a batch sweep is a
    deterministic cartesian expansion, so an infeasible cell means the sweep's
    own bounds are wrong — something to fix in the file, not to skip past.
    """
    from robovast.common.config_generation import \
        generate_scenario_variations  # pylint: disable=import-outside-toplevel
    from robovast.common.variation.base_variation import \
        VariationInfeasibleError  # pylint: disable=import-outside-toplevel
    try:
        campaign_data = generate_scenario_variations(
            variation_file=config_path, output_dir=None)
    except VariationInfeasibleError as e:
        # The message already names the config block and the plugin's reason; say
        # explicitly that the rest was never reached, so "one problem" is not read
        # as "one problem exists".
        return {"valid": False,
                "problems": [_problem(
                    "generation",
                    f"{e} — this parameter combination cannot be realized. "
                    "Composition stopped here, so any later configuration was "
                    "not checked.",
                    config=e.config_name)],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}
    except Exception as e:  # noqa: BLE001 - a check the linter missed; report it
        return {"valid": False,
                "problems": [_problem("generation", _message_with_next_step(e))],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}
    configs = campaign_data["configs"]
    runs_per_config = campaign_data.get("execution", {}).get("runs", 1)
    return {"valid": True,
            "problems": (_build_context_advisories(config_path)
                         + _resource_advisories(config_path)
                         + _calibration_role_advisories(config_path)
                         + _liveness_advisories(config_path)),
            "configs": len(configs), "runs_per_config": runs_per_config,
            "total_trials": len(configs) * runs_per_config}


def _search_composition_report(config_path):
    """Compose a sample of a search-mode ``.vast`` and report what it produced.

    A search ``.vast`` has no ``configuration:`` block to expand, so the batch
    path would report zero configs and call the file valid — hiding exactly the
    infeasible draws a pre-flight check exists to surface. Infeasible draws are
    reported as advisories rather than errors: unlike a batch sweep's fixed
    cells, a probabilistic draw failing is an expected property of the search
    space, and the campaign tolerates it (skipping that param set) rather than
    dying.
    """
    from robovast.search.compose import \
        preview_search_sample  # pylint: disable=import-outside-toplevel
    try:
        sample = preview_search_sample(config_path)
    except Exception as e:  # noqa: BLE001 - a check the linter missed; report it
        return {"valid": False,
                "problems": [_problem("generation", _message_with_next_step(e))],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}

    problems = []
    if sample["infeasible"]:
        listed = "; ".join(f"{item['name']} {item['params']}"
                           for item in sample["infeasible"])
        problems.append(_problem(
            "search-composition",
            f"{len(sample['infeasible'])} of {sample['distinct']} distinct parameter "
            f"set(s) could not be composed: {listed}. The campaign skips such draws "
            "and continues, but a high rate here means much of the search space is "
            "infeasible — check the search_space bounds against the variation's "
            "constraints.",
            field="search.search_space"))

    # Counts describe one composed batch, not the whole campaign: how many configs a
    # search ultimately evaluates depends on its budget and on how many draws turn out
    # infeasible, neither of which is knowable before it runs.
    configs = sample["composed"]
    runs_per_config = sample["runs_per_config"]
    return {"valid": True,
            "problems": (problems + _build_context_advisories(config_path)
                         + _resource_advisories(config_path)
                         + _calibration_role_advisories(config_path)
                         + _liveness_advisories(config_path)),
            "configs": configs, "runs_per_config": runs_per_config,
            "total_trials": configs * runs_per_config}
