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
import random

import numpy as np

from ..config import VariationConfig
from .base_variation import Variation


class ParameterVariationDistributionUniformConfig(VariationConfig):
    name: str
    num_variations: int
    min: float | int | None = None
    max: float | int | None = None
    type: str = "float"
    seed: int


class ParameterVariationDistributionUniform(Variation):
    """
    Creates variants with random parameter values from a uniform distribution.

    Expected parameters:
        name: Name of the parameter to vary
        num_variations: Number of random variants to generate
        min: Minimum value (inclusive)
        max: Maximum value (inclusive)
        type: Type to convert values to ('string', 'int', 'float', 'bool')
        seed: Random seed for reproducibility
    """
    CONFIG_CLASS = ParameterVariationDistributionUniformConfig

    def variation(self, in_variants):
        self.progress_update("Running Parameter Variation (Random)...")

        # Extract parameters
        param_name = self.parameters.get("name")
        num_variations = self.parameters.get("num_variations", 1)
        min_val = self.parameters.get("min")
        max_val = self.parameters.get("max")
        value_type = self.parameters.get("type", "float")
        seed = self.parameters.get("seed")

        # Validate required parameters
        if not param_name:
            raise ValueError("Parameter 'name' is required for ParameterVariationDistributionUniform")
        if min_val is None or max_val is None:
            raise ValueError("Parameters 'min' and 'max' are required for ParameterVariationDistributionUniform")
        if seed is None:
            raise ValueError("Parameter 'seed' is required for ParameterVariationDistributionUniform")

        # Set random seed
        random.seed(seed)
        np.random.seed(seed)

        # If no input variants, create initial empty variant
        if not in_variants or len(in_variants) == 0:
            in_variants = [{'variant': {}}]

        # Generate random parameter values once
        random_values = []
        for _ in range(num_variations):
            # Generate random value
            if value_type in ['int', 'integer']:
                value = random.randint(int(min_val), int(max_val))
            elif value_type in ['float', 'double', 'number']:
                value = random.uniform(float(min_val), float(max_val))
            elif value_type == 'bool':
                # For bool, min/max are interpreted as probabilities
                value = random.random() < float(max_val)
            else:  # default to string
                # Generate random number and convert to string
                if isinstance(min_val, int) and isinstance(max_val, int):
                    value = str(random.randint(int(min_val), int(max_val)))
                else:
                    value = str(random.uniform(float(min_val), float(max_val)))

            random_values.append(value)
            self.progress_update(f"Generated random value: {param_name}={value}")

        # Apply each random value to all input variants (creating all combinations)
        results = []
        for value in random_values:
            for variant in in_variants:
                new_variant = copy.deepcopy(variant)

                # Ensure variant dict exists
                if 'variant' not in new_variant:
                    new_variant['variant'] = {}

                # Add parameter to variant
                new_variant['variant'][param_name] = value
                new_variant['name'] = self.get_variant_name()

                results.append(new_variant)

        return results


class ParameterVariationDistributionGaussianConfig(VariationConfig):
    name: str
    num_variations: int
    mean: float
    std: float
    min: float | int | None = None
    max: float | int | None = None
    type: str = "float"
    seed: int


class ParameterVariationDistributionGaussian(Variation):
    """
    Creates variants with random parameter values from a Gaussian (normal) distribution.

    Expected parameters:
        name: Name of the parameter to vary
        num_variations: Number of random variants to generate
        mean: Mean (mu) of the Gaussian distribution
        std: Standard deviation (sigma) of the Gaussian distribution
        min: Minimum value (optional, clips values below this)
        max: Maximum value (optional, clips values above this)
        type: Type to convert values to ('string', 'int', 'float', 'bool')
        seed: Random seed for reproducibility
    """
    CONFIG_CLASS = ParameterVariationDistributionGaussianConfig

    def variation(self, in_variants):
        self.progress_update("Running Parameter Variation (Gaussian)...")

        # Extract parameters
        param_name = self.parameters.name
        num_variations = self.parameters.num_variations
        mean = self.parameters.mean
        std = self.parameters.std
        min_val = self.parameters.min
        max_val = self.parameters.max
        value_type = self.parameters.type
        seed = self.parameters.seed

        # Validate required parameters
        if not param_name:
            raise ValueError("Parameter 'name' is required for ParameterVariationDistributionGaussian")
        if mean is None:
            raise ValueError("Parameter 'mean' is required for ParameterVariationDistributionGaussian")
        if std is None:
            raise ValueError("Parameter 'std' is required for ParameterVariationDistributionGaussian")
        if seed is None:
            raise ValueError("Parameter 'seed' is required for ParameterVariationDistributionGaussian")

        # Set random seed
        random.seed(seed)
        np.random.seed(seed)

        # If no input variants, create initial empty variant
        if not in_variants or len(in_variants) == 0:
            in_variants = [{'variant': {}}]

        # Generate Gaussian distributed parameter values
        random_values = []
        for _ in range(num_variations):
            # Generate Gaussian random value
            value = np.random.normal(float(mean), float(std))

            # Apply clipping if min/max are specified
            if min_val is not None:
                value = max(value, float(min_val))
            if max_val is not None:
                value = min(value, float(max_val))

            # Convert to appropriate type
            if value_type in ['int', 'integer']:
                value = int(round(value))
            elif value_type in ['float', 'double', 'number']:
                value = float(value)
            elif value_type == 'bool':
                # For bool, convert based on threshold at mean
                value = value >= float(mean)
            else:  # default to string
                value = str(value)

            random_values.append(value)
            self.progress_update(f"Generated Gaussian value: {param_name}={value}")

        # Apply each random value to all input variants (creating all combinations)
        results = []
        for value in random_values:
            for variant in in_variants:
                new_variant = copy.deepcopy(variant)

                # Ensure variant dict exists
                if 'variant' not in new_variant:
                    new_variant['variant'] = {}

                # Add parameter to variant
                new_variant['variant'][param_name] = value
                new_variant['name'] = self.get_variant_name()

                results.append(new_variant)

        return results


class ParameterVariationListConfig(VariationConfig):
    name: str
    values: list[float | int | bool | dict | list]


class ParameterVariationList(Variation):
    """
    Creates variants with parameter values from a predefined list.

    Expected parameters:
        name: Name of the parameter to vary
        values: List of values to use for the parameter
    """
    CONFIG_CLASS = ParameterVariationListConfig

    def variation(self, in_variants):
        self.progress_update("Running Parameter Variation (List)...")

        # Extract parameters
        param_name = self.parameters.name
        values = self.parameters.values

        # Validate required parameters
        if not param_name:
            raise ValueError("Parameter 'name' is required for ParameterVariationList")
        if not values or len(values) == 0:
            raise ValueError("Parameter 'values' must be a non-empty list for ParameterVariationList")

        # If no input variants, create initial empty variant
        if not in_variants or len(in_variants) == 0:
            in_variants = [{'variant': {}}]

        # Log each value that will be used
        for value in values:
            self.progress_update(f"Using value: {param_name}={value}")

        # Apply each value to all input variants (creating all combinations)
        results = []
        for value in values:
            for variant in in_variants:
                results.append(self.update_variant(variant, {param_name: value}))

        return results
