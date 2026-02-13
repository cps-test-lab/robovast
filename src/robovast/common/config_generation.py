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

import copy
import fnmatch
import logging
import os
import re
import tempfile
from datetime import datetime
from importlib.metadata import entry_points

from .common import (get_scenario_parameters, load_config)

logger = logging.getLogger(__name__)


def progress_update(msg):
    logger.info(msg)


def execute_variation(base_dir, configs, variation_class, parameters, general_parameters, progress_update_callback, scenario_file, output_dir=None):
    logger.debug(f"Executing variation: {variation_class.__name__}")
    variation = variation_class(base_dir, parameters, general_parameters, progress_update_callback, scenario_file, output_dir)
    try:
        configs = variation.variation(copy.deepcopy(configs))
    except Exception as e:
        logger.error(f"Variation failed. {variation_class.__name__}: {e}")
        progress_update_callback(f"Variation failed. {variation_class.__name__}: {e}")
        return []

    # Check if configs is None and return empty list
    if configs is None:
        logger.warning(f"Variation failed. {variation_class.__name__}: No configs returned")
        progress_update_callback(f"Variation failed. {variation_class.__name__}: No configs returned")
        return []

    logger.debug(f"Variation {variation_class.__name__} completed successfully")
    # progress_update(f"Current configs {configs}")
    return configs


def collect_filtered_files(filter_pattern, rel_path):
    """Collect files from scenario directory that match the filter patterns"""
    filtered_files = []
    logger.debug(f"Collecting filtered files from: {rel_path}")
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


def _get_variation_classes(scenario_config):
    """
    Read variation class names scenario

    """

    # Get the variation list from settings
    variation_list = scenario_config.get('variations', [])

    if not variation_list or not isinstance(variation_list, list):
        return []

    # Dynamically discover available variation classes from entry points
    available_classes = {}

    # Load variation types from robovast.variation_types entry point
    try:
        eps = entry_points()
        variation_eps = eps.select(group='robovast.variation_types')
        
        ep_list = list(variation_eps)
        if not ep_list:
            logger.warning("No variation types found in entry points. This usually means the package is not properly installed.")
            logger.warning("Try running: poetry install")
            print("WARNING: No variation type plugins found! Run 'poetry install' to register plugins.")

        for ep in ep_list:
            try:
                variation_class = ep.load()
                available_classes[ep.name] = variation_class
                logger.debug(f"Loaded variation type: {ep.name}")
            except Exception as e:
                logger.warning(f"Failed to load variation type '{ep.name}': {e}")
                print(f"Warning: Failed to load variation type '{ep.name}': {e}")
    except Exception as e:
        logger.error(f"Failed to load variation types from entry points: {e}")
        print(f"Warning: Failed to load variation types from entry points: {e}")

    # Extract variation class names from the list
    variation_classes = []
    for item in variation_list:
        if isinstance(item, dict):
            # Each item in the list should be a dict with one key (the class name)
            for class_name in item.keys():
                if class_name in available_classes:
                    variation_classes.append((available_classes[class_name], item[class_name]))
                else:
                    error_msg = f"Unknown variation class '{class_name}' found in variation file.\n"
                    if not available_classes:
                        error_msg += "No variation plugins are registered. This usually means the robovast package is not properly installed.\n"
                        error_msg += "To fix this, run: poetry install\n"
                        error_msg += "If you're in a CI environment, ensure 'poetry install' (without --no-root) has been executed."
                    else:
                        error_msg += f"Available variation types: {', '.join(available_classes.keys())}"
                    raise ValueError(error_msg)

    return variation_classes


