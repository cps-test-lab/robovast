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

"""Default preprocessing command plugins for RoboVAST.

This module provides built-in preprocessing commands that can be referenced
by name in the configuration file.

Each function is a Python implementation that executes commands using subprocess.
All functions accept a results_dir parameter containing the path to the run-<id> directory
to process, along with a config_dir for resolving relative paths, and additional
command-specific parameters.

Each function returns a tuple of (success: bool, message: str).

Configuration format:
    preprocessing:
      - name: plugin_name
        param1: value1
        param2: value2
"""
import os
import subprocess
from typing import List, Optional, Tuple


def command(results_dir: str, config_dir: str, script: str, args: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Execute an arbitrary command or script.
    
    Generic plugin that allows execution of any command or script path.
    Use this for custom scripts or when a specific plugin doesn't exist.
    
    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        script: Script path to execute (relative or absolute)
        args: Optional list of command-line arguments to pass to the script
    
    Returns:
        Tuple of (success, message)
    
    Example usage in .vast config:
        preprocessing:
          - name: command
            script: ../../../tools/docker_exec.sh
            args: [custom_script.py, --arg, value]
          - name: command
            script: /absolute/path/to/script.sh
    """
    # Resolve script path if not absolute
    script_path = script
    if not os.path.isabs(script_path) and config_dir:
        script_path = os.path.join(config_dir, script_path)
    
    if not os.path.exists(script_path):
        return False, f"Script not found: {script_path}"
    
    # Build full command
    full_command = [script_path]
    if args:
        full_command.extend(args)
    full_command.append(results_dir)
    
    try:
        result = subprocess.run(
            full_command,
            cwd=results_dir,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        if result.returncode != 0:
            return False, f"Command failed with exit code {result.returncode}\n{result.stderr}"
        
        return True, "Command executed successfully"
        
    except Exception as e:
        return False, f"Error executing command: {e}"


def rosbags_tf_to_csv(results_dir: str, config_dir: str, frames: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Convert ROS TF (transform) data from rosbags to CSV format.
    
    Extracts transformation data from ROS bag files and converts it to CSV
    format for easier analysis. Useful for analyzing robot poses, sensor
    positions, and coordinate transformations over time.
    
    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        frames: Optional list of TF frame names to extract
    
    Returns:
        Tuple of (success, message)
    
    Example usage in .vast config:
        preprocessing:
          - name: rosbags_tf_to_csv
            frames: [base_link, map]
          - name: rosbags_tf_to_csv  # Extract all frames
    """
    script_path = "tools/docker_exec.sh"
    if config_dir:
        script_path = os.path.join(config_dir, script_path)
    
    if not os.path.exists(script_path):
        return False, f"Script not found: {script_path}"
    
    # Build command with frame arguments
    command = [script_path, "rosbags_tf_to_csv.py"]
    
    if frames:
        for frame in frames:
            command.extend(["--frame", frame])
    
    command.append(results_dir)
    
    try:
        result = subprocess.run(
            command,
            cwd=os.path.dirname(script_path),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        if result.returncode != 0:
            return False, f"rosbags_tf_to_csv failed with exit code {result.returncode}\n{result.stderr}"
        
        return True, "TF data converted to CSV successfully"
        
    except Exception as e:
        return False, f"Error executing rosbags_tf_to_csv: {e}"


def rosbags_bt_to_csv(results_dir: str, config_dir: str) -> Tuple[bool, str]:
    """Convert ROS behavior tree data from rosbags to CSV format.
    
    Extracts behavior tree execution logs from ROS bag files and converts
    them to CSV format. Useful for analyzing robot decision-making,
    task execution sequences, and behavior tree node activations.
    
    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
    
    Returns:
        Tuple of (success, message)
    
    Example usage in .vast config:
        preprocessing:
          - name: rosbags_bt_to_csv
    """
    script_path = "tools/docker_exec.sh"
    if config_dir:
        script_path = os.path.join(config_dir, script_path)
    
    if not os.path.exists(script_path):
        return False, f"Script not found: {script_path}"
    
    command = [script_path, "rosbags_bt_to_csv.py", results_dir]
    
    try:
        result = subprocess.run(
            command,
            cwd=os.path.dirname(script_path),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        if result.returncode != 0:
            return False, f"rosbags_bt_to_csv failed with exit code {result.returncode}\n{result.stderr}"
        
        return True, "Behavior tree data converted to CSV successfully"
        
    except Exception as e:
        return False, f"Error executing rosbags_bt_to_csv: {e}"
