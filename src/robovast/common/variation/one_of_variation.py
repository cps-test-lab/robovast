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
import logging
from importlib.metadata import entry_points
from typing import Any

from ..config import VariationConfig
from .base_variation import Variation

logger = logging.getLogger(__name__)


class OneOfVariationConfig(VariationConfig):
    """Configuration for OneOfVariation.

    ``variations`` is a list of fully-configured variation entries, each
    formatted exactly like an entry in the top-level ``variations:`` list::

        - OneOfVariation:
            variations:
              - ObstacleVariation:
                  name: static_objects
                  seed: 42
              - ObstacleVariationWithDistanceTrigger:
                  name: dynamic_objects
                  seed: 42
    """

    variations: list[dict[str, Any]]


class OneOfVariation(Variation):
    """Branches the config pipeline by running each child variation independently.

    Each child variation receives its own deep copy of ``in_configs`` and
    produces a branch of output configs.  All branches are concatenated into
    a single flat list, so the result is::

        output = branch_1 + branch_2 + ... + branch_N

    This is the "one of N alternatives" semantic: the downstream pipeline
    sees every possible alternative as a separate configuration.

    Child variations are specified with their full type name and parameters,
    mirroring the top-level ``variations:`` list syntax:

    .. code-block:: yaml

        - OneOfVariation:
            variations:
              - ParameterVariationList:
                  name: my_param
                  values: [1, 2]
              - ParameterVariationList:
                  name: my_param
                  values: [3, 4]
    """

    CONFIG_CLASS = OneOfVariationConfig

    def variation(self, in_configs):
        self.progress_update(f"Running OneOfVariation with {len(self.parameters.variations)} children ...")

        # Resolve available variation classes from entry points (same mechanism
        # as _get_variation_classes in config_generation.py).
        available_classes: dict[str, type] = {}
        try:
            eps = entry_points(group="robovast.variation_types")
            for ep in eps:
                try:
                    available_classes[ep.name] = ep.load()
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning("Failed to load variation plugin '%s': %s", ep.name, e)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Failed to enumerate variation plugins: %s", e)

        results = []

        for child_entry in self.parameters.variations:
            if not isinstance(child_entry, dict) or len(child_entry) != 1:
                raise ValueError(
                    "Each entry in OneOfVariation.variations must be a single-key dict "
                    f"{{TypeName: {{params}}}}, got: {child_entry!r}"
                )

            type_name, child_params = next(iter(child_entry.items()))

            if type_name not in available_classes:
                raise ValueError(
                    f"Unknown variation type '{type_name}' in OneOfVariation. "
                    f"Available types: {', '.join(sorted(available_classes.keys()))}"
                )

            # child_params may be None for parameter-less child variations.
            if child_params is None:
                child_params = {}

            self.progress_update(f"Running child variation: {type_name}")

            child_class = available_classes[type_name]
            # Note: Variation.__init__ calls reset_config_index() which resets the
            # shared module-level counter.  This is harmless because all built-in
            # variations use the per-instance _config_child_indices dict for naming,
            # not the module-level counter.
            child = child_class(
                self.base_path,
                child_params,
                self.general_parameters,
                self.progress_update_callback,
                self.scenario_file,
                self.output_dir,
            )

            branch = child.variation(copy.deepcopy(in_configs))
            if branch:
                results.extend(branch)

        self.progress_update(f"OneOfVariation produced {len(results)} configs from {len(self.parameters.variations)} branches.")
        return results
