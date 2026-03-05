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

"""Generic metadata generation for campaign results.

This module provides:

- ``MetadataGenerator`` — collects structural metadata from a campaign directory
  (configurations, test results, execution info).
- ``MetadataProcessor`` — abstract base class for user-defined metadata
  processing plugins registered via the ``robovast.metadata_processing``
  entry-point group.
- ``generate_campaign_metadata`` — orchestrates the three-phase metadata
  pipeline (generic → variation hooks → user-defined processors) and writes
  ``metadata.yaml`` into each campaign directory.
"""

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .campaign_data import (read_execution_metadata, read_sysinfo,
                            read_test_result)
from .common import load_config
from .results_utils import find_campaign_vast_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MetadataProcessor abstract base class
# ---------------------------------------------------------------------------

class MetadataProcessor(ABC):
    """Abstract base class for user-defined metadata processing plugins.

    Implementations are discovered via the ``robovast.metadata_processing``
    entry-point group and configured in the ``.vast`` file::

        analysis:
          metadata_processing:
            - my_plugin
            - my_plugin:
                param1: value1

    Each plugin is instantiated with its parameters and called after the
    generic metadata and variation-plugin metadata have been collected.
    """

    def __init__(self, parameters: Optional[dict] = None):
        self.parameters = parameters or {}

    @abstractmethod
    def process_metadata(self, metadata: dict, campaign_dir: Path) -> dict:
        """Modify and return the campaign metadata dictionary.

        Args:
            metadata: The metadata dictionary built so far (generic +
                variation-plugin additions).
            campaign_dir: Path to the ``campaign-<id>`` directory.

        Returns:
            The (possibly modified) metadata dictionary.
        """


# ---------------------------------------------------------------------------
# MetadataGenerator — generic structural metadata
# ---------------------------------------------------------------------------

