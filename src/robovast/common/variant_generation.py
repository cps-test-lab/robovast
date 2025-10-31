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
import os
import tempfile
import yaml
from importlib.metadata import entry_points

from .common import load_config, save_scenario_variants_file


def progress_update(msg):
    print(msg)

def execute_variation(base_dir, variants, variation_class, parameters, general_parameters, progress_update_callback, output_dir=None):
    variation = variation_class(base_dir, parameters, general_parameters, progress_update_callback, output_dir)
    try:
        variants = variation.variation(copy.deepcopy(variants))
    except Exception as e:
        progress_update_callback(f"Variation failed. {variation_class.__name__}: {e}")
        return []

    # Check if variants is None and return empty list
    if variants is None:
        progress_update_callback(f"Variation failed. {variation_class.__name__}: No variants returned")
        return []

    # progress_update(f"Current variants {variants}")
    return variants

def _read_variation_classes_from_file(variation_file): # pylint: disable=too-many-return-statements
    """
    Read variation class names from the variation file settings.

    Returns a list of variation class objects in the order they appear in the file.
    Reads from settings.variation list.
    """
    if not os.path.exists(variation_file):
        return []

    with open(variation_file, 'r') as f:
        try:
            # Load YAML document
            documents = list(yaml.safe_load_all(f))
            if not documents:
                return []

            config = documents[0]
            settings = config.get('settings', {})

            if not settings:
                return []

            # Get the variation list from settings
            variation_list = settings.get('variation', [])

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
                            print(f"Warning: Unknown variation class '{class_name}' found in variation file")

            return variation_classes

        except yaml.YAMLError as e:
            print(f"Error parsing variation file: {e}")
            return []
        except Exception as e:
            print(f"Error reading variation classes from file: {e}")
            return []


def generate_scenario_variations(variation_file, progress_update_callback, variation_classes=None, output_dir=None):
    progress_update_callback("Start generating variants.")

    parameters = load_config(variation_file)

    if output_dir is None:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_variation_")
        output_dir = temp_path.name

    if variation_classes is None:
        # Read variation classes from the variation file
        variation_classes_and_parameters = _read_variation_classes_from_file(variation_file)
    else:
        raise NotImplementedError("Passing variation_classes is not implemented yet")

    general_parameters = parameters.get('general', {})
    variants = []
    for variation_class, parameters in variation_classes_and_parameters:
        result = execute_variation(os.path.dirname(variation_file), variants, variation_class,
                                   parameters, general_parameters, progress_update_callback, output_dir)
        if result is None or len(result) == 0:
            # If a variation step fails or produces no results, stop the pipeline
            progress_update_callback(f"Variation pipeline stopped at {variation_class.__name__} - no variants to process")
            variants = []
            break
        variants = result

    if variants:
        save_scenario_variants_file(variants, os.path.join(output_dir, 'scenario.variants'))

    return variants
