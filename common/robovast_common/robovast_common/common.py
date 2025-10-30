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
from dataclasses import asdict, is_dataclass
import yaml


def validate_config(settings):
    """
    Validate the configuration settings.
    
    Checks that:
    - All required sections are present: execution, analysis, general, variation
    - settings["variation"] entries have a name-field that is either:
      - A string (e.g., "server_jitter_ms")
      - A dictionary with string values (e.g., {"start": "start_pose", "goals": "goal_poses"})
    
    Args:
        settings: The settings dictionary to validate
        
    Raises:
        ValueError: If validation fails
    """
    # Check for required sections
    required_sections = ["execution", "analysis", "variation"]
    missing_sections = [section for section in required_sections if section not in settings]
    
    if missing_sections:
        raise ValueError(
            f"Missing required section(s) in settings: {', '.join(missing_sections)}"
        )
    
    # Validate variation section
    variation = settings["variation"]
    if not isinstance(variation, list):
        raise ValueError("settings['variation'] must be a list")
    
    for idx, entry in enumerate(variation):
        if not isinstance(entry, dict):
            raise ValueError(f"Variation entry {idx} must be a dictionary")
        
        # Check if entry has ParameterVariationList
        if "ParameterVariationList" not in entry:
            raise ValueError(f"Variation entry {idx} must contain 'ParameterVariationList'")
        
        param_variation = entry["ParameterVariationList"]
        if not isinstance(param_variation, dict):
            raise ValueError(f"Variation entry {idx}: 'ParameterVariationList' must be a dictionary")
        
        # Check for name field
        if "name" not in param_variation:
            raise ValueError(f"Variation entry {idx}: 'ParameterVariationList' must have a 'name' field")
        
        name_field = param_variation["name"]
        
        # Validate name field is either string or dict with string values
        if isinstance(name_field, str):
            # Valid: name is a string
            pass
        elif isinstance(name_field, dict):
            # Valid if all values are strings
            for key, value in name_field.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"Variation entry {idx}: name field dictionary keys must be strings, "
                        f"got {type(key).__name__} for key '{key}'"
                    )
                if not isinstance(value, str):
                    raise ValueError(
                        f"Variation entry {idx}: name field dictionary values must be strings, "
                        f"got {type(value).__name__} for key '{key}'"
                    )
        else:
            raise ValueError(
                f"Variation entry {idx}: 'name' field must be either a string or a dictionary "
                f"with string values, got {type(name_field).__name__}"
            )


def load_config(config_file, subsection=None):
    """Load and parse scenario variation file."""
    if not config_file:
        raise ValueError("No config file provided")

    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file {config_file} not found")

    with open(config_file, 'r') as f:
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

def dataclass_representer(dumper, data):
    """Custom YAML representer for dataclass objects."""
    return dumper.represent_dict(asdict(data))

def convert_dataclasses_to_dict(obj):
    """
    Recursively convert dataclass objects to dictionaries.
    Handles nested structures including lists and dicts.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    elif isinstance(obj, dict):
        return {key: convert_dataclasses_to_dict(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_dataclasses_to_dict(item) for item in obj]
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
