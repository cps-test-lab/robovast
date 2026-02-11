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

from typing import Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator

from robovast.common import convert_dataclasses_to_dict
from robovast.common.variation import VariationGuiRenderer

from ..gui.navigation_gui import NavigationGui
from ..obstacle_placer import ObstaclePlacer
from .nav_base_variation import NavVariation


class ObstacleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
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
    map_file: Optional[str] = None
    count: int = 1

    @field_validator('seed')
    @classmethod
    def validate_seed(cls, v):
        if v is None:
            raise ValueError('seed is required and cannot be None')
        return v

    @field_validator('robot_diameter')
    @classmethod
    def validate_robot_diameter(cls, v):
        if v <= 0.:
            raise ValueError('robot_diameter is required and cannot be None')
        return v


class ObstacleVariationGuiRenderer(VariationGuiRenderer):

    def update_gui(self, config, path):
        for obstacle in config["config"].get('static_objects', []):
            shape = None
            if "box" in obstacle["model"]:
                shape = "box"
            elif "cylinder" in obstacle["model"]:
                shape = "cylinder"
            if shape == 'box':
                pose = obstacle['spawn_pose']
                x = pose['position']['x']
                y = pose['position']['y']
                yaw = pose['orientation']['yaw']
                self.gui_object.draw_obstacle(x, y, {'width': 1, 'length': 1, 'height': 1}, yaw, shape=shape)


class ObstacleVariation(NavVariation):
    """Placement of random obstacles in the environment."""

    CONFIG_CLASS = ObstacleVariationConfig
    GUI_CLASS = NavigationGui
    GUI_RENDERER_CLASS = ObstacleVariationGuiRenderer

    def variation(self, in_configs):
        self.progress_update("Running Obstacle Variation...")

        results = []
        for config in in_configs:
            np.random.seed(self.parameters.seed)
            for _ in range(self.parameters.count):
                result = self._generate_obstacles_for_config(self.base_path, config, self.parameters.obstacle_configs)
                results.extend(result)
        return results

    def _generate_obstacles_for_config(self, base_path, config, obstacle_configs):
        resulting_configs = []

        placer = ObstaclePlacer()

        try:
            map_file_path = self.get_map_file(self.parameters.map_file, config)
        except Exception as e:  # pylint: disable=broad-except
            raise ValueError(f"Error determining map file for config {config['name']}: {e}") from e

        # Initialize obstacle_objects as an empty list to accumulate all obstacles
        obstacle_objects = []

        if 'start_pose' not in config['config'] or 'goal_poses' not in config['config']:
            self.progress_update("start_pose and/or goal_poses not defined in config, placing obstacles randomly (idependent of path)...")

            for obstacle_config in obstacle_configs:
                # Accumulate obstacles from each obstacle_config
                obstacles = placer.place_obstacles_random(
                    map_file_path,
                    obstacle_config.amount,
                    obstacle_config.model,
                    obstacle_config.xacro_arguments,
                )
                obstacle_objects.extend(obstacles)
        else:
            raise NotImplementedError("Path-dependent obstacle placement is not implemented yet.")
            # waypoints = [
            #     config["config"]["start_pose"],
            # ] + config["config"]["goal_poses"]

            # path_generator = PathGenerator(map_file_path, self.parameters.robot_diameter)
            # path = path_generator.generate_path(waypoints, [])

            # for obstacle_config in obstacle_configs:
            #     if obstacle_config.amount > 0:
            #         max_attempts = 10
            #         attempt = 0
            #         navigable_config_found = False

            #         while (
            #             attempt < max_attempts
            #             and not navigable_config_found
            #         ):
            #             attempt += 1

            #             try:
            #                 obstacle_objects = placer.place_obstacles(
            #                     path,
            #                     obstacle_config.max_distance,
            #                     obstacle_config.amount,
            #                     obstacle_config.model,
            #                     obstacle_config.xacro_arguments,
            #                     robot_diameter=self.parameters.robot_diameter,
            #                     waypoints=waypoints,
            #                 )
            #             except Exception as e:
            #                 self.progress_update(f"Error placing obstacles: {e}")
            #                 obstacle_objects = []

            #             if not obstacle_objects:
            #                 navigable_config_found = True

            #             # Validate navigation with the placed obstacles
            #             if obstacle_objects and config['config']["map_file"]:
            #                 self.progress_update(f"Validating navigation on map {map_path} with {len(obstacle_objects)} obstacles")
            #                 if os.path.exists(map_path):
            #                     try:
            #                         generator = PathGenerator(
            #                             map_path, self.parameters.robot_diameter
            #                         )

            #                         # Check if navigation is still possible
            #                         path = generator.generate_path(
            #                             waypoints,
            #                             obstacle_objects,
            #                         )

            #                         if path:
            #                             # Success! Navigation is still possible
            #                             navigable_config_found = True
            #                             self.progress_update(
            #                                 f"Successfully placed {obstacle_config.amount} obstacles for config"
            #                             )
            #                         else:
            #                             self.progress_update(
            #                                 f"Try to set obstacles {
            #                                     attempt}/{max_attempts}: obstacles block navigation, retrying..."
            #                             )

            #                     except Exception as e:
            #                         self.progress_update(
            #                             f"Attempt {attempt}/{max_attempts}: validation error: {str(e)}, retrying..."
            #                         )
            #                 else:
            #                     raise FileNotFoundError(f"Warning: Map file not found: {map_path}")

            #             # If we couldn't find a navigable configuration after
            #             # all attempts
            #             if not navigable_config_found:
            #                 if obstacle_config.amount > 0:
            #                     self.progress_update(
            #                         f"Warning: Could not place {obstacle_config['amount']
            #                                                     } obstacles for config while maintaining navigation"
            #                     )
            #                     raise ValueError("Could not place obstacles while maintaining navigation")

        # Always create variation with parameter, even if obstacle_objects is empty
        # This ensures consistent naming and parameters in scenario.config
        static_objects_parameter_name = self.parameters.name
        result_config = self.update_config(config, {
            static_objects_parameter_name: convert_dataclasses_to_dict(obstacle_objects) if obstacle_objects else []
        })

        resulting_configs.append(result_config)

        return resulting_configs
