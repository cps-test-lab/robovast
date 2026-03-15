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
import hashlib
import inspect
import json
import os
import tempfile
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from robovast.common.common import load_config
from robovast.common.execution import is_campaign_dir
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
# Maps plugin name → handler type string used in rosbags_process.py config.
_ROSBAG_BATCH_MAP: Dict[str, str] = {
    "rosbags_to_csv":       "to_csv",
    "rosbags_tf_to_csv":    "tf_to_csv",
    "rosbags_bt_to_csv":    "bt_to_csv",
    "rosbags_action_to_csv": "action_to_csv",
    "rosbags_rosout_to_csv": "rosout_to_csv",
}


def _batch_rosbags_commands(commands: List) -> List:
    """Replace all batchable rosbags_* plugin calls with a single rosbags_process call.

    Reads the command list and groups every command whose plugin name appears in
    ``_ROSBAG_BATCH_MAP`` into a single ``rosbags_process`` command.  The batch
    command is inserted at the position of the first batchable command found;
    all other batchable commands are removed.  Non-batchable commands keep their
    original order relative to the batch insertion point.

    ``rosout_to_csv`` is always included in the batch (with default parameters if
    not explicitly configured) so that the forced separate rosout run is no longer
    needed.

    Args:
        commands: Raw list of postprocessing commands from the .vast config.

    Returns:
        New command list with batchable commands replaced by one rosbags_process call.
    """
    batch_plugins: List[dict] = []
    result: List = []
    batch_placeholder_inserted = False

    for cmd in commands:
        plugin_name = cmd if isinstance(cmd, str) else list(cmd.keys())[0]
        if plugin_name in _ROSBAG_BATCH_MAP:
            params = {} if isinstance(cmd, str) else (cmd[plugin_name] or {})
            batch_plugins.append({"type": _ROSBAG_BATCH_MAP[plugin_name], **params})
            if not batch_placeholder_inserted:
                result.append(None)  # reserve slot at position of first batchable command
                batch_placeholder_inserted = True
        else:
            result.append(cmd)

    # Always include rosout (with defaults) so the forced separate rosout run is not needed
    if not any(p.get("type") == "rosout_to_csv" for p in batch_plugins):
        batch_plugins.append({"type": "rosout_to_csv"})

    batch_cmd: dict = {"rosbags_process": {"plugins": batch_plugins}}

    if batch_placeholder_inserted:
        for i, item in enumerate(result):
            if item is None:
                result[i] = batch_cmd
                break
    else:
        # No batchable commands were in the config — append batch (rosout-only) at end
        result.append(batch_cmd)

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


