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

import logging
import os
import pickle
import sys
from dataclasses import asdict, is_dataclass

import numpy as np
import yaml
from scenario_execution import \
    get_scenario_parameters as _external_get_scenario_parameters

from .config import validate_config
from .file_cache import FileCache

logger = logging.getLogger(__name__)


def load_config(config_file, subsection=None, allow_missing=False):
    """Load and parse scenario variation file.
    
    Args:
        config_file: Path to the configuration file
        subsection: Optional subsection to extract
        allow_missing: If True, return empty dict when subsection is missing instead of raising error
    
    Returns:
        Configuration dict or subsection dict
    """
    logger.debug(f"Loading config file: {config_file}")
    if not config_file:
        logger.error("No config file provided")
        raise ValueError("No config file provided")

    if not os.path.exists(config_file):
        logger.error(f"Config file {config_file} not found")
        raise FileNotFoundError(f"Config file {config_file} not found")

    with open(config_file, 'r') as f:
        try:
            # Load all documents, the first one contains the config
            documents = list(yaml.safe_load_all(f))
            if not documents:
                logger.error("No documents found in scenario file")
                raise ValueError("No documents found in scenario file")
            config = documents[0]

            # Validate the configuration
            validate_config(config)

            if subsection:
                subsection_data = config.get(subsection, None)
                if not subsection_data:
                    if allow_missing:
                        logger.debug(f"No subsection '{subsection}' found in configuration, returning empty dict")
                        return {}
                    else:
                        logger.error(f"No subsection '{subsection}' found in configuration")
                        raise ValueError(f"No subsection '{subsection}' found in configuration")
                logger.debug(f"Successfully loaded config subsection: {subsection}")
                return subsection_data
            else:
                logger.debug(f"Successfully loaded config file: {config_file}")
                return config
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML file: {e}")
            sys.exit(1)


def dataclass_representer(dumper, data):
    """Custom YAML representer for dataclass objects."""
    return dumper.represent_dict(asdict(data))


def convert_dataclasses_to_dict(obj):  # pylint: disable=too-many-return-statements
    """
    Recursively convert dataclass objects to dictionaries.
    Handles nested structures including lists, tuples, and dicts.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    elif isinstance(obj, dict):
        return {key: convert_dataclasses_to_dict(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_dataclasses_to_dict(item) for item in obj]
    elif isinstance(obj, tuple):
        # Convert tuples to lists and recursively process elements
        return [convert_dataclasses_to_dict(item) for item in obj]
    elif isinstance(obj, np.ndarray):
        # Convert numpy arrays to lists and recursively process elements
        return [convert_dataclasses_to_dict(item) for item in obj.tolist()]
    elif isinstance(obj, (np.integer, np.floating)):
        # Convert numpy scalars to Python native types
        return obj.item()
    elif isinstance(obj, np.bool_):
        # Convert numpy bool to Python bool
        return bool(obj)
    else:
        return obj


def save_scenario_configs_file(configs, output_file):
    # Ensure the directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Create the complete data structure with configs and settings
    data_to_save = []

    for config_data in configs:
        # Convert dataclasses to dicts automatically
        converted_config = convert_dataclasses_to_dict(config_data)
        converted_config.pop('path', None)
        data_to_save.append(converted_config)

    # Write settings at the top, then configurations
    with open(output_file, "w") as f:
        # Write each config as a separate YAML document
        for idx, config_dict in enumerate(data_to_save):
            yaml.dump(config_dict, f, default_flow_style=False)
            # separate documents with '---'
            if idx < len(data_to_save) - 1:
                f.write("---\n")


def filter_configs(configs):
    """Parse YAML config file and filter out keys starting with underscore.

    Args:
        configs: List of configuration dictionaries

    Returns:
        list: List of filtered configuration documents
    """
    # Process each document
    filtered_documents = []
    for configs_data in configs:
        # Filter out keys starting with "_"
        if isinstance(configs_data, list):
            filtered_configs = []
            for config in configs_data:
                if isinstance(config, dict):
                    filtered_config = {k: v for k, v in config.items() if not k.startswith("_")}
                    filtered_configs.append(filtered_config)
                else:
                    filtered_configs.append(config)
            filtered_documents.append(filtered_configs)
        elif isinstance(configs_data, dict):
            filtered_config = {k: v for k, v in configs_data.items() if not k.startswith("_")}
            filtered_documents.append(filtered_config)
        else:
            filtered_documents.append(configs_data)

    return filtered_documents


def get_scenario_parameters(scenario_file):
    """Get scenario parameters from scenario file.

    Args:
        scenario_file: Path to the scenario file
    """
    file_cache = FileCache(os.path.dirname(scenario_file), "robovast_scenario_parameters_" +
                           os.path.basename(scenario_file).replace("/", "_").replace(".", "_"), [])

    cached_params = file_cache.get_cached_file([scenario_file], binary=True, content=True)
    if cached_params:
        return pickle.loads(cached_params)
    else:
        params = _external_get_scenario_parameters(scenario_file)
        file_cache.save_file_to_cache([scenario_file], pickle.dumps(params), content=True, binary=True)
        return params


def is_scenario_parameter(value, scenario_file):
    params_dict = get_scenario_parameters(scenario_file)
    if params_dict:
        scenario_parameters = next(iter(params_dict.values()))
        for val in scenario_parameters:
            if value == val["name"]:
                return True
    return False
