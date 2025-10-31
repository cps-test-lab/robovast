#!/usr/bin/env python3
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

from enum import Enum
import yaml


class RunType(Enum):
    SINGLE_TEST = 0
    SINGLE_VARIANT = 1
    RUN = 2


def clean_test_name(test_file_path):
    run_file = test_file_path / "run.yaml"

    if not run_file.exists():
        print(f"Run file not found: {run_file}")
        return None

    try:
        # 2. Parse scenarios/Dataset/scenario.variants to get the entry that matches the real name
        with open(run_file, 'r') as f:
            content = f.read()

        try:
            run_data = yaml.safe_load(content)
        except yaml.YAMLError:
            return None

    except Exception as e:
        print(f"Error getting run data for {run_file}: {str(e)}")
        return None

    if not run_data or "SCENARIO_CONFIG" not in run_data:
        return None

    return run_data["SCENARIO_CONFIG"]


def get_variant_value(test_file_path, key):
    """
    Get a specific value from the variant data for a test by parsing scenario.variants

    Args:
        test_file_path: The path to the test file
        key: The key to retrieve from the variant data

    Returns:
        Value corresponding to the key, or None if not found
    """
    variant_file = test_file_path / "scenario.variant"

    if not variant_file.exists():
        print(f"Variant file not found: {variant_file}")
        return None

    try:
        # 2. Parse scenarios/Dataset/scenario.variants to get the entry that matches the real name
        with open(variant_file, 'r') as f:
            content = f.read()

        try:
            variant_data = yaml.safe_load(content)
        except yaml.YAMLError:
            return None

    except Exception as e:
        print(f"Error getting variant data for {variant_file}: {str(e)}")
        return None

    if not variant_data:
        return None
    path = ["nav_scenario"]
    path.append(key)

    # Navigate through the variant data using path
    value = variant_data
    for k in path:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return None
    return value


def check_preferred_log_file(file_path):
    """Check if a log file is the preferred one (starts with 'python' and contains 'scenario_execution_ros')"""
    try:
        # Check if filename starts with 'python'
        if not file_path.name.lower().startswith('python'):
            return False

        # Check if file contains 'scenario_execution_ros'
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            # Read first few KB to check for the pattern
            content = f.read(8192)  # Read first 8KB
            if 'scenario_execution_ros' in content:
                return True

            # If not found in first chunk, continue reading
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                if 'scenario_execution_ros' in chunk:
                    return True

    except Exception:
        pass
    return False


def get_scenario_execution_log_file(logs_dir):
    """
    Get the scenario execution log file from the specified directory.

    Args:
        logs_dir: Path to the directory containing log files

    Returns:
        Path object representing the scenario execution log file, or None if not found
    """
    if not logs_dir or not logs_dir.exists():
        return None

    for log_file in get_log_files(logs_dir):
        if check_preferred_log_file(log_file):
            return log_file
    return None


def get_log_files(logs_dir):
    """
    Get all log files in the specified directory.

    Args:
        logs_dir: Path to the directory containing log files

    Returns:
        List of Path objects representing the log files
    """
    if not logs_dir or not logs_dir.exists():
        return []

    log_files = []
    for pattern in ["*.log", "*.txt"]:
        log_files.extend(logs_dir.glob(pattern))

    # Sort by name
    log_files = sorted(log_files, key=lambda f: f.name)
    return log_files
