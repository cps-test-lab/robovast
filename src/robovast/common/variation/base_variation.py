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

from ..common import get_scenario_parameters
from ..config import get_validated_config

# Module-level counter for generating short, unique variant indexes.
# All variation classes can call `get_variant_index()` to obtain a new
# sequential index. Call `reset_variant_index()` to reset back to 0
# (the next `get_variant_index()` will return 1).
_variant_index = 0  # pylint: disable=invalid-name


def reset_variant_index():
    """Reset the shared variant index back to zero.

    This should be called whenever a new Variation instance (or new
    variation run) starts so generated short names begin at
    `variant1` again.
    """
    global _variant_index  # pylint: disable=global-statement
    _variant_index = 0


def get_variant_index():
    """Return the next unique variant index (1-based).

    Thread-safe.
    """
    global _variant_index  # pylint: disable=global-statement
    _variant_index += 1
    return _variant_index


class Variation():

    CONFIG_CLASS = None  # Pydantic model class for config validation
    GUI_CLASS = None  # Could be set to a GUI class for editing
    GUI_RENDERER_CLASS = None  # Could be set to a GUI renderer class

    def __init__(self, base_path, parameters, general_parameters, progress_update_callback, scenario_file, output_dir):
        # Reset shared variant index for each new Variation instance so
        # generated short names start from 1 for this variation run.
        reset_variant_index()
        self.base_path = base_path
        if self.CONFIG_CLASS is not None:
            self.parameters = get_validated_config(parameters, self.CONFIG_CLASS)
        else:
            self.parameters = parameters
        self.general_parameters = general_parameters
        self.progress_update_callback = progress_update_callback
        self.scenario_file = scenario_file
        self.output_dir = output_dir
        # Track the next index for each parent variant name
        self._variant_child_indices = {}

    def variation(self, in_variants):
        # vary in_variants and return result
        return None

    def progress_update(self, msg):
        self.progress_update_callback(f"{self.__class__.__name__}: {msg}")

    def update_variant(self, variant, scenario_values, variant_files: list = None, other_values=None):
        new_variant = copy.deepcopy(variant)

        # Ensure variant dict exists
        if 'variant' not in new_variant:
            new_variant['variant'] = {}

        # Add parameters to variant
        for key, val in scenario_values.items():
            new_variant['variant'][key] = val

        # Add other parameters to variant
        if other_values:
            for key, val in other_values.items():
                new_variant[key] = val

        # Ensure variant_files list exists
        if '_variant_files' not in new_variant:
            new_variant['_variant_files'] = []

        new_variant['_variant_files'].extend(variant_files or [])

        # Update variant name with automatic per-parent indexing
        parent_name = variant['name']
        # Automatically track index per parent variant
        if parent_name not in self._variant_child_indices:
            self._variant_child_indices[parent_name] = 1
        local_index = self._variant_child_indices[parent_name]
        self._variant_child_indices[parent_name] += 1

        new_variant['name'] = f"{parent_name}-{local_index}"
        return new_variant

    def check_scenario_parameter_reference(self, reference_name):
        """Check if a scenario parameter reference exists."""
        parameters = get_scenario_parameters(self.scenario_file)
        if not isinstance(parameters, dict) or not len(parameters) == 1:
            raise ValueError("Unexpected scenario parameters format.")

        parameters = next(iter(parameters.values()))
        for param in parameters:
            if param.get('name') == reference_name:
                return
        raise ValueError(f"Scenario parameter reference '{reference_name}' not found in scenario parameters.")
