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

"""Default postprocessing command plugins for RoboVAST.

This module provides built-in postprocessing commands that can be referenced
by name in the configuration file.

Each function is a Python implementation that executes commands using subprocess
or host-side logic. All functions accept a results_dir parameter containing the
path to the results directory (parent of run-* dirs) or run-<id> directory to
process, along with a config_dir for resolving relative paths, and additional
command-specific parameters.

Each function returns a tuple of (success: bool, message: str).

Configuration format:
    postprocessing:
      - plugin_name:
          param1: value1
          param2: value2
      - simple_plugin_name
"""
import os
import subprocess
import tarfile
from importlib.resources import files
from pathlib import Path
from typing import List, Optional, Tuple


def command(
    results_dir: str,
    config_dir: str,
    script: str,
    args: Optional[List[str]] = None,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Execute an arbitrary command or script.

    Generic plugin that allows execution of any command or script path.
    Use this for custom scripts or when a specific plugin doesn't exist.

    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        script: Script path to execute (relative or absolute)
        args: Optional list of command-line arguments to pass to the script
        provenance_file: Optional path for provenance JSON (passed to script if it supports it)

    Returns:
        Tuple of (success, message)

    Example usage in .vast config:
        postprocessing:
          - command:
              script: ../../../tools/docker_exec.sh
              args: [custom_script.py, --arg, value]
          - command:
              script: /absolute/path/to/script.sh
    """
    # Resolve script path if not absolute
    script_path = script
    if not os.path.isabs(script_path) and config_dir:
        script_path = os.path.join(config_dir, script_path)

    if not os.path.exists(script_path):
        return False, f"Script not found: {script_path}"

    # Build full command (optionally pass provenance to docker_exec and script)
    full_command = [script_path]
    if provenance_file:
        full_command.extend(["--provenance-file", provenance_file])
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


def rosbags_tf_to_csv(
    results_dir: str,
    config_dir: str,
    frames: Optional[List[str]] = None,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
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
        postprocessing:
          - rosbags_tf_to_csv:
              frames: [base_link, map]
          - rosbags_tf_to_csv  # Extract all frames
    """
    # Get docker_exec.sh from package data
    script_path = str(files('robovast.common.data').joinpath('docker_exec.sh'))

    # Build command: docker_exec [--provenance-file HOST] script.py [--provenance-file /provenance/...] [args] results_dir
    cmd = [script_path]
    if provenance_file:
        cmd.extend(["--provenance-file", provenance_file])
    cmd.append("rosbags_tf_to_csv.py")
    if provenance_file:
        cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
    if frames:
        for frame in frames:
            cmd.extend(["--frame", frame])
    cmd.append(results_dir)

    try:
        result = subprocess.run(
            cmd,
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


def rosbags_bt_to_csv(
    results_dir: str,
    config_dir: str,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
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
        postprocessing:
          - rosbags_bt_to_csv
    """
    # Get docker_exec.sh from package data
    script_path = str(files('robovast.common.data').joinpath('docker_exec.sh'))

    cmd = [script_path]
    if provenance_file:
        cmd.extend(["--provenance-file", provenance_file])
    cmd.append("rosbags_bt_to_csv.py")
    if provenance_file:
        cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
    cmd.append(results_dir)

    try:
        result = subprocess.run(
            cmd,
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


def rosbags_localization_error_to_csv(
    results_dir: str,
    config_dir: str,
    topic: Optional[str] = None,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Extract localization error (covariance) data from rosbags to CSV format.

    Extracts pose and covariance data from PoseWithCovarianceStamped messages
    in ROS bag files and converts them to CSV format. Useful for analyzing
    localization uncertainty, AMCL performance, and pose estimation quality.
    The output includes pose (position and orientation) and key covariance
    values (diagonal elements for x, y, z, roll, pitch, yaw and some
    off-diagonal correlations).

    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        topic: Optional topic name containing PoseWithCovarianceStamped messages
               (default: /amcl_pose)

    Returns:
        Tuple of (success, message)

    Example usage in .vast config:
        postprocessing:
          - rosbags_localization_error_to_csv
          - rosbags_localization_error_to_csv:
              topic: /amcl_pose
    """
    # Get docker_exec.sh from package data
    script_path = str(files('robovast.common.data').joinpath('docker_exec.sh'))

    cmd = [script_path]
    if provenance_file:
        cmd.extend(["--provenance-file", provenance_file])
    cmd.append("rosbags_localization_error_to_csv.py")
    if provenance_file:
        cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
    if topic:
        cmd.extend(["--topic", topic])
    cmd.append(results_dir)

    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(script_path),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )

        if result.returncode != 0:
            return False, f"rosbags_localization_error_to_csv failed with exit code {result.returncode}\n{result.stderr}"

        return True, "Localization error data converted to CSV successfully"

    except Exception as e:
        return False, f"Error executing rosbags_localization_error_to_csv: {e}"


def rosbags_action_to_csv(
    results_dir: str,
    config_dir: str,
    action: str,
    filename_prefix: Optional[str] = None,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Extract ROS2 action feedback and status from rosbags to CSV format.

    Reads /<action>/_action/feedback and /<action>/_action/status topics from
    ROS bag files and writes two CSV files: <filename_prefix>_feedback.csv
    and <filename_prefix>_status.csv with flattened columns.

    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        action: Action name to extract (e.g. 'navigate_to_pose')
        filename_prefix: Output filename prefix (default: action_<action>).
            Produces <prefix>_feedback.csv and <prefix>_status.csv.

    Returns:
        Tuple of (success, message)

    Example usage in .vast config:
        postprocessing:
          - rosbags_action_to_csv:
              action: navigate_to_pose
          - rosbags_action_to_csv:
              action: navigate_to_pose
              filename_prefix: nav_action
    """
    script_path = str(files('robovast.common.data').joinpath('docker_exec.sh'))

    action_name = action.lstrip('/')
    effective_prefix = filename_prefix or f"action_{action_name}"

    cmd = [script_path]
    if provenance_file:
        cmd.extend(["--provenance-file", provenance_file])
    cmd.append("rosbags_action_to_csv.py")
    if provenance_file:
        cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
    cmd.extend(["--filename-prefix", effective_prefix])
    cmd.append(action_name)
    cmd.append(results_dir)

    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(script_path),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )

        if result.returncode != 0:
            return False, f"rosbags_action_to_csv failed with exit code {result.returncode}\n{result.stderr}"

        return True, f"Action '{action_name}' data extracted to CSV successfully"

    except Exception as e:
        return False, f"Error executing rosbags_action_to_csv: {e}"


def rosbags_to_csv(
    results_dir: str,
    config_dir: str,
    skip_topics: Optional[List[str]] = None,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Convert all ROS messages from rosbags to CSV format.

    Extracts all message data from ROS bag files and converts each topic
    to a separate CSV file. Useful for analyzing any ROS topic data that
    doesn't have a specialized converter. By default, skips large topics
    like costmaps and snapshots.

    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        skip_topics: Optional list of topic names to skip during conversion

    Returns:
        Tuple of (success, message)

    Example usage in .vast config:
        postprocessing:
          - rosbags_to_csv  # Use default skip list
          - rosbags_to_csv:
              skip_topics: [/large_topic, /another_topic]
    """
    # Get docker_exec.sh from package data
    script_path = str(files('robovast.common.data').joinpath('docker_exec.sh'))

    cmd = [script_path]
    if provenance_file:
        cmd.extend(["--provenance-file", provenance_file])
    cmd.append("rosbags_to_csv.py")
    if provenance_file:
        cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
    if skip_topics:
        for topic in skip_topics:
            cmd.extend(["--skip-topic", topic])
    cmd.append(results_dir)

    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(script_path),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )

        if result.returncode != 0:
            return False, f"rosbags_to_csv failed with exit code {result.returncode}\n{result.stderr}"

        return True, "ROS messages converted to CSV successfully"

    except Exception as e:
        return False, f"Error executing rosbags_to_csv: {e}"


def rosbags_to_webm(
    results_dir: str,
    config_dir: str,
    topic: Optional[str] = None,
    fps: Optional[float] = None,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Convert a CompressedImage topic from rosbags to WebM video files.

    Extracts compressed image frames from a ROS bag file and encodes them
    into a WebM video using FFmpeg (VP9 codec). JPEG frames are piped
    directly to FFmpeg without intermediate decoding for maximum performance.

    Args:
        results_dir: Path to the run-<id> directory to process
        config_dir: Directory containing the config file (for resolving relative paths)
        topic: CompressedImage topic name to convert (default: /camera/image_raw/compressed)
        fps: Fallback FPS when timestamps are unavailable (default: 30)

    Returns:
        Tuple of (success, message)

    Example usage in .vast config:
        postprocessing:
          - rosbags_to_webm  # Use default topic
          - rosbags_to_webm:
              topic: /front_camera/image_raw/compressed
              fps: 15
    """
    # Get docker_exec.sh from package data
    script_path = str(files('robovast.common.data').joinpath('docker_exec.sh'))

    cmd = [script_path]
    if provenance_file:
        cmd.extend(["--provenance-file", provenance_file])
    cmd.append("rosbags_to_webm.py")
    if provenance_file:
        cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
    if topic:
        cmd += ["--topic", topic]
    if fps is not None:
        cmd += ["--fps", str(fps)]
    cmd.append(results_dir)

    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(script_path),
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )

        if result.returncode != 0:
            return False, f"rosbags_to_webm failed with exit code {result.returncode}\n{result.stderr}"

        return True, "CompressedImage topic converted to WebM successfully"

    except Exception as e:
        return False, f"Error executing rosbags_to_webm: {e}"


def compress(
    results_dir: str,
    config_dir: str,
    output_dir: Optional[str] = None,
    exclude_dirs: Optional[List[str]] = None,
    overwrite: bool = True,
    provenance_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Create a gzipped tarball for each run-* directory (runs on host).

    For each direct subdirectory of results_dir whose name starts with ``run-``,
    creates a ``run-<id>.tar.gz`` in the output directory containing that run's
    contents. Does not use Docker; runs entirely on the host using Python's
    tarfile module. Useful for archiving or transferring results.

    output_dir must not be inside the results directory (would break postprocessing
    hash caching). Relative paths are resolved from the directory containing the
    .vast file (config_dir).

    Args:
        results_dir: Path to the results directory (parent of run-* dirs).
        config_dir: Directory containing the .vast config file; relative output_dir
            is resolved from here.
        output_dir: Where to write tarballs. If not set, defaults to config_dir.
            Relative paths are resolved from config_dir. Must not be inside
            results_dir.
        exclude_dirs: Directory names to exclude from the tarball (default: ['.cache']).
            Pass an empty list to include everything.
        overwrite:  If True (default), recreate and overwrite existing tarballs.
            If False, skip run dirs that already have a corresponding .tar.gz in the
            output directory.

    Returns:
        Tuple of (success, message).

    Example usage in .vast config:

    .. code-block:: yaml

       postprocessing:
         - compress:
             output_dir: archives
         - compress:
             output_dir: /path/to/archives
             overwrite: false
    """
    _ = provenance_file  # unused; kept for plugin API
    # Resolve output_dir from config_dir (relative to .vast file dir); default = config_dir
    if output_dir:
        out_dir = os.path.normpath(
            os.path.join(config_dir, output_dir) if not os.path.isabs(output_dir) else output_dir
        )
    else:
        out_dir = os.path.abspath(config_dir)
    out_abs = Path(out_dir).resolve()
    results_abs = Path(results_dir).resolve()
    # Forbid writing into results directory so postprocessing hashing is not affected
    if out_abs == results_abs or (out_abs != results_abs and results_abs in out_abs.parents):
        return False, (
            f"compress output_dir must not be inside the results directory "
            f"(would break postprocessing hash). output_dir={out_dir!r}, results_dir={results_dir!r}. "
            f"Use a path outside results (e.g. relative to .vast dir: output_dir: archives)."
        )
    exclude = set(exclude_dirs if exclude_dirs is not None else [".cache"])

    root = Path(results_dir)
    if not root.is_dir():
        return False, f"Results directory does not exist: {results_dir}"

    created = []
    for run_item in sorted(root.iterdir()):
        if not run_item.is_dir() or not run_item.name.startswith("run-"):
            continue
        if run_item.name == "_config":
            continue

        tarball_path = Path(out_dir) / f"{run_item.name}.tar.gz"
        if not overwrite and tarball_path.exists():
            continue
        try:
            os.makedirs(out_dir, exist_ok=True)
            with tarfile.open(tarball_path, "w:gz") as tf:
                for entry in run_item.rglob("*"):
                    if not entry.is_file():
                        continue
                    if any(part in exclude for part in entry.relative_to(run_item).parts):
                        continue
                    tf.add(entry, arcname=run_item.name + "/" + str(entry.relative_to(run_item)))
            created.append(tarball_path.name)
        except OSError as e:
            return False, f"Failed to create {tarball_path}: {e}"

    if not created:
        return True, "No run-* directories found or all tarballs already exist (use overwrite: true to recreate)"
    return True, f"Created tarballs: {', '.join(created)}"
