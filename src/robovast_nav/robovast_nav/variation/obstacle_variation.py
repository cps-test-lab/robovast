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

import numpy as np
from pydantic import BaseModel, ConfigDict

from robovast.common.variation import Variation

from ..obstacle_placer import ObstaclePlacer
from ..path_generator import PathGenerator


class ObstacleConfig(BaseModel):
    amount: int
    max_distance: float
    model: str
    xacro_arguments: str


class ObstacleVariationConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    obstacle_configs: list[ObstacleConfig]
    seed: int
    robot_diameter: float


class ObstacleVariation(Variation):
    """Placement of random obstacles in the environment."""

    CONFIG_CLASS = ObstacleVariationConfig

    def variation(self, in_variants):
        self.progress_update("Running Obstacle Variation...")

        # Create a list of tasks for the thread pool
        tasks = []

        for variant in in_variants:
            tasks.append((variant, self.parameters.obstacle_configs, self.parameters.seed))

        results = []

        for task in tasks:
            result = self._generate_obstacles_for_variant(self.base_path, task[0], task[1], task[2])
            if not result:
                return []
            results.extend(result)

        return results

    def _generate_obstacles_for_variant(self, base_path, variant, obstacle_configs, seed):
        self.progress_update(f"Generating obstacles for {variant['name']}, {obstacle_configs}, {seed}...")

        np.random.seed(seed)
        resulting_variants = []
        for obstacle_config in obstacle_configs:
            if obstacle_config.amount > 0:
                max_attempts = 10
                attempt = 0
                navigable_variant_found = False

                while (
                    attempt < max_attempts
                    and not navigable_variant_found
                ):
                    attempt += 1

                    # Create obstacle placer without setting seed (use
                    # global random state)
                    placer = ObstaclePlacer()

                    waypoints = [
                        variant["variant"]["start_pose"],
                    ] + variant["variant"]["goal_poses"]

                    robot_diameter = float(self.parameters.robot_diameter)

                    map_path = os.path.join(self.output_dir,
                                            variant["floorplan_variant_path"],
                                            variant['variant']["map_file"])

                    path_generator = PathGenerator(map_path, self.parameters.robot_diameter)
                    path = path_generator.generate_path(waypoints, [])

                    try:
                        obstacle_objects = placer.place_obstacles(
                            path,
                            obstacle_config.max_distance,
                            obstacle_config.amount,
                            obstacle_config.model,
                            obstacle_config.xacro_arguments,
                            robot_diameter=robot_diameter,
                            waypoints=waypoints,
                        )
                    except Exception as e:
                        self.progress_update(f"Error placing obstacles: {e}")
                        obstacle_objects = []

                    if not obstacle_objects:
                        navigable_variant_found = True

                    # Validate navigation with the placed obstacles
                    if obstacle_objects and variant['variant']["map_file"]:
                        self.progress_update(f"Validating navigation on map {map_path} with {len(obstacle_objects)} obstacles")
                        if os.path.exists(map_path):
                            try:
                                generator = PathGenerator(
                                    map_path, robot_diameter
                                )

                                # Check if navigation is still possible
                                path = generator.generate_path(
                                    waypoints,
                                    obstacle_objects,
                                )

                                if path:
                                    # Success! Navigation is still possible
                                    navigable_variant_found = True
                                    self.progress_update(
                                        f"Successfully placed {obstacle_config.amount} obstacles for variant"
                                    )
                                else:
                                    self.progress_update(
                                        f"Try to set obstacles {
                                            attempt}/{max_attempts}: obstacles block navigation, retrying..."
                                    )

                            except Exception as e:
                                self.progress_update(
                                    f"Attempt {attempt}/{max_attempts}: validation error: {str(e)}, retrying..."
                                )
                        else:
                            raise FileNotFoundError(f"Warning: Map file not found: {map_path}")

                    # If we couldn't find a navigable configuration after
                    # all attempts
                    if not navigable_variant_found:
                        if obstacle_config.amount > 0:
                            self.progress_update(
                                f"Warning: Could not place {obstacle_config['amount']} obstacles for variant while maintaining navigation"
                            )
                            raise ValueError("Could not place obstacles while maintaining navigation")

                if obstacle_objects:
                    static_objects_parameter_name = self.parameters.name
                    result_variant = self.update_variant(variant, {
                        static_objects_parameter_name: obstacle_objects
                    })

                    resulting_variants.append(result_variant)
                else:
                    resulting_variants.append(variant)

        return resulting_variants
