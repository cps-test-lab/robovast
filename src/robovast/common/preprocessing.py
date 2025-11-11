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
import os
import subprocess
from typing import List

from .file_cache import FileCache
from .common import load_config


def get_preprocessing_commands(config_path: str) -> List[str]:
    """Get preprocessing commands from configuration file.

    Args:
        config_path: Path to .vast configuration file

    Returns:
        List of preprocessing commands or empty list if none defined
    """
    try:
        analysis_config = load_config(config_path, subsection="analysis")
        return analysis_config.get("preprocessing", [])
    except (ValueError, KeyError):
        return []


def get_command_files_and_paths(config_path: str, commands: List[str]) -> tuple[List[str], List[List[str]]]:
    """Extract command files and paths from preprocessing commands.

    Args:
        config_path: Path to .vast configuration file
        commands: List of preprocessing commands

    Returns:
        Tuple of (command_files, command_paths) where:
        - command_files: List of resolved file paths for each command
        - command_paths: List of split command arguments

    Raises:
        ValueError: If a command is invalid or file not found
    """
    command_files = []
    command_paths = []
    config_dir = os.path.dirname(config_path)

    for command in commands:
        splitted = command.split()
        if not splitted:
            raise ValueError(f"Invalid preprocessing command: {command}")

        if os.path.isabs(splitted[0]):
            command_path = splitted[0]
        else:
            command_path = os.path.join(config_dir, splitted[0])

        if not os.path.exists(command_path):
            raise ValueError(f"Preprocessing command not found: {command_path}")

        command_files.append(command_path)
        command_paths.append(splitted)

    return command_files, command_paths


def is_preprocessing_needed(config_path: str, results_dir: str) -> bool:
    """Check if preprocessing is needed.

    Args:
        config_path: Path to .vast configuration file
        results_dir: Path to the results directory

    Returns:
        bool indicating if preprocessing is needed
    """
    commands = get_preprocessing_commands(config_path)

    if not commands:
        return False

    try:
        command_files, _ = get_command_files_and_paths(config_path, commands)
    except ValueError:
        # If command validation fails, preprocessing is needed (will fail later with proper error)
        return True

    config_dir = os.path.dirname(config_path)
    cached_file = get_cached_file(config_dir, results_dir, commands, command_files)

    return not bool(cached_file)


def get_cached_file(config_dir, results_dir, commands, command_files):
    """Check if preprocessing cache is valid.

    Args:
        config_dir: Directory containing the configuration file
        results_dir: Path to the results directory
        commands: List of preprocessing commands
        command_files: List of command file paths

    Returns:
        Cached file path if cache is valid, None otherwise
    """
    file_cache = FileCache(config_dir, "robovast_preprocess_" + results_dir.replace(os.sep, "_"), commands)
    return file_cache.get_cached_file(command_files, content=False, strings_for_hash=commands, hash_only=True)


def reset_preprocessing_cache(config_path, results_dir):
    commands = get_preprocessing_commands(config_path)

    if not commands:
        return

    config_dir = os.path.dirname(config_path)
    file_cache = FileCache(config_dir, "robovast_preprocess_" + results_dir.replace(os.sep, "_"), commands)
    file_cache.remove_cache()


def run_preprocessing(config_path: str, results_dir: str, force: bool = False, output_callback=None):
    """Run preprocessing commands on test results.

    Args:
        config_path: Path to .vast configuration file
        results_dir: Directory containing test results
        force: Force preprocessing by skipping cache check
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

    # Get preprocessing commands
    commands = get_preprocessing_commands(config_path)

    if not commands:
        return False, "No preprocessing commands defined in configuration."

    # Validate and resolve command paths
    try:
        command_files, command_paths = get_command_files_and_paths(config_path, commands)
    except ValueError as e:
        return False, str(e)

    config_dir = os.path.dirname(config_path)

    # Check cache
    cached_file = get_cached_file(config_dir, results_dir, commands, command_files)

    if cached_file and not force:
        return True, "Preprocessing is already up to date. No action needed."

    # Validate results directory
    if not os.path.exists(results_dir):
        return False, f"Results directory does not exist: {results_dir}"

    # Execute each preprocessing command
    success = True

    for i, command in enumerate(command_paths, 1):
        command.append(os.path.abspath(results_dir))
        output(f"\n[{i}/{len(command_paths)}] Executing: {' '.join(command)}")

        try:
            result = subprocess.run(
                command,
                cwd=config_dir,
                check=False,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )

            if result.returncode == 0:
                output("✓ Success")
            else:
                output(f"✗ Failed with exit code {result.returncode}")
                success = False
                break

        except Exception as e:
            output(f"✗ Error executing command: {e}")
            success = False
            break

    # Save cache on success
    if success:
        file_cache = FileCache(config_dir, "robovast_preprocess_" + results_dir.replace(os.sep, "_"), commands)
        file_cache.save_file_to_cache(command_files, None, content=False, strings_for_hash=commands)
        return True, "Preprocessing completed successfully!"
    else:
        return False, "Preprocessing failed!"