class MetadataGenerator:
    """Collects generic structural metadata from a campaign directory."""

    def __init__(self, campaign_dir: str | Path):
        self.campaign_dir = Path(campaign_dir)

    def generate_metadata(self) -> Dict[str, Any]:
        """Generate structural metadata for the campaign.

        Returns:
            Dictionary containing configurations, test results, execution
            metadata, run files, and scenario file reference.
        """
        metadata: Dict[str, Any] = {}

        # --- read configurations.yaml ----------------------------------
        config_path = self.campaign_dir / "_transient" / "configurations.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"configurations.yaml not found at {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Run files
        run_files = data.get("_run_files", [])
        metadata["run_files"] = [f"_config/{rf}" for rf in run_files]

        # Scenario file
        metadata["scenario_file"] = "scenario.osc"

        # User-provided metadata section (title, description, etc.)
        metadata["metadata"] = data.get("metadata", {})

        # Configurations
        metadata["configurations"] = data.get("configs", [])

        # --- per-config processing ------------------------------------
        for config_entry in metadata["configurations"]:
            config_name = config_entry["name"]

            # Validate and list config files attached by variations
            config_files_list = []
            for key, _value in config_entry.get("_config_files", []):
                config_file_name = os.path.join(config_name, "_config", key)
                config_file_path = self.campaign_dir / config_file_name
                if not os.path.exists(config_file_path):
                    raise FileNotFoundError(f"Config file not found: {config_file_name}")
                config_files_list.append(config_file_name)
            config_entry["config_files"] = config_files_list

            # Timestamp
            config_entry["created_at"] = data.get("created_at")

        # Strip internal fields (keys starting with "_"), preserving
        # _variations for the metadata hooks phase.
        for config_entry in metadata["configurations"]:
            keys_to_remove = [
                k for k in config_entry
                if k.startswith("_") and k != "_variations"
            ]
            for k in keys_to_remove:
                config_entry.pop(k)

        # --- execution metadata ----------------------------------------
        metadata["execution"] = read_execution_metadata(self.campaign_dir)

        # --- test results per config -----------------------------------
        expected_runs = metadata["execution"].get("runs")
        for config_entry in metadata["configurations"]:
            config_name = config_entry.get("name", "")
            config_dir_path = self.campaign_dir / config_name

            # Discover run directories
            test_dirs = []
            if config_dir_path.exists() and config_dir_path.is_dir():
                for item in config_dir_path.iterdir():
                    if item.is_dir() and item.name.isdigit():
                        test_dirs.append(int(item.name))
            test_dirs.sort()

            if expected_runs is not None and len(test_dirs) != expected_runs:
                raise ValueError(
                    f"Config '{config_name}' has {len(test_dirs)} run directories "
                    f"but expected {expected_runs} runs"
                )

            config_entry["test_results"] = []
            for test_num in test_dirs:
                run_dir = self.campaign_dir / config_name / str(test_num)
                entry = {"dir": f"{config_name}/{test_num}"}

                # test.xml
                try:
                    result = read_test_result(run_dir)
                    entry["success"] = "true" if result["passed"] else "false"
                    entry["start_time"] = result["start_time"]
                    if result["start_time"] and result["duration_sec"] is not None:
                        start_dt = datetime.fromisoformat(result["start_time"])
                        end_dt = datetime.fromtimestamp(
                            start_dt.timestamp() + result["duration_sec"]
                        )
                        entry["end_time"] = end_dt.isoformat()
                except Exception as e:
                    raise ValueError(
                        f"Failed to parse test.xml in {run_dir}: {e}"
                    ) from e

                # Output files
                output_files = []
                if run_dir.exists() and run_dir.is_dir():
                    for file_path in run_dir.rglob("*"):
                        if file_path.is_file() and file_path.name not in (
                            "test.xml", "postprocessing.yaml"
                        ):
                            relative_path = file_path.relative_to(
                                run_dir.parent.parent
                            )
                            output_files.append(str(relative_path))
                output_files.sort()
                entry["output_files"] = output_files

                # sysinfo
                try:
                    entry["sysinfo"] = read_sysinfo(run_dir)
                except FileNotFoundError as exc:
                    raise FileNotFoundError(
                        f"sysinfo.yaml not found in {run_dir}"
                    ) from exc

                # postprocessing.yaml
                pp_path = run_dir / "postprocessing.yaml"
                if pp_path.exists():
                    with open(pp_path, "r", encoding="utf-8") as f:
                        entry["postprocessing"] = yaml.safe_load(f)
                else:
                    entry["postprocessing"] = {}

                config_entry["test_results"].append(entry)

        return metadata


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def generate_campaign_metadata(
    results_dir: str,
    vast_file: Optional[str] = None,
    output_callback=None,
) -> tuple[bool, str]:
    """Run the full metadata generation pipeline for all campaigns.

    Pipeline phases:
      1. Generic structural metadata (``MetadataGenerator``)
      2. Variation-plugin metadata hooks (``collect_config_metadata`` /
         ``collect_run_metadata``)
      3. User-defined ``MetadataProcessor`` plugins

    Writes ``metadata.yaml`` into each campaign directory.

    Args:
        results_dir: Path to the results directory containing
            ``campaign-<id>`` subdirectories.
        vast_file: Optional explicit ``.vast`` file path.  When ``None``,
            the ``.vast`` file is discovered from the most recent campaign.
        output_callback: Optional callable for status messages.

    Returns:
        Tuple ``(success, message)``.
    """
    def output(msg):
        if output_callback:
            output_callback(msg)
        else:
            logger.info(msg)

    results_path = Path(results_dir)
    if not results_path.is_dir():
        return False, f"Results directory does not exist: {results_dir}"

    # Find campaign directories
    campaign_dirs = sorted(
        d for d in results_path.iterdir()
        if d.is_dir() and d.name.startswith("campaign-")
    )
    if not campaign_dirs:
        return False, f"No campaign directories found in {results_dir}"

    # Load variation classes (for metadata hooks)
    variation_classes = _load_variation_classes()

    # Load metadata processing plugins
    metadata_plugins = _load_metadata_plugins()

    # Discover metadata_processing commands from vast file
    metadata_processing_commands = _get_metadata_processing_commands(
        results_dir, vast_file
    )

    try:
        for campaign_dir in campaign_dirs:
            output(f"Generating metadata for {campaign_dir.name}...")

            # Phase 1: Generic metadata
            generator = MetadataGenerator(campaign_dir)
            metadata = generator.generate_metadata()

            output_path = campaign_dir / "tmp.yaml"
            with open(output_path, "w", encoding="utf-8") as f:
                yaml.dump(
                    metadata, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )

            # Phase 2: Variation plugin metadata hooks
            _apply_variation_metadata(metadata, campaign_dir, variation_classes)

            # Resolve config-file references inside each "config" block now that
            # variation hooks have had a chance to run first.
            for config_entry in metadata.get("configurations", []):
                config_name = config_entry.get("name", "")
                if "config" in config_entry and isinstance(config_entry["config"], dict):
                    _replace_file_urls(config_entry["config"])

                    _resolve_file_strings(
                        config_entry["config"],
                        os.path.join(config_name, "_config"),
                        config_entry.get("config_files", []),
                    )

            # Phase 3: User-defined metadata processors
            _apply_user_metadata_processors(
                metadata, campaign_dir, metadata_processing_commands, metadata_plugins
            )

            # Write metadata.yaml
            output_path = campaign_dir / "metadata.yaml"
            with open(output_path, "w", encoding="utf-8") as f:
                yaml.dump(
                    metadata, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            output(f"Wrote {output_path}")

    except Exception as e:
        return False, f"Metadata generation failed: {e}"

    return True, f"Metadata generated for {len(campaign_dirs)} campaign(s)"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _replace_file_urls(obj):
    """Recursively replace ``file:///config`` with ``_config`` in strings."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            obj[key] = _replace_file_urls(value)
    elif isinstance(obj, list):
        return [_replace_file_urls(item) for item in obj]
    elif isinstance(obj, str):
        return obj.replace("file:///config", "_config")
    return obj


def _resolve_file_strings(
    obj: Any,
    real_path: str,
    config_files: list,
) -> list:
    """Recursively find string values that reference a known config file.

    For each string value in *obj* (dict or list, searched recursively), if
    the value matches an entry in *config_files* when prefixed with
    ``<config_name>/_config/``, the string is replaced in-place with that
    full relative path and the ``(key, path)`` pair is appended to the
    returned list.

    Args:
        obj: Dict or list to search (modified in-place).
        config_name: Name of the configuration (e.g. ``"config-1"``).
        config_files: List of known relative config-file paths under the
            campaign directory (e.g. ``["config-1/_config/params.yaml"]``).

    Returns:
        List of ``(key_or_index, resolved_path)`` tuples for every string
        that was resolved to a config file.
    """
    found: list = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                if real_path is not None:
                    candidate = os.path.join(real_path, value)
                else:
                    candidate = value
                if candidate in config_files:
                    obj[key] = candidate
                    found.append((key, candidate))
            else:
                found.extend(_resolve_file_strings(value, real_path, config_files))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                candidate = os.path.join(real_path, item)
                if candidate in config_files:
                    obj[i] = candidate
                    found.append((i, candidate))
            else:
                found.extend(_resolve_file_strings(item, real_path, config_files))
    return found


def _load_variation_classes() -> Dict[str, type]:
    """Load variation classes from the ``robovast.variation_types`` entry-point group."""
    classes = {}
    try:
        eps = entry_points(group="robovast.variation_types")
        for ep in eps:
            try:
                classes[ep.name] = ep.load()
            except Exception as e:
                logger.warning("Failed to load variation class '%s': %s", ep.name, e)
    except Exception:
        pass
    return classes


def _load_metadata_plugins() -> Dict[str, type]:
    """Load metadata processor classes from the ``robovast.metadata_processing`` entry-point group."""
    plugins = {}
    try:
        eps = entry_points(group="robovast.metadata_processing")
        for ep in eps:
            try:
                plugins[ep.name] = ep.load()
            except Exception as e:
                logger.warning("Failed to load metadata plugin '%s': %s", ep.name, e)
    except Exception:
        pass
    return plugins


def _get_metadata_processing_commands(
    results_dir: str,
    vast_file: Optional[str],
) -> List:
    """Read ``analysis.metadata_processing`` from the vast file."""
    if vast_file is not None:
        vast_path = vast_file
    else:
        # Discover from most recent campaign
        vast_path, _config_dir = find_campaign_vast_file(results_dir)

    if vast_path is None:
        return []

    analysis_config = load_config(vast_path, subsection="analysis", allow_missing=True)
    return analysis_config.get("metadata_processing", [])


def _apply_variation_metadata(
    metadata: dict,
    campaign_dir: Path,
    variation_classes: Dict[str, type],
) -> None:
    """Call variation-plugin metadata hooks and merge results."""
    for config_entry in metadata.get("configurations", []):
        config_name = config_entry.get("name", "")
        config_dir = campaign_dir / config_name

        # Read and consume _variations, expose as public "variations"
        variation_data = config_entry.pop("_variations", [])
        config_entry["variations"] = variation_data

        for vdata in variation_data:
            vtype_name = vdata["name"]
            cls = variation_classes.get(vtype_name)
            if cls is None:
                continue

            # Config-level metadata
            if hasattr(cls, "collect_config_metadata"):
                try:
                    extra = cls.collect_config_metadata(config_entry, config_dir, campaign_dir)
                    if extra and isinstance(extra, dict):
                        config_entry.update(extra)
                except Exception as e:
                    logger.warning(
                        "Variation '%s' collect_config_metadata failed for '%s': %s",
                        vtype_name, config_name, e,
                    )

            # Run-level metadata
            if hasattr(cls, "collect_run_metadata"):
                for test_result in config_entry.get("test_results", []):
                    run_dir = campaign_dir / test_result.get("dir", "")
                    try:
                        extra = cls.collect_run_metadata(config_entry, run_dir, campaign_dir)
                        if extra and isinstance(extra, dict):
                            test_result.update(extra)
                    except Exception as e:
                        logger.warning(
                            "Variation '%s' collect_run_metadata failed for '%s': %s",
                            vtype_name, test_result.get("dir", ""), e,
                        )


def _apply_user_metadata_processors(
    metadata: dict,
    campaign_dir: Path,
    commands: List,
    plugins: Dict[str, type],
) -> None:
    """Instantiate and run user-defined metadata processors."""
    for command in commands:
        if isinstance(command, str):
            plugin_name = command
            params = {}
        elif isinstance(command, dict) and len(command) == 1:
            plugin_name = list(command.keys())[0]
            params = command[plugin_name] or {}
        else:
            logger.warning("Invalid metadata_processing command: %s", command)
            continue

        plugin_cls = plugins.get(plugin_name)
        if plugin_cls is None:
            available = ", ".join(sorted(plugins.keys())) or "none"
            raise ValueError(
                f"Unknown metadata processing plugin: '{plugin_name}'. "
                f"Available: {available}"
            )

        processor = plugin_cls(parameters=params)
        metadata = processor.process_metadata(metadata, campaign_dir)
