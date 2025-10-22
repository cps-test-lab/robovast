import os
from dataclasses import asdict, is_dataclass

import yaml


def load_scenario_config(scenario_file, subsection=None):
    """Load and parse scenario variation file."""
    if not scenario_file:
        scenario_file = os.path.join(os.getcwd(), "scenario.variation")

    if not os.path.exists(scenario_file):
        print(f"Scenario variants file {scenario_file} not found")
        return None
    with open(scenario_file, 'r') as f:
        # Load all documents, the first one contains the settings
        documents = list(yaml.safe_load_all(f))
        if not documents:
            raise ValueError("No documents found in scenario file")
        config = documents[0]
        settings = config.get('settings', None)
        if settings:
            if subsection:
                return settings.get(subsection, None)
            else:
                return settings
        else:
            return None


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
