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

"""Preprocessing functionality for analysis data."""
import hashlib
import os
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List

from .common import load_config


def load_preprocessing_plugins() -> Dict[str, callable]:
    """Load preprocessing command plugins from entry points.

    Returns:
        Dictionary mapping plugin names to their callable functions
    """
    plugins = {}
    try:
        eps = entry_points(group='robovast.preprocessing_commands')
        for ep in eps:
            try:
                # Load the entry point - should return a callable
                plugin_func = ep.load()
                plugins[ep.name] = plugin_func
            except Exception as e:
                # Log and continue if a plugin fails to load
                print(f"Warning: Failed to load preprocessing plugin '{ep.name}': {e}")
    except Exception:
        # No plugins available or entry_points call failed
        pass
    return plugins


def execute_preprocessing_plugin(
    plugin_name: str, 
    plugin_func: callable, 
    params: dict,
    results_dir: str,
    config_dir: str
) -> tuple[bool, str]:
    """Execute a preprocessing plugin with parameters.
    
    Args:
        plugin_name: Name of the plugin
        plugin_func: The plugin function to call
        params: Dictionary of parameters for the plugin
        results_dir: Path to the run-<id> directory
        config_dir: Directory containing the configuration file
        
    Returns:
        Tuple of (success, message)
    """
    # Add common arguments
    kwargs = {
        'results_dir': results_dir,
        'config_dir': config_dir,
        **params  # Merge in plugin-specific parameters
    }
    
    try:
        # Call the plugin function with parameters
        success, message = plugin_func(**kwargs)
        return success, message
    except TypeError as e:
        return False, f"Plugin '{plugin_name}' argument error: {e}"
    except Exception as e:
        return False, f"Plugin '{plugin_name}' execution error: {e}"


def validate_preprocessing_command(command: dict, plugins: Dict[str, callable]) -> tuple[bool, str]:
    """Validate a preprocessing command.

    Args:
        command: Command dict with 'name' key
        plugins: Dictionary of available plugins

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(command, dict):
        return False, f"Preprocessing command must be a dict with 'name' field, got {type(command)}"
    
    plugin_name = command.get('name')
    if not plugin_name:
        return False, "Preprocessing command missing 'name' field"
    
    if plugin_name not in plugins:
        available = ', '.join(sorted(plugins.keys()))
        return False, (
            f"Unknown preprocessing plugin: '{plugin_name}'. "
            f"Available plugins: {available if available else 'none'}. "
            f"Use 'vast analysis preprocessing-commands' to list all plugins."
        )
    
    return True, ""


def get_preprocessing_commands(config_path: str) -> List[dict]:
    """Get preprocessing commands from configuration file.

    Args:
        config_path: Path to .vast configuration file

    Returns:
        List of preprocessing commands (dicts) or empty list if none defined
    """
    analysis_config = load_config(config_path, subsection="analysis", allow_missing=True)
    return analysis_config.get("preprocessing", [])


def compute_dir_hash(dir_path):
    """Compute a hash for a directory based on modification time and file sizes."""
    path = Path(dir_path)
    path_str = str(path)
    path_len = len(path_str) + 1  # +1 for trailing slash

    # Collect all files recursively except those starting with "." or ending with .pyc or .tar.gz
    # Also skip files in .cache subdirectories
    # Optimized: use tuple for endswith()
    files_to_check = [
        f for f in path.rglob("*")
        if f.is_file()
        and not f.name.startswith(".")
        and not f.is_symlink()
        and not f.name.endswith(('.pyc', '.tar.gz'))
        and '.cache' not in f.parts
    ]

    # Build all data first, then hash once - fastest approach
    # Avoids repeated encode() and hasher.update() calls
    hash_parts = []
    for file_path in sorted(files_to_check):
        stat = file_path.stat()
        rel_path = str(file_path)[path_len:]  # Fast string slice
        hash_parts.append(f"{rel_path}|{stat.st_size}|{stat.st_mtime}\n")

    # Single join, encode, and hash operation
    hash_string = "".join(hash_parts)
    return hashlib.md5(hash_string.encode()).hexdigest()


def run_preprocessing(config_path: str, results_dir: str, output_callback=None):  # pylint: disable=too-many-return-statements
    """Run preprocessing commands on test results.

    Args:
        config_path: Path to .vast configuration file
        results_dir: Directory containing test results
        output_callback: Optional callback function for output messages (takes message string)

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

    # Get preprocessing commands
    commands = get_preprocessing_commands(config_path)

    if not commands:
        return True, "No preprocessing commands defined."

    output(f"Checking if preprocessing is needed...")
    # Compute hash of results directory
    start_time = time.time()
    hash_result = compute_dir_hash(results_dir)
    elapsed_time = time.time() - start_time
    output(f"Hashing {results_dir} took {elapsed_time:.4f} seconds")

    # Check if preprocessing is needed by comparing with stored hash
    hash_file = os.path.join(results_dir, ".robovast_preprocessing.hash")

    if os.path.exists(hash_file):
        try:
            with open(hash_file, 'r') as f:
                stored_hash = f.read().strip()

            if stored_hash == hash_result:
                output("Preprocessing skipped: results directory hash unchanged")
                return True, "Preprocessing not needed (hash unchanged)"
        except Exception as e:
            output(f"Warning: Could not read hash file: {e}")
            # Continue with preprocessing if we can't read the hash file

    # Load plugins
    plugins = load_preprocessing_plugins()
    
    # Validate all commands first
    for command in commands:
        is_valid, error_msg = validate_preprocessing_command(command, plugins)
        if not is_valid:
            return False, error_msg

    # Get config directory for resolving relative paths
    config_dir = os.path.dirname(os.path.abspath(config_path))

    # Execute each preprocessing command
    success = True

    for i, command in enumerate(commands, 1):
        if not isinstance(command, dict):
            output(f"[{i}/{len(commands)}] ✗ Invalid command format: must be dict with 'name' field")
            success = False
            continue
        
        plugin_name = command.get('name')
        params = {k: v for k, v in command.items() if k != 'name'}
        display_cmd = f"{plugin_name} (params: {params})" if params else plugin_name
        
        plugin_func = plugins[plugin_name]
        
        output(f"[{i}/{len(commands)}] Executing: {display_cmd}")

        # Execute the plugin
        plugin_success, message = execute_preprocessing_plugin(
            plugin_name=plugin_name,
            plugin_func=plugin_func,
            params=params,
            results_dir=results_dir,
            config_dir=config_dir
        )

        if not plugin_success:
            output(f"✗ {message}")
            success = False
            continue
        else:
            output(f"✓ {message}")

    # Store the hash if preprocessing was successful
    if success:
        try:
            with open(hash_file, 'w') as f:
                f.write(hash_result)
            output(f"Stored preprocessing hash to {hash_file}")
        except Exception as e:
            output(f"Warning: Could not write hash file: {e}")

        return True, "Preprocessing completed successfully!"
    else:
        return False, "Preprocessing failed!"
