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
from importlib.metadata import entry_points
from datetime import datetime
import json

from .common import (get_scenario_parameters, load_config,
                     save_scenario_configs_file)
from ..prov.generate_prov_graph import (
    _create_abstract_scenario,
    _create_concrete_scenario,
    save_scenario_prov,
)

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

        for ep in variation_eps:
            try:
                variation_class = ep.load()
                available_classes[ep.name] = variation_class
            except Exception as e:
                print(f"Warning: Failed to load variation type '{ep.name}': {e}")
    except Exception as e:
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
                    raise ValueError(f"Unknown variation class '{class_name}' found in variation file")

    return variation_classes


def generate_scenario_variations(variation_file, progress_update_callback=None, variation_classes=None, output_dir=None, test_files_filter=None):
    if not progress_update_callback:
        progress_update_callback = logger.debug
    progress_update_callback("Start generating configs.")

    parameters = load_config(variation_file)

    # Get scenario file from configuration section
    scenarios = parameters.get('configuration', [])

    prov_config = parameters.get('provenance', {})

    scenario_files = []
    prov = []
    # Get test_files_filter from config
    test_files_filter = parameters.get("execution", {}).get("test_files_filter", [])
    if test_files_filter:
        scenario_files = collect_filtered_files(test_files_filter, os.path.dirname(variation_file))
        progress_update_callback(f"Loaded {len(test_files_filter)} filter patterns (found {len(scenario_files)} files).")
    parent_dir = os.path.split(os.path.split(variation_file)[0])[-1]
    vast_file = f"{parent_dir}/{os.path.split(variation_file)[-1]}"

    configs = []
    variation_gui_classes = {}
    for scenario in scenarios:
        scenario_file_name = scenario.get('scenario_file')
        scenario_file = os.path.join(os.path.dirname(variation_file), scenario_file_name) if scenario_file_name else None
        scenario_id =  f"{parent_dir}/{scenario_file_name}"
        scenario_prov = _create_abstract_scenario(scenario_id)
        prov.append(scenario_prov)

        if output_dir is None:
            temp_path = tempfile.TemporaryDirectory(prefix="robovast_variation_")
            output_dir = temp_path.name

        if variation_classes is None:
            # Read variation classes from the variation file
            variation_classes_and_parameters = _get_variation_classes(scenario)
        else:
            raise NotImplementedError("Passing variation_classes is not implemented yet")

        general_parameters = parameters.get('general', {})

        # Initialize config dict with scenario parameters if they exist
        config_dict = {}
        scenario_param_dict = get_scenario_parameters(scenario_file)
        existing_scenario_parameters = next(iter(scenario_param_dict.values())) if scenario_param_dict else []

        scenario_parameters = scenario.get('parameters', [])
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
                        f"Invalid parameters in scenario '{scenario['name']}': {invalid_params}. "
                        f"Valid parameters are: {valid_param_names}"
                    )

        current_configs = [{
            'name': scenario['name'],
            'config': config_dict,
            '_config_files': [],
            '_scenario_file': scenario_file}]
        if test_files_filter:
            current_configs[0]['_scenario_files'] = scenario_files

        for variation_class, parameters in variation_classes_and_parameters:
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
                                       parameters, general_parameters, progress_update_callback, scenario_file, output_dir)
            if result is None or len(result) == 0:
                # If a variation step fails or produces no results, stop the pipeline
                progress_update_callback(f"Variation pipeline stopped at {variation_class.__name__} - no configs to process")
                current_configs = []
                break
            current_configs = result

        gen_time = datetime.now().isoformat()
        for c in current_configs:
            c["abstract_scenario"] = scenario_id
            c["gen_time"] = gen_time
            c["parent_dir"] = parent_dir
            c["source_files"] = [scenario_id, vast_file]
        configs.extend(current_configs)
    if configs:
        save_scenario_configs_file(configs, os.path.join(output_dir, 'scenario.configs'))
        prov.extend(scenario_gen_prov(configs))
        save_scenario_prov(prov, prov_config, output_dir)

    return configs, variation_gui_classes

def scenario_gen_prov(configs):
    prov = []
    for i, config in enumerate(configs):
        parent_scenario_id = config.get("abstract_scenario")
        parent_dir = config.get("parent_dir")
        concr_scenario_id = f"{parent_dir}/configs/{config['name']}-{i:03d}.config"

        if config.get("_config_files"):
            envmnt = [i[1] for i in config.get("_config_files", [])]
            prefix = os.path.commonpath(envmnt)
            references = [os.path.join(os.path.basename(prefix), os.path.relpath(e, prefix)) for e in envmnt]
        else:
            references = None

        concr_scenario_prov = _create_concrete_scenario(
            concr_scenario_id, parent_scenario_id,
            gen_time=config.get("gen_time"),
            source_files=config.get("source_files"),
            references=references
        )
        prov.append(concr_scenario_prov)
    return prov
