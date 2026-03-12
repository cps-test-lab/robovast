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
from datetime import datetime, timedelta
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from robovast.common.campaign_data import (
    read_execution_metadata,
    read_sysinfo,
    read_test_result,
)
from robovast.common.common import load_config
from robovast.common.execution import is_campaign_dir
from robovast.common.results_utils import find_campaign_vast_file
from robovast.common.variation.loader import load_variation_classes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MetadataProcessor abstract base class
# ---------------------------------------------------------------------------

class MetadataProcessor(ABC):
    """Abstract base class for user-defined metadata processing plugins.

    Implementations are discovered via the ``robovast.metadata_processing``
    entry-point group and configured in the ``.vast`` file::

        results_processing:
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

            # Content of _config/config.yaml
            config_yaml_path = self.campaign_dir / config_name / "_config" / "config.yaml"
            if config_yaml_path.exists():
                with open(config_yaml_path, "r", encoding="utf-8") as f:
                    config_yaml_content = yaml.safe_load(f) or {}
                config_entry.update(config_yaml_content)

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

        # --- campaign-level postprocessing provenance ------------------
        pp_yaml_path = self.campaign_dir / "_transient" / "postprocessing.yaml"
        if pp_yaml_path.exists():
            with open(pp_yaml_path, "r", encoding="utf-8") as f:
                postprocessing = yaml.safe_load(f) or {}

            if isinstance(postprocessing.get("entries"), list):
                for entry in postprocessing["entries"]:
                    if isinstance(entry, dict):
                        if isinstance(entry.get("output"), str):
                            entry["output"] = entry["output"].removeprefix("../")
                        if isinstance(entry.get("sources"), list):
                            entry["sources"] = [
                                s.removeprefix("../") if isinstance(s, str) else s
                                for s in entry["sources"]
                            ]

            metadata["postprocessing"] = postprocessing
        else:
            metadata["postprocessing"] = {}

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

            # Transient files
            transient_dir = config_dir_path / "_transient"
            transient_files = []
            if transient_dir.exists() and transient_dir.is_dir():
                for file_path in transient_dir.rglob("*"):
                    if file_path.is_file():
                        relative_path = file_path.relative_to(self.campaign_dir)
                        transient_files.append(str(relative_path))
            transient_files.sort()
            config_entry["transient_files"] = transient_files

            config_entry["test_results"] = []
            for test_num in test_dirs:
                run_dir = self.campaign_dir / config_name / str(test_num)
                entry = {"dir": f"{config_name}/{test_num}"}

                # test.xml
                try:
                    result = read_test_result(run_dir)
                    entry["success"] = "true" if result["success"] else "false"
                    entry["start_time"] = result["start_time"]
                    if result["start_time"] and result["duration_sec"] is not None:
                        start_dt = datetime.fromisoformat(result["start_time"])
                        end_dt = start_dt + timedelta(seconds=result["duration_sec"])
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
                            "test.xml"
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

                # rosbag2 metadata
                rosbag2_meta_path = run_dir / "rosbag2" / "metadata.yaml"
                if rosbag2_meta_path.exists():
                    try:
                        with open(rosbag2_meta_path, "r", encoding="utf-8") as f:
                            bag_meta = yaml.safe_load(f) or {}
                        bag_info = bag_meta.get("rosbag2_bagfile_information", {})
                        ros_distro = bag_info.get("ros_distro", "")
                        message_types = sorted({
                            t["topic_metadata"]["type"]
                            for t in bag_info.get("topics_with_message_count", [])
                            if "topic_metadata" in t and "type" in t["topic_metadata"]
                        })
                        mcap_files = bag_info.get("relative_file_paths", [])
                        entry["rosbag2"] = {
                            "ros_distro": ros_distro,
                            "message_types": message_types,
                            "files": mcap_files,
                        }
                    except Exception as e:
                        logger.warning(
                            "Failed to read rosbag2 metadata %s: %s",
                            rosbag2_meta_path, e,
                        )

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
      2. Variation-plugin metadata hooks (``collect_config_metadata``)
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
        if d.is_dir() and is_campaign_dir(d.name)
    )
    if not campaign_dirs:
        return False, f"No campaign directories found in {results_dir}"

    # Load variation classes (for metadata hooks)
    variation_classes = load_variation_classes()

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
                        [os.path.join(config_name, "_config"), "_config"],
                        config_entry.get("config_files", []) + (metadata.get("run_files", [])),
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

            # Generate PROV-O provenance graph (metadata.prov.json)
            try:
                from .fair_metadata import generate_prov_metadata  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
                prov_success, prov_msg = generate_prov_metadata(
                    campaign_dir, metadata, generate_visualization=False
                )
                if prov_success:
                    output(prov_msg)
                else:
                    logger.warning("PROV metadata: %s", prov_msg)
            except Exception as prov_exc:  # noqa: BLE001
                logger.warning("PROV metadata generation failed: %s", prov_exc)

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
    real_paths: Union[str, List[str]],
    config_files: list,
) -> list:
    """Recursively find string values that reference a known config file.

    For each string value in *obj* (dict or list, searched recursively), if
    the value matches an entry in *config_files* when prefixed with any of the
    paths in *real_paths*, the string is replaced in-place with the first
    matching full relative path and the ``(key, path)`` pair is appended to
    the returned list.  When *real_paths* is empty or ``None``, the bare
    string value is compared directly against *config_files*.

    Args:
        obj: Dict or list to search (modified in-place).
        real_paths: A prefix path or list of prefix paths to try in order
            (e.g. ``["config-1/_config", "_config"]``).  Pass ``None`` or an
            empty list to compare bare values.
        config_files: List of known relative config-file paths under the
            campaign directory (e.g. ``["config-1/_config/params.yaml"]``).

    Returns:
        List of ``(key_or_index, resolved_path)`` tuples for every string
        that was resolved to a config file.
    """
    if real_paths is None:
        prefixes: List[str] = []
    elif isinstance(real_paths, str):
        prefixes = [real_paths]
    else:
        prefixes = list(real_paths)

    def _first_match(value: str) -> Optional[str]:
        """Return the first candidate that exists in config_files, or None."""
        if prefixes:
            for prefix in prefixes:
                candidate = os.path.join(prefix, value)
                print(f"Checking candidate config file path: {candidate} in {config_files}")
                if candidate in config_files:
                    print("FOUND MATCH:", candidate)
                    return candidate
        else:
            if value in config_files:
                return value
        return None

    found: list = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                resolved = _first_match(value)
                if resolved is not None:
                    obj[key] = resolved
                    found.append((key, resolved))
            else:
                found.extend(_resolve_file_strings(value, prefixes, config_files))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                resolved = _first_match(item)
                if resolved is not None:
                    obj[i] = resolved
                    found.append((i, resolved))
            else:
                found.extend(_resolve_file_strings(item, prefixes, config_files))
    return found



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
    """Read ````results_processing.metadata_processing```` from the vast file."""
    if vast_file is not None:
        vast_path = vast_file
    else:
        # Discover from most recent campaign
        vast_path, _config_dir = find_campaign_vast_file(results_dir)

    if vast_path is None:
        return []

    data_config = load_config(vast_path, subsection="results_processing", allow_missing=True)
    return data_config.get("metadata_processing", [])


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
