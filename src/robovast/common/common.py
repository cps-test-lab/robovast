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


def load_config(config_file, subsection=None):
    """Load and parse scenario variation file."""
    if not config_file:
        raise ValueError("No config file provided")

    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file {config_file} not found")

    with open(config_file, 'r') as f:
        try:
            # Load all documents, the first one contains the settings
            documents = list(yaml.safe_load_all(f))
            if not documents:
                raise ValueError("No documents found in scenario file")
            config = documents[0]
            settings = config.get('settings', None)
            if settings:
                # Validate the configuration
                validate_config(settings)

                if subsection:
                    subsection = settings.get(subsection, None)
                    if not subsection:
                        raise ValueError(f"No subsection '{subsection}' found in settings")
                    return subsection
                else:
                    return settings
            else:
                raise ValueError("No 'settings' section found in scenario file")
        except yaml.YAMLError as e:
            print(f"Error parsing YAML file: {e}")
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


def save_scenario_variants_file(variants, output_file):
    # Ensure the directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Create the complete data structure with variants and settings
    data_to_save = []

    for variant_data in variants:
        # Convert dataclasses to dicts automatically
        converted_variant = convert_dataclasses_to_dict(variant_data)
        converted_variant.pop('path', None)
        data_to_save.append(converted_variant)

    # Write settings at the top, then variants
    with open(output_file, "w") as f:
        # Write each variant as a separate YAML document
        for idx, variant_dict in enumerate(data_to_save):
            yaml.dump(variant_dict, f, default_flow_style=False)
            # separate documents with '---'
            if idx < len(data_to_save) - 1:
                f.write("---\n")


def save_scenario_variant_file(variant, output_file):
    # Ensure the directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Create the complete data structure with variants and settings
    data_to_save = []

    # Convert dataclasses to dicts automatically
    converted_variant = convert_dataclasses_to_dict(variant)
    data_to_save.append(converted_variant)

    with open(output_file, "w") as f:
        yaml.dump(data_to_save, f, default_flow_style=False)


def filter_variants(variants):
    """Parse YAML variants file and filter out keys starting with underscore.

    Args:
        variants_file: Path to the variants YAML file

    Returns:
        list: List of filtered variant documents
    """
    # Process each document
    filtered_documents = []
    for variants_data in variants:
        # Filter out keys starting with "_"
        if isinstance(variants_data, list):
            filtered_variants = []
            for variant in variants_data:
                if isinstance(variant, dict):
                    filtered_variant = {k: v for k, v in variant.items() if not k.startswith("_")}
                    filtered_variants.append(filtered_variant)
                else:
                    filtered_variants.append(variant)
            filtered_documents.append(filtered_variants)
        elif isinstance(variants_data, dict):
            filtered_variant = {k: v for k, v in variants_data.items() if not k.startswith("_")}
            filtered_documents.append(filtered_variant)
        else:
            filtered_documents.append(variants_data)

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
