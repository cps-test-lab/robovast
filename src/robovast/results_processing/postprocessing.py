# Copyright (C) 2025 Frederik Pasch
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

"""Postprocessing functionality for run result data."""
import inspect
import json
import os
import tempfile
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from robovast.common.common import load_config
from robovast.results_processing.metadata import generate_campaign_metadata
from robovast.results_processing.postprocessing_plugins import generate_data_db
from robovast.common.results_utils import find_campaign_vast_file


def load_postprocessing_plugins() -> Dict[str, callable]:
    """Load postprocessing command plugins from entry points.

    All plugins must be classes that inherit from
    :class:`~robovast.results_processing.postprocessing_plugins.BasePostprocessingPlugin`.
    Class-based plugins are automatically instantiated so that callers always
    receive a ready-to-use callable.  Class instances additionally expose
    :meth:`~robovast.results_processing.postprocessing_plugins.BasePostprocessingPlugin.get_files_to_copy`
    which is used during config preparation to copy required files into
    ``_config/``.

    Returns:
        Dictionary mapping plugin names to their callable objects (class instances).
    """
    plugins = {}
    try:
        eps = entry_points(group='robovast.postprocessing_commands')
        for ep in eps:
            try:
                # Load the entry point - must be a class
                plugin_obj = ep.load()
                if not inspect.isclass(plugin_obj):
                    print(f"Warning: Postprocessing plugin '{ep.name}' is not a class and will be skipped. "
                          f"All plugins must be classes inheriting from BasePostprocessingPlugin.")
                    continue
                # Instantiate class-based plugins so callers get a consistent
                # callable interface and can also access get_files_to_copy.
                plugin_obj = plugin_obj()
                plugins[ep.name] = plugin_obj
            except Exception as e:
                # Log and continue if a plugin fails to load
                print(f"Warning: Failed to load postprocessing plugin '{ep.name}': {e}")
    except Exception:
        # No plugins available or entry_points call failed
        pass
    return plugins


