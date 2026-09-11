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
from typing import Optional

from robovast.common import is_scenario_parameter
from robovast.common.variation import Variation

from ..data_model import Pose

logger = logging.getLogger(__name__)


class NavVariation(Variation):

    def resolve_input_paths(self, paths) -> list:
        """Resolve relative input paths against ``base_path``.

        Paths in the ``.vast`` configuration are relative to the configuration
        file's directory, not the current working directory.  Resolving them
        against ``self.base_path`` ensures variations work regardless of where
        the ``vast`` command is invoked from.  Absolute paths are returned
        unchanged.
        """
        return [
            p if os.path.isabs(p) else os.path.join(self.base_path, p)
            for p in paths
        ]

    def collect_input_files(self, paths) -> list:
        """Expand input ``paths`` into individual files relative to ``base_path``.

        Accepts files and directories (resolved via :meth:`resolve_input_paths`)
        and returns the contained files as paths relative to ``base_path``,
        suitable for :meth:`get_input_files` so they are copied into the
        campaign ``_config/`` directory for self-contained cluster runs.
        Files outside ``base_path`` are skipped, since they cannot be packaged
        relative to the campaign.
        """
        collected = []
        for resolved in self.resolve_input_paths(paths):
            if os.path.isdir(resolved):
                file_paths = (
                    os.path.join(root, name)
                    for root, _, names in os.walk(resolved)
                    for name in names
                )
            elif os.path.isfile(resolved):
                file_paths = [resolved]
            else:
                logger.warning("Input path not found, skipping: %s", resolved)
                continue
            for file_path in file_paths:
                rel = os.path.relpath(file_path, self.base_path)
                if rel.startswith(".."):
                    logger.warning(
                        "Input file outside base directory, skipping: %s", file_path
                    )
                    continue
                collected.append(rel)
        return collected

    def get_map_file(self, map_file_parameter, config) -> Optional[str]:
        """Determine the map file path to use for this config.

        The map file can be specified in two ways:
        1. As a YAML parameter (map_file_parameter)
        2. Automatically from another variation via _map_file in config

        If both are defined, an error is raised.
        """

        map_file_path = None
        map_file_from_yaml = None
        map_file_from_variation = None

        # Check if map file is provided via YAML parameter.
        # Only fall back to config["config"]["map_file"] when no previous variation has
        # already resolved the map file (i.e. _map_file is not set).  If _map_file is
        # present it was placed there by a variation such as FloorplanGeneration and the
        # relative path stored in config["config"]["map_file"] is not meaningful relative
        # to base_path, so using it would cause a spurious "not a valid scenario parameter
        # reference" error.
        if not map_file_parameter and "_map_file" not in config and "map_file" in config.get("config", {}):
            # Fall back to map_file from configuration parameters block
            map_file_parameter = config["config"]["map_file"]
            self.progress_update(f"Using map_file from configuration parameters: {map_file_parameter}")

        if map_file_parameter:
            temp_path = os.path.join(self.base_path, map_file_parameter)
            if os.path.exists(temp_path):
                # 1.1. found map file directly
                self.progress_update(f"Using map file from YAML configuration: {temp_path}")
                map_file_from_yaml = temp_path
            else:
                # 2. try to resolve from scenario parameter
                self.progress_update(f"Map file {map_file_parameter} does not exist. Using it as scenario parameter reference.")
                if not is_scenario_parameter(map_file_parameter, self.scenario_file):
                    raise ValueError(f"Map file {map_file_parameter} is not a valid scenario parameter reference.")
                if map_file_parameter in config["config"]:
                    temp_path = os.path.join(config["config"][map_file_parameter])
                    if os.path.exists(temp_path):
                        self.progress_update(f"Resolved map file path from scenario parameter: {temp_path}")
                        map_file_from_yaml = temp_path
                    else:
                        raise FileNotFoundError(f"Resolved map file path from scenario parameter does not exist: {temp_path}")

        # Check if map file is provided from another variation
        if "_map_file" in config:
            temp_path = config["_map_file"]
            if os.path.exists(temp_path):
                self.progress_update(f"Found map file from previous variation (config._map_file): {temp_path}")
                map_file_from_variation = temp_path
            else:
                raise FileNotFoundError(f"Map file from config data does not exist: {temp_path}")

        # Validate that both methods are not used simultaneously
        if map_file_from_yaml and map_file_from_variation:
            raise ValueError(
                f"Map file is defined both in YAML parameter ({map_file_from_yaml}) "
                f"and from another variation ({map_file_from_variation}). "
                f"Please use only one method to specify the map file."
            )

        # Use whichever method provided the map file
        if map_file_from_yaml:
            map_file_path = map_file_from_yaml
        elif map_file_from_variation:
            self.progress_update(f"Using map file from previous variation: {map_file_from_variation}")
            map_file_path = map_file_from_variation
        else:
            raise ValueError(
                "No valid map file path could be determined. Please specify map_file in the YAML configuration or ensure a previous variation provides it.")

        return map_file_path

    def get_waypoints(self, config) -> list:
        """The trial's waypoints -- start first, then every goal -- as :class:`Pose` objects.

        Read through the ``start`` and ``goal`` input slots, so the parameter names are the ones
        the campaign bound rather than ones this code assumed. A campaign that binds its path
        variation to ``scenario: {start: robot_start}`` binds the consumer's ``reads:`` to the
        same name, and both halves stay one statement in the ``.vast``.

        Whichever wrote them, they arrive here as :class:`Pose`: a campaign stating poses in its
        ``parameters:`` block leaves YAML mappings, a variation that generated them leaves the
        dataclass, and :meth:`Pose.from_any` flattens that difference so a caller never asks which.

        The ``goal`` slot may be bound to a parameter holding one pose or a list of them -- the
        scenario file decides which it declares -- and both give a list here.

        Read-only: the poses are returned, never written back into *config*. What spelling the
        scenario declares is the scenario's business, and a resolver that normalised the config
        in passing would have to decide whether to leave ``goal_pose`` or ``goal_poses`` behind,
        with an OSC that rejects an undeclared parameter waiting on the other side.
        """
        values = {}
        for slot in ("start", "goal"):
            name = self.parameters.input_binding(slot)
            value = config.get("config", {}).get(name)
            if not value:
                raise ValueError(
                    f"Config '{config['name']}': '{name}', bound to this variation's '{slot}', "
                    f"holds no pose. Either state it in 'parameters.scenario', or run a "
                    f"variation that writes it (such as PathVariationRandom) ahead of this one.")
            values[slot] = value

        goals = values["goal"] if isinstance(values["goal"], list) else [values["goal"]]
        return [Pose.from_any(values["start"])] + [Pose.from_any(g) for g in goals]
