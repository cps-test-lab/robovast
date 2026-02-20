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

"""Postprocessing functionality for analysis data."""
import hashlib
import json
import os
import tempfile
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .common import load_config
from .results_utils import iter_test_folders


def load_postprocessing_plugins() -> Dict[str, callable]:
    """Load postprocessing command plugins from entry points.

    Returns:
        Dictionary mapping plugin names to their callable functions
    """
    plugins = {}
    try:
        eps = entry_points(group='robovast.postprocessing_commands')
        for ep in eps:
            try:
                # Load the entry point - should return a callable
                plugin_func = ep.load()
                plugins[ep.name] = plugin_func
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
) -> Tuple[bool, str, List[dict]]:
    """Execute a postprocessing plugin with parameters.

    Args:
        plugin_name: Name of the plugin
        plugin_func: The plugin function to call
        params: Dictionary of parameters for the plugin
        results_dir: Path to the run-<id> directory
        config_dir: Directory containing the configuration file
        provenance_file: Optional path for container plugins to write provenance JSON

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
            f"Use 'vast analysis postprocessing-commands' to list all plugins."
        )

    return True, ""


def get_postprocessing_commands(config_path: str) -> List[dict]:
    """Get postprocessing commands from configuration file.

    Args:
        config_path: Path to .vast configuration file

    Returns:
        List of postprocessing commands (dicts) or empty list if none defined
    """
    analysis_config = load_config(config_path, subsection="analysis", allow_missing=True)
    return analysis_config.get("postprocessing", [])


def _write_provenance_yaml_per_folder(results_dir: str, entries: List[dict]) -> None:
    """Write postprocessing.yaml in each test folder with entries whose output is under that folder."""
    results_path = Path(results_dir)
    # Normalize paths to forward slashes for consistent prefix match

    def norm(s: str) -> str:
        return str(Path(s)) if os.sep != "/" else s

    for _run_id, _config_name, _test_number, folder_path in iter_test_folders(results_dir):
        folder_rel = norm(os.path.relpath(str(folder_path), results_dir))
        prefix = folder_rel + os.sep
        folder_entries = []
        for ent in entries:
            out = ent.get("output") or ""
            out_norm = norm(out)
            if not (out_norm == folder_rel or out_norm.startswith(prefix)):
                continue
            # Output and sources are relative to results_dir; rewrite to be relative to folder
            try:
                out_full = results_path / out_norm
                out_rel = str(out_full.relative_to(folder_path))
            except (ValueError, TypeError):
                out_rel = out
            sources = ent.get("sources") or []
            sources_rel = []
            for src in sources:
                try:
                    src_full = results_path / norm(src)
                    sources_rel.append(str(src_full.relative_to(folder_path)))
                except (ValueError, TypeError):
                    sources_rel.append(src)
            folder_entries.append({
                "output": out_rel,
                "sources": sources_rel,
                "plugin": ent.get("plugin", ""),
                "params": ent.get("params") or {},
            })
        if folder_entries:
            yaml_path = folder_path / "postprocessing.yaml"
            try:
                with open(yaml_path, "w", encoding="utf-8") as f:
                    yaml.dump(
                        {"generated_by": "robovast", "entries": folder_entries},
                        f,
                        default_flow_style=False,
                        sort_keys=False,
                        allow_unicode=True,
                    )
            except OSError:
                pass  # skip if we cannot write


def get_project_cache_dir(config_path: str) -> str:
    """Return the project-level .cache directory (next to the .vast file)."""
    project_dir = os.path.dirname(os.path.abspath(config_path))
    cache_dir = os.path.join(project_dir, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _get_postprocessing_cache_paths(config_path: str, results_dir: str) -> tuple[str, str, str]:
    """Return (cache_dir, hash_file, outputs_file) for postprocessing.

    Both the hash file and the outputs listing are stored in the project-level
    .cache directory and keyed by the absolute results_dir path.
    """
    cache_dir = get_project_cache_dir(config_path)
    results_slug = hashlib.md5(os.path.abspath(results_dir).encode()).hexdigest()
    hash_file = os.path.join(cache_dir, f"postprocessing_{results_slug}.hash")
    outputs_file = os.path.join(cache_dir, f"postprocessing_{results_slug}.outputs")
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


def run_postprocessing(config_path: str, results_dir: str, output_callback=None, force: bool = False):  # pylint: disable=too-many-return-statements
    """Run postprocessing commands on test results.

    Args:
        config_path: Path to .vast configuration file
        results_dir: Directory containing test results
        output_callback: Optional callback function for output messages (takes message string)
        force: If True, bypass caching and force postprocessing even if results are unchanged

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

    # Get postprocessing commands
    commands = get_postprocessing_commands(config_path)

    if not commands:
        return True, "No postprocessing commands defined."

    # Determine cache locations (project-level .cache next to the .vast file)
    cache_dir, hash_file, outputs_file = _get_postprocessing_cache_paths(config_path, results_dir)

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

    # Validate all commands first
    for command in commands:
        is_valid, error_msg = validate_postprocessing_command(command, plugins)
        if not is_valid:
            return False, error_msg

    # Get config directory for resolving relative paths
    config_dir = os.path.dirname(os.path.abspath(config_path))

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
            )

            all_provenance_entries.extend(entries)

            if not plugin_success:
                output(f"✗ {message}")
                success = False
                continue
            output(f"✓ {message}")

    # Store the hash, list of postprocessing outputs, and write postprocessing.yaml if succeeded
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

        # Write postprocessing.yaml in each test folder
        _write_provenance_yaml_per_folder(results_dir_abs, all_provenance_entries)

        return True, "Postprocessing completed successfully!"
    return False, "Postprocessing failed!"
