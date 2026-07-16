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
"""

import logging
import os

import yaml
from pydantic import ValidationError

logger = logging.getLogger(__name__)


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
    if version != 1:
        problems.append(_problem(
            "version", f"Unsupported config version: {version!r} (expected 1).",
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

    if problems:
        return {"valid": False, "problems": problems,
                "configs": 0, "runs_per_config": 0, "total_trials": 0}

    # No problems — compute the same counts as ``vast config info``.
    from robovast.common.config_generation import \
        generate_scenario_variations  # pylint: disable=import-outside-toplevel
    try:
        campaign_data, _ = generate_scenario_variations(
            variation_file=config_path, output_dir=None)
    except Exception as e:  # noqa: BLE001 - a check the linter missed; report it
        return {"valid": False,
                "problems": [_problem("generation", str(e))],
                "configs": 0, "runs_per_config": 0, "total_trials": 0}
    configs = campaign_data["configs"]
    runs_per_config = campaign_data.get("execution", {}).get("runs", 1)
    return {"valid": True, "problems": [],
            "configs": len(configs), "runs_per_config": runs_per_config,
            "total_trials": len(configs) * runs_per_config}
