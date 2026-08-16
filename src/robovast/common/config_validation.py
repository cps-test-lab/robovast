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

The normal load/generate path is fail-fast — it raises on the first problem and,
for a YAML error, historically ``sys.exit(1)``. That is hostile to a program
(e.g. the MCP server) that validates configs an LLM produced: the LLM sees one
error at a time, or the server dies.

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
import re
from contextlib import contextmanager

import yaml
from pydantic import ValidationError

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

    problems = []
    version = raw.get("version")
    if version == 1:
        # Report what moved where, not just the version number. There is no migration
        # tool and no v1 reader, so this text is the migration path -- and this is the
        # collect-all validator, the surface an agent or the web editor hits first.
        from robovast.common.config import _V1_MIGRATION  # pylint: disable=import-outside-toplevel
        problems.append(_problem(
            "version",
            "config version 1 is no longer supported. Version 2 replaces "
            "execution.image, execution.resources, execution.secondary_containers and "
            "the top-level build: section with a single execution.containers "
            "mapping.\n\n" + _V1_MIGRATION + "\n\nThen set 'version: 2'.",
            field="version"))
    elif version != 2:
        problems.append(_problem(
            "version", f"Unsupported config version: {version!r} (expected 2).",
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


def _panel_entry(entry):
    """Extract ``(type, fields)`` from a raw ``visualization.panels`` entry, accepting the
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
    viz = raw.get("visualization") or {}
    if not isinstance(viz, dict):
        return problems
    patterns = ((raw.get("execution") or {}).get("run_files")) or []
    generated = _generated_out_dirs(raw)
    for i, entry in enumerate(viz.get("panels") or []):
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
        field = f"visualization.panels[{i}].scene.path"
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
    viz = raw.get("visualization") or {}
    if not isinstance(viz, dict):
        return problems
    panels = [(i, entry) for i, entry in enumerate(viz.get("panels") or [])
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
        field=f"visualization.panels[{i}]") for i, _entry in panels]


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
    viz = raw.get("visualization") or {}
    if not isinstance(viz, dict):
        return problems
    panels = [(i, props) for i, entry in enumerate(viz.get("panels") or [])
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
        field=f"visualization.panels[{i}]")
        for i, props in panels if not ((props or {}).get("source") or {}).get("path")]


def _vega_panel_problems(i, props):
    """The ``vega`` panel's bindings, as a collect-all check.

    Mirrors ``PanelConfig._vega_needs_bindings``; the duplication is the point — the schema
    raises on the first bad panel, this reports every one of them in a single report."""
    from robovast.common.config import \
        panel_source_problems  # pylint: disable=import-outside-toplevel

    problems = []
    prefix = f"visualization.panels[{i}]"
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
    """Validate ``visualization.panels`` beyond the schema: a ``custom`` panel's built
    bundle must exist next to the ``.vast``, and a ``vega`` panel carries the bindings it
    needs. Package panels (entry-point types) are only name-checked by the schema — their
    built assets ship with the plugin and may be absent in a source checkout, so they are
    not required here."""
    from robovast.common.config import (  # pylint: disable=import-outside-toplevel
        CUSTOM_PANEL_TYPE, VEGA_PANEL_TYPE)
    from robovast.common.config_generation import \
        _validate_relative_path  # pylint: disable=import-outside-toplevel

    problems = []
    viz = raw.get("visualization") or {}
    if not isinstance(viz, dict):
        return problems
    for i, entry in enumerate(viz.get("panels") or []):
        ptype, props = _panel_entry(entry)
        if ptype == VEGA_PANEL_TYPE:
            problems.extend(_vega_panel_problems(i, props if isinstance(props, dict) else {}))
            continue
        if ptype != CUSTOM_PANEL_TYPE:
            continue
        remote = props.get("remote") if isinstance(props, dict) else None
        if not remote:
            continue  # schema already flags a custom panel missing 'remote'
        field = f"visualization.panels[{i}].remote"
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


def _config_block_problems(config, vast_dir, valid_param_names):
    """Accumulate problems for a single configuration block."""
    from robovast.common.config import \
        get_validated_config  # pylint: disable=import-outside-toplevel
    from robovast.common.config_generation import \
        _get_variation_classes  # pylint: disable=import-outside-toplevel

    problems = []
    name = config.get("name", "<unnamed>") if isinstance(config, dict) else "<unnamed>"

    # Variation-type resolution (unknown type / invalid local plugin).
    variation_classes = []
    try:
        variation_classes = _get_variation_classes(config, vast_dir)
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
        p = os.path.abspath(os.path.join(vast_dir, entry))
        exists = os.path.isdir(p) or (entry.endswith(".whl") and os.path.isfile(p))
        if exists:
            continue
        is_pip_url = ("git+" in entry or "://" in entry or " @ " in entry)
        looks_like_path = (entry.startswith((".", "/")) or entry.endswith(".whl")
                           or ("/" in entry and not is_pip_url))
        if looks_like_path:
            problems.append(_problem(
                "build",
                f"'{entry}' looks like a workspace path but no such directory/wheel "
                "exists in the project",
                field="build.python_packages"))
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
        problems.extend(_config_block_problems(config, vast_dir, valid_param_names))

    # Top-level plugin refs (postprocessing / search strategy / extractor).
    problems.extend(_plugin_ref_problems(raw, vast_dir))

    # Derived campaign inputs: the generator must resolve *in this process*, and its
    # 'out' must be a sane project-relative directory — both before compute is spent.
    problems.extend(_generator_problems(raw, vast_dir))

    # Custom run-view panel bundles must exist next to the .vast.
    problems.extend(_panel_problems(raw, vast_dir))

    # A campaign-scope 3D scene descriptor must be produced by a generator or matched by
    # a run_files pattern — otherwise the panel 404s only once someone opens the run.
    problems.extend(_scene_descriptor_problems(raw, vast_dir))
    problems.extend(_run_capture_problems(raw))
    # ...and a camera panel needs a step that produces the video it plays.
    problems.extend(_camera_panel_problems(raw))

    # A build: section's workspace-path python_packages must exist (fail-fast at
    # submit, before any image build runs). Schema-level checks (tag shape, the
    # execution.image <-> build.tag consistency) are already covered by the config
    # model in _schema_problems.
    problems.extend(_build_problems(raw, vast_dir))

    if problems:
        return {"valid": False, "problems": problems,
                "configs": 0, "runs_per_config": 0, "total_trials": 0}

    # No problems — compute the same counts as ``vast config info``.
    if raw.get("search"):
        return _search_composition_report(config_path)
    return _batch_composition_report(config_path)


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
        campaign_data, _ = generate_scenario_variations(
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
                "problems": [_problem("generation", str(e))],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}
    configs = campaign_data["configs"]
    runs_per_config = campaign_data.get("execution", {}).get("runs", 1)
    return {"valid": True, "problems": [],
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
                "problems": [_problem("generation", str(e))],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}

    problems = []
    if sample["infeasible"]:
        listed = "; ".join(f"{item['name']} {item['params']}"
                           for item in sample["infeasible"])
        problems.append(_problem(
            "search-composition",
            f"{len(sample['infeasible'])} of {sample['sampled']} sampled parameter "
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
    return {"valid": True, "problems": problems,
            "configs": configs, "runs_per_config": runs_per_config,
            "total_trials": configs * runs_per_config}