def get_project_cache_dir(results_dir: str) -> str:
    """Return the .cache directory inside results_dir."""
    cache_dir = os.path.join(os.path.abspath(results_dir), ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _get_postprocessing_cache_paths(results_dir: str) -> tuple[str, str, str]:
    """Return (cache_dir, hash_file, outputs_file) for postprocessing.

    All cache files are stored inside ``results_dir/.cache/``.
    """
    cache_dir = get_project_cache_dir(results_dir)
    hash_file = os.path.join(cache_dir, "postprocessing.hash")
    outputs_file = os.path.join(cache_dir, "postprocessing.outputs")
    return cache_dir, hash_file, outputs_file


def _load_postprocessing_outputs_exclude_set(outputs_file: str) -> set:
    """Load the set of paths to exclude from hashing (postprocessing outputs).

    Reads the per-results hidden file in .cache that lists all files produced by
    postprocessing. Paths are normalized to forward slashes, relative to the
    results directory.
    """
    exclude = set()
    if not os.path.isfile(outputs_file):
        return exclude
    try:
        with open(outputs_file, "r", encoding="utf-8") as f:
            for line in f:
                p = line.strip()
                if not p:
                    continue
                # Normalize to forward slashes for consistent comparison
                if os.sep != "/":
                    p = str(Path(p))
                exclude.add(p)
    except OSError:
        pass
    return exclude


def _path_should_exclude_from_hash(rel_path: str, exclude_set: set) -> bool:
    """Return True if rel_path is an output of postprocessing and should be excluded."""
    if not exclude_set:
        return False
    # Normalize to forward slashes
    if os.sep != "/":
        rel_path = str(Path(rel_path))
    if rel_path in exclude_set:
        return True
    # Exclude files under a listed path (e.g. output was a directory)
    for prefix in exclude_set:
        if prefix and (rel_path == prefix or rel_path.startswith(prefix + "/")):
            return True
    return False


def compute_dir_hash(dir_path: str, exclude_set: Optional[set] = None) -> str:
    """Compute a hash for a directory based on modification time and file sizes."""
    path = Path(dir_path)
    path_str = str(path)
    path_len = len(path_str) + 1  # +1 for trailing slash

    # Collect all files recursively except outputs of postprocessing and cache
    # Skip: hidden, .cache, .pyc, .tar.gz, postprocessing.yaml (written by postprocessing)
    # Optimized: use tuple for endswith()
    files_to_check = []
    for f in path.rglob("*"):
        if not f.is_file() or f.name.startswith(".") or f.is_symlink():
            continue
        if f.name.endswith(('.pyc')):
            continue
        if '.cache' in f.parts or f.name == "postprocessing.yaml":
            continue
        rel_path = str(f)[path_len:]
        if _path_should_exclude_from_hash(rel_path, exclude_set):
            continue
        files_to_check.append(f)

    # Build all data first, then hash once - fastest approach
    hash_parts = []
    for file_path in sorted(files_to_check):
        stat = file_path.stat()
        rel_path = str(file_path)[path_len:]  # Fast string slice
        hash_parts.append(f"{rel_path}|{stat.st_size}|{stat.st_mtime}\n")

    # Single join, encode, and hash operation
    hash_string = "".join(hash_parts)
    return hashlib.md5(hash_string.encode()).hexdigest()


def is_postprocessing_needed(  # pylint: disable=too-many-return-statements
        results_dir: str,
        vast_file: Optional[str] = None,
) -> bool:
    """Check whether postprocessing needs to run for *results_dir*.

    Returns ``True`` when:
    - No hash file exists (postprocessing has never been run), or
    - The results directory hash differs from the stored hash, or
    - Postprocessing commands are defined but the hash cannot be determined.

    Returns ``False`` when:
    - No postprocessing commands are configured, or
    - The stored hash matches the current directory hash (cache is valid).

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs).
        vast_file: Optional explicit path to a ``.vast`` file.

    Returns:
        ``True`` if postprocessing should be run, ``False`` otherwise.
    """
    if not os.path.exists(results_dir):
        return False

    # Resolve vast file
    if vast_file is not None:
        if not os.path.isfile(vast_file):
            return False
        vast_path = os.path.abspath(vast_file)
    else:
        vast_path, _ = find_campaign_vast_file(results_dir)
        if vast_path is None:
            return False

    # If no postprocessing commands are defined, nothing to do
    commands = get_postprocessing_commands(vast_path)
    if not commands:
        return False

    # Compare directory hash with stored hash
    _, hash_file, outputs_file = _get_postprocessing_cache_paths(results_dir)
    exclude_set = _load_postprocessing_outputs_exclude_set(outputs_file)
    try:
        current_hash = compute_dir_hash(results_dir, exclude_set=exclude_set)
    except Exception:  # pylint: disable=broad-except
        return True

    if not os.path.exists(hash_file):
        return True

    try:
        with open(hash_file, 'r') as f:
            stored_hash = f.read().strip()
        return stored_hash != current_hash
    except Exception:  # pylint: disable=broad-except
        return True


def run_postprocessing(  # pylint: disable=too-many-return-statements
        results_dir: str,
        output_callback=None,
        force: bool = False,
        vast_file: Optional[str] = None,
        debug: bool = False,
):
    """Run postprocessing commands on run results.

    The postprocessing configuration is read from the ``.vast`` file found in
    the most recent ``campaign-<id>/_config/`` directory under *results_dir*,
    unless *vast_file* is provided explicitly.

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs)
        output_callback: Optional callback function for output messages (takes message string)
        force: If True, bypass caching and force postprocessing even if results are unchanged
        vast_file: Optional explicit path to a ``.vast`` file.  When given, the
            campaign copy is ignored entirely.
        debug: If True, include full plugin stdout in output; otherwise show only the summary line.

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

    # Determine cache locations inside results_dir/.cache/
    cache_dir, hash_file, outputs_file = _get_postprocessing_cache_paths(results_dir)

    # Load list of postprocessing outputs (to be excluded from hashing)
    exclude_set = _load_postprocessing_outputs_exclude_set(outputs_file)

    # Skip cache check if force is enabled
    if not force:
        output(f"Checking if postprocessing is needed...")
        # Compute hash of results directory
        start_time = time.time()
        hash_result = compute_dir_hash(results_dir, exclude_set=exclude_set)
        elapsed_time = time.time() - start_time
        output(f"Hashing {results_dir} took {elapsed_time:.4f} seconds")

        # Check if postprocessing is needed by comparing with stored hash
        if os.path.exists(hash_file):
            try:
                with open(hash_file, 'r') as f:
                    stored_hash = f.read().strip()

                if stored_hash == hash_result:
                    output("Postprocessing skipped: results directory hash unchanged")
                    return True, "Postprocessing not needed (hash unchanged)"
            except Exception as e:
                output(f"Warning: Could not read hash file: {e}")
                # Continue with postprocessing if we can't read the hash file
    else:
        output("Force mode enabled: skipping cache check")
        hash_result = compute_dir_hash(results_dir, exclude_set=exclude_set)

    # Load plugins
    plugins = load_postprocessing_plugins()

    # Batch all batchable rosbags_* commands into a single rosbags_process call
    # (reads each rosbag once instead of once per plugin). rosout_to_csv is always
    # included in the batch, so the separate forced rosout run below is no longer needed.
    commands = _batch_rosbags_commands(commands)

    # Validate all commands first
    for command in commands:
        is_valid, error_msg = validate_postprocessing_command(command, plugins)
        if not is_valid:
            return False, error_msg

    results_dir_abs = os.path.abspath(results_dir)
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
            )

            all_provenance_entries.extend(entries)

            if not plugin_success:
                output(f"✗ {message}")
                success = False
                continue
            display_message = message if debug else message.splitlines()[0]
            output(f"✓ {display_message}")

    # Note: rosout_to_csv is always included in the rosbags_process batch created by
    # _batch_rosbags_commands(), so a separate forced rosout run is no longer needed.

    # Store the hash, list of postprocessing outputs, and write postprocessing.yaml
    output_paths = set()
    if success:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(hash_file, 'w') as f:
                f.write(hash_result)
            output(f"Stored postprocessing hash to {hash_file}")

            # Write hidden file listing all postprocessing output paths (used to exclude from hash)
            output_paths = set()
            for ent in all_provenance_entries:
                out = ent.get("output") or ""
                if not out:
                    continue
                if os.sep != "/":
                    out = str(Path(out))
                output_paths.add(out)
            with open(outputs_file, "w", encoding="utf-8") as f:
                for p in sorted(output_paths):
                    f.write(p + "\n")
        except Exception as e:
            output(f"Warning: Could not write cache files: {e}")

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

    # Add metadata.yaml and data.db to exclude set for future hash computations
    for campaign_item in Path(results_dir_abs).iterdir():
        if campaign_item.is_dir() and is_campaign_dir(campaign_item.name):
            meta_file = campaign_item / "metadata.yaml"
            if meta_file.exists():
                rel = str(meta_file.relative_to(Path(results_dir_abs)))
                output_paths.add(rel)
            db_file = campaign_item / "_execution" / "data.db"
            if db_file.exists():
                rel = str(db_file.relative_to(Path(results_dir_abs)))
                output_paths.add(rel)
    # Re-write outputs file with metadata.yaml included
    try:
        with open(outputs_file, "w", encoding="utf-8") as f:
            for p in sorted(output_paths):
                f.write(p + "\n")
    except OSError:
        pass

    if success:
        return True, "Postprocessing completed successfully!"
    return False, "Postprocessing failed!"
