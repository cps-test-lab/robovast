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


import random

import numpy as np

from .base_variation import DestinationConfig, Variation


class ParameterVariationDistributionUniformConfig(DestinationConfig):
    num_variations: int
    min: float | int | None = None
    max: float | int | None = None
    type: str = "float"
    seed: int | None = None


class ParameterVariationDistributionUniform(Variation):
    """Creates configurations with random parameter values from a uniform distribution.

    Expected parameters:

    - ``scenario`` **or** ``sim``: the destination this variation writes -- a scenario
      parameter name, or a dotted key of the simulator backend
    - ``num_variations``: Number of configurations to create
    - ``min``: Minimum value for the parameter
    - ``max``: Maximum value for the parameter
    - ``type``: Data type of the parameter (e.g., ``int``, ``float``, ``string``)
    - ``seed``: Seed for random number generation to ensure reproducibility
    """
    CONFIG_CLASS = ParameterVariationDistributionUniformConfig

    def variation(self, in_configs):
        self.progress_update("Running Parameter Variation (Random)...")

        # Extract parameters
        param_name = self.parameters.destination
        num_variations = self.parameters.num_variations
        min_val = self.parameters.min
        max_val = self.parameters.max
        value_type = self.parameters.type
        seed = self.parameters.seed

        # Validate required parameters
        if min_val is None or max_val is None:
            raise ValueError("Parameters 'min' and 'max' are required for ParameterVariationDistributionUniform")

        # Set random seed for reproducibility when one is provided
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        # If no input configs, create initial empty config
        if not in_configs or len(in_configs) == 0:
            in_configs = [{'config': {}}]

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

        # Apply each random value to all input configs (creating all combinations)
        results = []
        for config in in_configs:
            for value in random_values:
                results.append(self.update_destination(config, {param_name: value}))

        return results


class ParameterVariationDistributionGaussianConfig(DestinationConfig):
    num_variations: int
    mean: float
    std: float
    min: float | int | None = None
    max: float | int | None = None
    type: str = "float"
    seed: int


class ParameterVariationDistributionGaussian(Variation):
    """Creates configurations with random parameter values from a Gaussian (normal) distribution.

    Expected parameters:

    - ``scenario`` **or** ``sim``: the destination this variation writes -- a scenario
      parameter name, or a dotted key of the simulator backend
    - ``num_variations``: Number of configurations to create
    - ``mean``: Mean value for the parameter
    - ``std``: Standard deviation for the parameter
    - ``min``: Minimum value for the parameter
    - ``max``: Maximum value for the parameter
    - ``type``: Data type of the parameter (e.g., ``int``, ``float``, ``string``)
    - ``seed``: Seed for random number generation to ensure reproducibility
    """
    CONFIG_CLASS = ParameterVariationDistributionGaussianConfig

    def variation(self, in_configs):
        self.progress_update("Running Parameter Variation (Gaussian)...")

        # Extract parameters
        param_name = self.parameters.destination
        num_variations = self.parameters.num_variations
        mean = self.parameters.mean
        std = self.parameters.std
        min_val = self.parameters.min
        max_val = self.parameters.max
        value_type = self.parameters.type
        seed = self.parameters.seed

        # Validate required parameters
        if mean is None:
            raise ValueError("Parameter 'mean' is required for ParameterVariationDistributionGaussian")
        if std is None:
            raise ValueError("Parameter 'std' is required for ParameterVariationDistributionGaussian")
        if seed is None:
            raise ValueError("Parameter 'seed' is required for ParameterVariationDistributionGaussian")

        # Set random seed
        random.seed(seed)
        np.random.seed(seed)

        # If no input configs, create initial empty config
        if not in_configs or len(in_configs) == 0:
            in_configs = [{'config': {}}]

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

        # Apply each random value to all input configs (creating all combinations)
        results = []
        for config in in_configs:
            for value in random_values:
                results.append(self.update_destination(config, {param_name: value}))

        return results


class ParameterVariationListConfig(DestinationConfig):
    values: list[float | int | bool | dict | list | str]


class ParameterVariationList(Variation):
    """Creates configurations from a predefined list of parameter values.

    Expected parameters:

    - ``scenario`` **or** ``sim``: the destination this variation writes -- a scenario
      parameter name, or a dotted key of the simulator backend. Either may be a *list*
      of destinations for simultaneous multi-parameter variation.
    - ``values``: List of values for the parameter.  When the destination is a list,
      each entry must itself be a list of values — one per destination.

    Example (a scenario parameter):

    .. code-block:: yaml

        - ParameterVariationList:
            scenario: robot_radius
            values:
            - 0.175
            - 0.22

    Example (the simulator's world, and a value inside it):

    .. code-block:: yaml

        - ParameterVariationList:
            sim: config
            values:
            - world/depot_nav2.yaml
            - world/warehouse_nav2.yaml
        - ParameterVariationList:
            sim: plugins.floorplan.floor.friction
            values: [0.6, 1.4]

    Example (several destinations varied together):

    .. code-block:: yaml

        - ParameterVariationList:
            scenario:
            - mesh_file
            - map_file
            values:
            - - environments/office/office.stl
              - environments/office/office.yaml
            - - environments/hospital/hospital.stl
              - environments/hospital/hospital.yaml

    ``values`` includes a bare-string member, so a string-valued factor is written the
    obvious way — ``values: [world/depot.yaml, world/warehouse.yaml]``. It did not always:
    without it, ``values: ["False"]`` was *coerced* to the boolean ``False`` and handed a
    bool to a parameter the scenario declares as ``string``, which is why the nested-list
    form below used to be the only way to vary a string. With ``str`` in the union pydantic
    matches the exact type instead, so a quoted value stays a string.

    The multi-destination form is still the way to fix several values at one level without
    multiplying the configuration count:

    .. code-block:: yaml

        - ParameterVariationList:
            scenario: [sim_launch_package, sim_launch_file, headless]
            values:
            - ["nav2_bringup", "tb4_simulation_launch.py", "False"]
    """
    CONFIG_CLASS = ParameterVariationListConfig

    def variation(self, in_configs):
        self.progress_update("Running Parameter Variation (List)...")

        # Extract parameters
        param_name = self.parameters.destination
        values = self.parameters.values

        # Validate required parameters
        if not values or len(values) == 0:
            raise ValueError("Parameter 'values' must be a non-empty list for ParameterVariationList")

        # If no input configs, create initial empty config
        if not in_configs or len(in_configs) == 0:
            in_configs = [{'config': {}}]

        multi_key = isinstance(param_name, list)

        if multi_key:
            # Validate that every value entry is a list of the same length as param_name
            for entry in values:
                if not isinstance(entry, list) or len(entry) != len(param_name):
                    raise ValueError(
                        f"Each entry in 'values' must be a list of length {len(param_name)} "
                        f"to match '{self.parameters.channel}' {param_name}, got: {entry!r}"
                    )
            for entry in values:
                combo = dict(zip(param_name, entry))
                self.progress_update(f"Using values: {combo}")

            results = []
            for config in in_configs:
                for entry in values:
                    combo = dict(zip(param_name, entry))
                    results.append(self.update_destination(config, combo))
        else:
            # Single-key form (original behaviour)
            for value in values:
                self.progress_update(f"Using value: {param_name}={value}")

            results = []
            for config in in_configs:
                for value in values:
                    results.append(self.update_destination(config, {param_name: value}))

        return results
