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
from pathlib import Path
import time
import json
import os
import subprocess
from typing import List

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


def compute_dir_hash(dir_path):
    """Compute a hash for a directory based on modification time and file sizes."""
    path = Path(dir_path)

    # Collect all files recursively except those starting with "." or ending with .pyc
    files_to_check = [f for f in path.rglob("*") if f.is_file() and not f.name.startswith(".")
                      and not f.is_symlink() and not f.name.endswith('pyc')]

    # Create hash based on file stats (even if empty)
    hash_data = []
    for file_path in sorted(files_to_check):
        stat = file_path.stat()
        hash_data.append({
            "name": file_path.name,
            "size": stat.st_size,
            "mtime": stat.st_mtime
        })

    # Create a simple hash string
    hash_string = json.dumps(hash_data, sort_keys=True)
    return hashlib.md5(hash_string.encode()).hexdigest()


def run_preprocessing(config_path: str, results_dir: str, output_callback=None): # pylint: disable=too-many-return-statements
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

    # Get preprocessing commands
    commands = get_preprocessing_commands(config_path)

    if not commands:
        return False, "No preprocessing commands defined in configuration."

    # Validate and resolve command paths
    try:
        _, command_paths = get_command_files_and_paths(config_path, commands)
    except ValueError as e:
        return False, str(e)

    config_dir = os.path.dirname(config_path)

    # Execute each preprocessing command
    success = True

    for i, command in enumerate(command_paths, 1):
        command.append(os.path.abspath(results_dir))
        output(f"[{i}/{len(command_paths)}] Executing: {' '.join(command)}")

        try:
            result = subprocess.run(
                command,
                cwd=config_dir,
                check=False,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )

            if result.returncode != 0:
                output(f"✗ Failed with exit code {result.returncode}")
                success = False
                continue

        except Exception as e:
            output(f"✗ Error executing command: {e}")
            success = False
            continue

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