def execute_postprocessing_plugin(
    plugin_name: str,
    plugin_func: callable,
    params: dict,
    results_dir: str,
    config_dir: str,
    provenance_file: Optional[str] = None,
    execution_image: Optional[str] = None,
    debug: bool = False,
    force: bool = False,
) -> Tuple[bool, str, List[dict]]:
    """Execute a postprocessing plugin with parameters.

    Args:
        plugin_name: Name of the plugin
        plugin_func: The plugin function to call
        params: Dictionary of parameters for the plugin
        results_dir: Path to the campaign-<id> directory
        config_dir: Directory containing the configuration file
        provenance_file: Optional path for container plugins to write provenance JSON
        execution_image: Optional Docker image from the execution phase

    Returns:
        Tuple of (success, message, provenance_entries)
    """
    kwargs = {
        'results_dir': results_dir,
        'config_dir': config_dir,
        **params,
    }
    if provenance_file is not None:
        kwargs['provenance_file'] = provenance_file
    if execution_image is not None:
        kwargs['execution_image'] = execution_image
    if debug:
        kwargs['debug'] = debug
    if force:
        kwargs['force'] = force

    try:
        result = plugin_func(**kwargs)
        if isinstance(result, (list, tuple)) and len(result) >= 3:
            success, message, entries = result[0], result[1], result[2]
            return success, message, entries if isinstance(entries, list) else []
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            success, message = result[0], result[1]
        else:
            success, message = result
        # Collect provenance from container-written file if present
        entries = []
        if provenance_file and os.path.isfile(provenance_file):
            try:
                with open(provenance_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    entries = data.get('entries', [])
            except (json.JSONDecodeError, OSError):
                pass
        return success, message, entries
    except TypeError as e:
        return False, f"Plugin '{plugin_name}' argument error: {e}", []
    except Exception as e:
        return False, f"Plugin '{plugin_name}' execution error: {e}", []


# Plugin names that can be transparently batched into a single rosbags_process call.
# Maps plugin name → (handler_type, default_bag_dir).
_ROSBAG_BATCH_MAP: Dict[str, Tuple[str, str]] = {
    "rosbags_to_csv":        ("to_csv",         "rosbag2"),
    "rosbags_tf_to_csv":     ("tf_to_csv",       "rosbag2"),
    "rosbags_bt_to_csv":     ("bt_to_csv",       "rosbag2"),
    "rosbags_action_to_csv": ("action_to_csv",   "rosbag2"),
    "rosbags_rosout_to_csv": ("rosout_to_csv",   "logs/rosout_bag"),
}


def _batch_rosbags_commands(commands: List, skip_rosout: bool = False) -> List:
    """Replace all batchable rosbags_* plugin calls with rosbags_process calls.

    Groups every command whose plugin name appears in ``_ROSBAG_BATCH_MAP`` by
    their ``bag_dir`` (the subdirectory name to search for rosbags).  One
    ``rosbags_process`` command is emitted per distinct ``bag_dir``.  Each batch
    is inserted at the position of the first batchable command sharing that
    ``bag_dir``; all other batchable commands are removed.  Non-batchable
    commands keep their original order.

    ``rosout_to_csv`` is always added unless *skip_rosout* is ``True``.

    Args:
        commands: Raw list of postprocessing commands from the .vast config.
        skip_rosout: When ``True``, omit rosout processing entirely (neither
            auto-injected nor taken from explicit ``rosbags_rosout_to_csv``
            commands in the config).

    Returns:
        New command list with batchable commands replaced by rosbags_process calls.
    """
    # bag_dir → list of handler dicts for that bag dir
    bag_dir_plugins: Dict[str, List[dict]] = {}
    # bag_dir → index in result where the placeholder lives
    bag_dir_slot: Dict[str, int] = {}
    result: List = []

    rosout_bag_dir = _ROSBAG_BATCH_MAP["rosbags_rosout_to_csv"][1]

    for cmd in commands:
        plugin_name = cmd if isinstance(cmd, str) else list(cmd.keys())[0]
        if plugin_name in _ROSBAG_BATCH_MAP:
            handler_type, default_bag_dir = _ROSBAG_BATCH_MAP[plugin_name]
            # Skip rosout if requested
            if skip_rosout and handler_type == "rosout_to_csv":
                continue
            params = {} if isinstance(cmd, str) else (cmd[plugin_name] or {})
            # Allow per-command bag_dir override; pop it so it's not passed to handler
            params = dict(params)
            bag_dir = params.pop("bag_dir", default_bag_dir)
            bag_dir_plugins.setdefault(bag_dir, []).append({"type": handler_type, **params})
            if bag_dir not in bag_dir_slot:
                bag_dir_slot[bag_dir] = len(result)
                result.append(None)  # reserve slot
        else:
            result.append(cmd)

    # Always include rosout unless explicitly skipped
    if not skip_rosout and not any(
        p.get("type") == "rosout_to_csv"
        for plugins in bag_dir_plugins.values()
        for p in plugins
    ):
        bag_dir_plugins.setdefault(rosout_bag_dir, []).append({"type": "rosout_to_csv"})
        if rosout_bag_dir not in bag_dir_slot:
            bag_dir_slot[rosout_bag_dir] = len(result)
            result.append(None)

    # Fill placeholder slots with the batch commands
    for bag_dir, slot_idx in bag_dir_slot.items():
        plugins = bag_dir_plugins[bag_dir]
        result[slot_idx] = {"rosbags_process": {"plugins": plugins, "bag_dir": bag_dir}}

    return result


def validate_postprocessing_command(command: str | dict, plugins: Dict[str, callable]) -> tuple[bool, str]:
    """Validate a postprocessing command.

    Args:
        command: Command as string (simple name) or dict (name as key with parameters)
        plugins: Dictionary of available plugins

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Parse command to get plugin name
    if isinstance(command, str):
        plugin_name = command
    elif isinstance(command, dict):
        if len(command) != 1:
            return False, f"Postprocessing command dict must have exactly one key (the plugin name), got {len(command)}"
        plugin_name = list(command.keys())[0]
    else:
        return False, f"Postprocessing command must be a string or dict, got {type(command)}"

    if plugin_name not in plugins:
        available = ', '.join(sorted(plugins.keys()))
        return False, (
            f"Unknown postprocessing plugin: '{plugin_name}'. "
            f"Available plugins: {available if available else 'none'}. "
            f"Use 'vast results postprocess-commands' to list all plugins."
        )

    return True, ""


def get_postprocessing_commands(config_path: str) -> List[dict]:
    """Get postprocessing commands from a .vast configuration file.

    Args:
        config_path: Path to .vast configuration file

    Returns:
        List of postprocessing commands (dicts) or empty list if none defined
    """
    data_config = load_config(config_path, subsection="results_processing", allow_missing=True)
    if data_config is None:
        return []
    else:
        postprocessing_cmds = data_config.get("postprocessing", [])
        if postprocessing_cmds is None:
            return []
        else:
            return postprocessing_cmds


def _write_postprocessing_provenance_yaml(
    campaign_dir: str,
    entries: List[dict],
) -> None:
    """Write postprocessing.yaml under campaign-<id>/_transient/ with all provenance entries.

    Args:
        campaign_dir: Path to the campaign-<id> directory.
        entries: List of provenance entry dicts.
    """
    transient_dir = Path(campaign_dir) / "_transient"
    try:
        transient_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    yaml_path = transient_dir / "postprocessing.yaml"

    # Paths in entries are relative to results_dir (parent of campaign_dir).
    # Rewrite them to be relative to transient_dir so the yaml is self-contained.
    results_dir_path = Path(campaign_dir).parent

    def _rel_to_transient(p: str) -> str:
        if not p:
            return p
        try:
            return str(Path(os.path.relpath(results_dir_path / p, transient_dir)))
        except (ValueError, TypeError):
            return p

    relative_entries = []
    for ent in entries:
        relative_entries.append({
            "output": _rel_to_transient(ent.get("output") or ""),
            "sources": [_rel_to_transient(s) for s in (ent.get("sources") or [])],
            "plugin": ent.get("plugin", ""),
            "params": ent.get("params") or {},
        })

    data: dict = {
        "generated_by": "robovast",
        "entries": relative_entries,
    }
    try:
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
    except OSError:
        pass  # skip if we cannot write



def is_postprocessing_needed(
        results_dir: str,
        vast_file: Optional[str] = None,
) -> bool:
    """Check whether postprocessing needs to run for *results_dir*.

    Returns ``True`` when postprocessing commands are configured; per-rosbag
    caching inside ``rosbags_process`` handles skipping already-processed bags.

    Returns ``False`` when no postprocessing commands are configured or the
    results directory / vast file cannot be found.

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs).
        vast_file: Optional explicit path to a ``.vast`` file.

    Returns:
        ``True`` if postprocessing should be run, ``False`` otherwise.
    """
    if not os.path.exists(results_dir):
        return False

    if vast_file is not None:
        if not os.path.isfile(vast_file):
            return False
        vast_path = os.path.abspath(vast_file)
    else:
        vast_path, _ = find_campaign_vast_file(results_dir)
        if vast_path is None:
            return False

    commands = get_postprocessing_commands(vast_path)
    return bool(commands)


def run_postprocessing(  # pylint: disable=too-many-return-statements
        results_dir: str,
        output_callback=None,
        force: bool = False,
        vast_file: Optional[str] = None,
        debug: bool = False,
        skip_rosout: bool = False,
        skip: Optional[List[str]] = None,
):
    """Run postprocessing commands on run results.

    The postprocessing configuration is read from the ``.vast`` file found in
    the most recent ``campaign-<id>/_config/`` directory under *results_dir*,
    unless *vast_file* is provided explicitly.

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs)
        output_callback: Optional callback function for output messages (takes message string)
        force: If True, bypass per-rosbag caches and reprocess all bags.
        vast_file: Optional explicit path to a ``.vast`` file.  When given, the
            campaign copy is ignored entirely.
        debug: If True, include full plugin stdout in output; otherwise show only the summary line.
        skip_rosout: If True, skip rosout processing entirely (shorthand for ``skip=['rosbags_rosout_to_csv']``).
        skip: List of plugin names to skip entirely (e.g. ``['rosbags_to_webm']``).

    Returns:
        Tuple of (success: bool, message: str)
    """
    def output(msg):
        """Helper to call output callback or print."""
        if output_callback:
            output_callback(msg)
        else:
            print(msg)

    # Validate results directory
    if not os.path.exists(results_dir):
        return False, f"Results directory does not exist: {results_dir}"

    if vast_file is not None:
        # Explicit override: validate the supplied path and skip campaign discovery
        if not os.path.isfile(vast_file):
            return False, f"Override .vast file does not exist: {vast_file}"
        vast_path = os.path.abspath(vast_file)
        config_dir = os.path.dirname(vast_path)
        output(f"Using override config: {vast_path}")
    else:
        # Discover the .vast config file from the most recent campaign
        vast_path, config_dir = find_campaign_vast_file(results_dir)
        if vast_path is None:
            return False, (
                f"No .vast file found in any campaign-*/_config/ directory under: {results_dir}\n"
                "Ensure at least one execution campaign has been completed."
            )
        output(f"Using config from: {vast_path}")

    campaign_dir = str(Path(config_dir).parent)

    # Read execution image from execution.yaml (if available)
    execution_image = None
    execution_yaml_path = os.path.join(campaign_dir, "_execution", "execution.yaml")
    if os.path.isfile(execution_yaml_path):
        try:
            with open(execution_yaml_path, 'r', encoding='utf-8') as f:
                exec_data = yaml.safe_load(f) or {}
            execution_image = exec_data.get("image")
            if execution_image:
                output(f"Using execution image for postprocessing: {execution_image}")
        except (yaml.YAMLError, OSError):
            pass

    # Get postprocessing commands
    commands = get_postprocessing_commands(vast_path)

    if force:
        output("Force mode: per-rosbag caches will be ignored")

    # Build unified skip set
    skip_set: set = set(skip) if skip else set()
    if skip_rosout:
        skip_set.add("rosbags_rosout_to_csv")

    # Filter out explicitly skipped plugins before batching
    if skip_set:
        filtered = []
        for cmd in commands:
            name = cmd if isinstance(cmd, str) else list(cmd.keys())[0]
            if name in skip_set:
                output(f"Skipping: {name}")
            else:
                filtered.append(cmd)
        commands = filtered

    # Load plugins
    plugins = load_postprocessing_plugins()

    # Batch all batchable rosbags_* commands into a single rosbags_process call
    # (reads each rosbag once instead of once per plugin). rosout_to_csv is always
    # included unless skipped.
    commands = _batch_rosbags_commands(commands, skip_rosout="rosbags_rosout_to_csv" in skip_set)

    # Validate all commands first
    for command in commands:
        is_valid, error_msg = validate_postprocessing_command(command, plugins)
        if not is_valid:
            return False, error_msg

    all_provenance_entries: List[dict] = []

    with tempfile.TemporaryDirectory(prefix="robovast_provenance_") as temp_dir:
        # Execute each postprocessing command
        success = True

        for i, command in enumerate(commands, 1):
            # Parse command to get plugin name and parameters
            if isinstance(command, str):
                plugin_name = command
                params = {}
            elif isinstance(command, dict):
                if len(command) != 1:
                    output(f"[{i}/{len(commands)}] ✗ Invalid command format: dict must have exactly one key")
                    success = False
                    continue
                plugin_name = list(command.keys())[0]
                params = command[plugin_name] or {}
                if not isinstance(params, dict):
                    output(f"[{i}/{len(commands)}] ✗ Invalid command format: parameters must be a dict")
                    success = False
                    continue
            else:
                output(f"[{i}/{len(commands)}] ✗ Invalid command format: must be string or dict, got {type(command)}")
                success = False
                continue

            display_cmd = f"{plugin_name} (params: {params})" if params else plugin_name

            plugin_func = plugins[plugin_name]

            output(f"[{i}/{len(commands)}] Executing: {display_cmd}")

            provenance_file = os.path.join(temp_dir, f"{plugin_name}_provenance.json")

            plugin_success, message, entries = execute_postprocessing_plugin(
                plugin_name=plugin_name,
                plugin_func=plugin_func,
                params=params,
                results_dir=results_dir,
                config_dir=config_dir,
                provenance_file=provenance_file,
                execution_image=execution_image,
                debug=debug,
                force=force,
            )

            all_provenance_entries.extend(entries)

            if not plugin_success:
                output(f"✗ {message}")
                success = False
                continue
            display_message = message if debug else message.splitlines()[0]
            output(f"✓ {display_message}")

    # Write postprocessing.yaml in campaign/_transient/
    _write_postprocessing_provenance_yaml(campaign_dir, all_provenance_entries)

    # Build SQLite data.db for the campaign
    db_success, db_msg = generate_data_db(campaign_dir, output_callback=output_callback)
    if db_success:
        output(f"✓ {db_msg}")
    else:
        raise RuntimeError(f"data.db generation failed: {db_msg}")

    # Generate metadata.yaml in each campaign directory
    meta_success, meta_msg = generate_campaign_metadata(
        results_dir, vast_file=vast_file, output_callback=output_callback,
    )
    if not meta_success:
        output(f"Warning: Metadata generation failed: {meta_msg}")

    if success:
        return True, "Postprocessing completed successfully!"
    return False, "Postprocessing failed!"