def generate_scenario_variations(variation_file, progress_update_callback=None, variation_classes=None, output_dir=None, test_files_filter=None):
    if not progress_update_callback:
        progress_update_callback = logger.debug
    progress_update_callback("Start generating configs.")

    parameters = load_config(variation_file)

    # Get scenario file from configuration section
    configurations = parameters.get('configuration', [])

    test_files = []
    # Get test_files_filter from config
    test_files_filter = parameters.get("execution", {}).get("test_files_filter", [])
    if test_files_filter:
        additional_test_files = collect_filtered_files(test_files_filter, os.path.dirname(variation_file))
        progress_update_callback(f"Loaded {len(test_files_filter)} filter patterns (found {len(additional_test_files)} files).")
        test_files.extend(additional_test_files)

    configs = []
    variation_gui_classes = {}

    # Get scenario_file from execution section
    execution_scenario_file_name = parameters.get('execution', {}).get('scenario_file')
    scenario_file = os.path.join(os.path.dirname(variation_file), execution_scenario_file_name) if execution_scenario_file_name else None

    if output_dir is None:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_variation_")
        output_dir = temp_path.name

    general_parameters = parameters.get('general', {})

    # Get scenario parameters once (same for all configurations)
    scenario_param_dict = get_scenario_parameters(scenario_file)
    existing_scenario_parameters = next(iter(scenario_param_dict.values())) if scenario_param_dict else []

    for config in configurations:
        if variation_classes is None:
            # Read variation classes from the variation file
            variation_classes_and_parameters = _get_variation_classes(config)
        else:
            raise NotImplementedError("Passing variation_classes is not implemented yet")

        # Initialize config dict with scenario parameters if they exist
        config_dict = {}

        scenario_parameters = config.get('parameters', [])
        if scenario_parameters:
            # Convert list of single-key dicts to a single dict
            for param in scenario_parameters:
                if isinstance(param, dict):
                    config_dict.update(param)

            # Validate that all specified parameters exist in the scenario
            if existing_scenario_parameters:
                # Extract parameter names from the scenario (each entry has a 'name' field)
                valid_param_names = [p.get('name') for p in existing_scenario_parameters if isinstance(p, dict) and 'name' in p]

                # Check each parameter in config_dict
                invalid_params = [p for p in config_dict if p not in valid_param_names]
                if invalid_params:
                    raise ValueError(
                        f"Invalid parameters in scenario '{config['name']}': {invalid_params}. "
                        f"Valid parameters are: {valid_param_names}"
                    )

        current_configs = [{
            'name': config['name'],
            'config': config_dict}]

        for variation_class, variation_parameters in variation_classes_and_parameters:
            variation_gui_class = None
            if hasattr(variation_class, 'GUI_CLASS'):
                if variation_class.GUI_CLASS is not None:
                    variation_gui_class = variation_class.GUI_CLASS
            if variation_gui_class:
                if variation_gui_class not in variation_gui_classes:
                    variation_gui_classes[variation_gui_class] = []
            variation_gui_renderer_class = None
            if hasattr(variation_class, 'GUI_RENDERER_CLASS'):
                variation_gui_renderer_class = variation_class.GUI_RENDERER_CLASS
                if variation_gui_renderer_class is not None:
                    if variation_gui_class is None:
                        raise ValueError(f"Variation class {variation_class.__name__} has GUI_RENDERER_CLASS defined but no GUI_CLASS.")
                    variation_gui_classes[variation_gui_class].append(variation_gui_renderer_class)
            result = execute_variation(os.path.dirname(variation_file), current_configs, variation_class,
                                       variation_parameters, general_parameters, progress_update_callback, scenario_file, output_dir)
            if result is None or len(result) == 0:
                # If a variation step fails or produces no results, stop the pipeline
                progress_update_callback(f"Variation pipeline stopped at {variation_class.__name__} - no configs to process")
                current_configs = []
                break
            current_configs = result

        configs.extend(current_configs)

    # Extract execution parameters from execution section
    execution_section = parameters.get('execution', {})
    execution_params = {
        "env": execution_section.get('env'),
        "run_as_user": execution_section.get('run_as_user')
    }
    
    # Build result dictionary
    result = {
        "vast": variation_file,
        "scenario_file": scenario_file,
        "configs": configs,
        "_test_files": test_files,
        "execution": execution_params,
        "created_at": datetime.now().isoformat()
    }
    
    # Add metadata if it exists
    metadata = parameters.get('metadata')
    if metadata:
        result["metadata"] = metadata
    
    return result, variation_gui_classes
