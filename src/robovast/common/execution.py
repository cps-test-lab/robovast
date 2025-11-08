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

import datetime
import fnmatch
import os
import re
import shutil
import tempfile

import yaml

from .common import convert_dataclasses_to_dict, load_config
from .variant_generation import generate_scenario_variations


def get_execution_env_variables(run_num, variant_name):
    run_id = f"run-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"
    scenario_id = f"{variant_name}-{run_num}"
    return {
        'RUN_ID': run_id,
        'RUN_NUM': str(run_num),
        'SCENARIO_ID': scenario_id,
        'SCENARIO_CONFIG': variant_name,
        'ROS_LOG_DIR': '/out/logs',
    }


def get_execution_variants(variation_config):

    parameters = load_config(variation_config, subsection="execution")

    # Read filter patterns once
    test_files_filter = parameters.get("test_files_filter", [])

    if test_files_filter:
        print(f"### Loaded {len(test_files_filter)} filter patterns.")

    # Discover scenarios and collect filtered files
    scenario_file = os.path.join(os.path.dirname(variation_config), parameters["scenario"])
    if not os.path.exists(scenario_file):
        raise FileNotFoundError(f"Scenario file does not exist: {scenario_file}")

    scenarios, output_dir = get_filtered_files(variation_config, scenario_file, test_files_filter)

    if not scenarios:
        raise ValueError("No scenario variants generated. ")
    return scenarios, output_dir


def get_filtered_files(variation_file, scenario_file, test_files_filter):

    output_dir = tempfile.TemporaryDirectory(prefix="robovast_execution_")
    variants = generate_scenario_variations(variation_file, print, variation_classes=None, output_dir=output_dir.name)

    if not variants:
        print("### Warning: No variants found.")
        return {}, output_dir
    scenarios = {}

    # Add files located next to the scenario file that match the filter patterns
    scenario_files = collect_filtered_files(test_files_filter, os.path.dirname(variation_file))

    # If we have variants data, create separate scenario entries for each variant
    for variant in variants:
        if variant is None:
            continue
        if 'name' not in variant and 'variant' not in variant:
            continue
        # Extract the name from the variant data for unique identification
        variant_name = variant["name"]

        variant_data = variant.get('variant')

        scenarios[variant_name] = {
            'scenario_files': scenario_files,
            'variant_files': variant["variant_files"],
            'original_scenario_path': scenario_file,
            'variant_data': {
                'test_scenario': convert_dataclasses_to_dict(variant_data)
            }
        }
        if 'variant_file_path' in variant:
            scenarios[variant_name]['variant_file_path'] = variant['variant_file_path']
        print(f"### Created {variant_name}")
    return scenarios, output_dir


def collect_filtered_files(filter_pattern, rel_path):
    """Collect files from scenario directory that match the filter patterns"""
    filtered_files = []
    print("### Collecting filtered files from:", rel_path)
    if not filter_pattern:
        return filtered_files
    for root, _, files in os.walk(rel_path):
        for file in files:
            file_path = os.path.join(root, file)
            if matches_patterns(file_path, filter_pattern, rel_path):
                key = os.path.relpath(file_path, rel_path)
                filtered_files.append(key)

    return filtered_files


def matches_patterns(file_path, patterns, base_dir):
    """Check if a file matches any of the gitignore-like patterns with support for ** recursive matching"""
    if not patterns:
        return False

    # Get relative path from base directory
    try:
        rel_path = os.path.relpath(file_path, base_dir)
    except ValueError:
        # Path is not relative to base_dir
        return False

    # Normalize path separators for consistent matching
    rel_path = rel_path.replace(os.sep, '/')

    for pattern in patterns:
        if _match_pattern(rel_path, pattern):
            return True

    return False


def _match_pattern(rel_path, pattern):
    """Match a single pattern against a relative path, supporting ** for recursive matching"""
    # Normalize pattern separators
    pattern = pattern.replace(os.sep, '/')

    # Handle directory patterns (ending with /)
    if pattern.endswith('/'):
        pattern = pattern[:-1]
        # Check if any parent directory matches
        parts = rel_path.split('/')
        for i in range(len(parts)):
            parent_path = '/'.join(parts[:i+1])
            if _glob_match(parent_path, pattern):
                return True
        return _glob_match(os.path.dirname(rel_path), pattern)
    else:
        # Handle file patterns
        if _glob_match(rel_path, pattern):
            return True
        # Also check just the filename
        if _glob_match(os.path.basename(rel_path), pattern):
            return True
        return False


def _glob_match(path, pattern):
    """Enhanced glob matching with support for ** recursive patterns"""
    # Handle ** patterns
    if '**' in pattern:
        return _match_recursive_pattern(path, pattern)
    else:
        # Use standard fnmatch for simple patterns
        return fnmatch.fnmatch(path, pattern)


def _match_recursive_pattern(path, pattern):
    """Match patterns containing ** for recursive directory matching"""

    # Split pattern by ** to handle each part
    pattern_parts = pattern.split('**')

    if len(pattern_parts) == 1:
        # No ** in pattern, use standard matching
        return fnmatch.fnmatch(path, pattern)

    # Convert glob pattern to regex, handling ** specially
    regex_pattern = ''
    for i, part in enumerate(pattern_parts):
        if i > 0:
            # Add regex for ** (match zero or more path segments)
            regex_pattern += '(?:[^/]+/)*'

        # Convert glob to regex for this part
        if part:
            # Remove leading/trailing slashes to avoid double slashes
            part = part.strip('/')
            if part:
                # Convert fnmatch pattern to regex
                part_regex = fnmatch.translate(part).replace('\\Z', '')
                # Remove the (?ms: prefix and ) suffix that fnmatch.translate adds
                if part_regex.startswith('(?ms:'):
                    part_regex = part_regex[5:-1]
                regex_pattern += part_regex
                if i < len(pattern_parts) - 1:
                    regex_pattern += '/'

    # Ensure the pattern matches the entire string
    regex_pattern = '^' + regex_pattern + '$'

    try:
        return bool(re.match(regex_pattern, path))
    except re.error:
        # Fallback to simple fnmatch if regex fails
        return fnmatch.fnmatch(path, pattern)


def prepare_run_configs(run_id, variants, variant_files_output_dir, output_dir):
    # Create the config directory structure: /config/$RUN_ID/
    config_dir = os.path.join(output_dir, "config", run_id)
    os.makedirs(config_dir, exist_ok=True)
    # Organize files by scenario
    for scenario_key, scenario_data in variants.items():
        scenario_dir = os.path.join(config_dir, scenario_key)
        os.makedirs(scenario_dir, exist_ok=True)

        # Copy scenario file
        original_scenario_path = scenario_data.get('original_scenario_path')
        shutil.copy2(original_scenario_path, os.path.join(scenario_dir, 'scenario.osc'))

        # Copy filtered files
        for config_file in scenario_data["scenario_files"]:
            src_path = os.path.join(os.path.dirname(original_scenario_path), config_file)
            dst_path = os.path.join(scenario_dir, config_file)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)

        # Copy variant files
        for config_file in scenario_data["variant_files"]:
            if "variant_file_path" not in scenario_data:
                raise ValueError("variant_file_path missing in scenario data")
            src_path = os.path.join(variant_files_output_dir,
                                    scenario_data["variant_file_path"], config_file)
            dst_path = os.path.join(scenario_dir, config_file)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)

        # Create variant file if needed
        variant_data = scenario_data.get('variant_data')
        if variant_data is not None:
            with open(os.path.join(scenario_dir, 'scenario.variant'), 'w') as f:
                yaml.dump(variant_data, f)
