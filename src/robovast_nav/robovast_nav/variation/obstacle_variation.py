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
import os
from pydantic import BaseModel, ConfigDict, field_validator

from robovast.common import convert_dataclasses_to_dict
from robovast.common.variation import VariationGuiRenderer

from ..gui.navigation_gui import NavigationGui
from ..obstacle_placer import ObstaclePlacer
from .nav_base_variation import NavVariation
from ..path_generator import PathGenerator
from ..object_shapes import get_obstacle_dimensions, get_object_type_from_model_path


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
            # Get model path and determine shape
            model_path = obstacle.get('model', '')
            object_type = get_object_type_from_model_path(model_path)
            
            if object_type == 'cylinder':
                shape = 'circle'
            else:
                shape = 'box'
            
            # Parse xacro_arguments to get actual dimensions
            xacro_args_str = obstacle.get('xacro_arguments', '')
            dimensions = get_obstacle_dimensions(xacro_args_str)
            
            # Prepare draw_args based on object type
            if object_type == 'cylinder':
                # For cylinder, use diameter
                radius = dimensions.get('radius', 0.25)
                draw_args = {'diameter': radius * 2}
            else:
                # Default to box with parsed dimensions
                draw_args = {
                    'width': dimensions.get('width', 0.5),
                    'length': dimensions.get('length', 0.5)
                }
            
            pose = obstacle['spawn_pose']
            x = pose['position']['x']
            y = pose['position']['y']
            yaw = pose['orientation']['yaw']
            self.gui_object.draw_obstacle(x, y, draw_args, yaw, shape=shape)


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

        # Get start and goal poses from config (set by previous variations)
        start_pose = config['config'].get('start_pose')
        goal_poses = config['config'].get('goal_poses', [])
        goal_pose = config['config'].get('goal_pose')
        
        # Handle both goal_pose (singular, from PathVariationRandom) and goal_poses (plural)
        if goal_pose and not goal_poses:
            goal_poses = [goal_pose]
        
        if not start_pose or not goal_poses:
            raise ValueError(
                f"start_pose and goal_pose(s) are required for path-dependent obstacle placement. "
                f"Config '{config['name']}' missing: "
                f"{'start_pose ' if not start_pose else ''}"
                f"{'goal_pose(s) ' if not goal_poses else ''}"
                f"Make sure a path variation (like PathVariationRandom) runs before ObstacleVariation."
            )

        self.progress_update(f"Placing obstacles along path from start_pose to {len(goal_poses)} goal_pose(s)...")

        waypoints = [start_pose] + goal_poses

        # Check if path is already available from previous variation
        if '_path' in config:
            path = config['_path']
            self.progress_update("Using pre-generated path from previous variation")
        else:
            # Generate path if not available
            path_generator = PathGenerator(map_file_path, self.parameters.robot_diameter)
            path = path_generator.generate_path(waypoints, [])
            self.progress_update("Generated new path for obstacle placement")

        obstacle_objects = []
        for obstacle_config in obstacle_configs:
            if obstacle_config.amount > 0:
                max_attempts = 10
                attempt = 0
                navigable_config_found = False

                while (
                    attempt < max_attempts
                    and not navigable_config_found
                ):
                    attempt += 1

                    try:
                        obstacle_objects = placer.place_obstacles(
                            path,
                            obstacle_config.max_distance,
                            obstacle_config.amount,
                            obstacle_config.model,
                            obstacle_config.xacro_arguments,
                            robot_diameter=self.parameters.robot_diameter,
                            waypoints=waypoints,
                        )
                    except Exception as e:
                        self.progress_update(f"Error placing obstacles: {e}")
                        obstacle_objects = []

                    if not obstacle_objects:
                        navigable_config_found = True
                    else:
                        # Validate navigation with the placed obstacles
                        self.progress_update(f"Validating navigation on map {map_file_path} with {len(obstacle_objects)} obstacles")
                        if os.path.exists(map_file_path):
                            try:
                                generator = PathGenerator(
                                    map_file_path, self.parameters.robot_diameter
                                )

                                # Check if navigation is still possible
                                validation_path = generator.generate_path(
                                    waypoints,
                                    obstacle_objects,
                                )

                                if validation_path:
                                    # Success! Navigation is still possible
                                    navigable_config_found = True
                                    self.progress_update(
                                        f"Successfully placed {obstacle_config.amount} obstacles for config"
                                    )
                                else:
                                    self.progress_update(
                                        f"Attempt {attempt}/{max_attempts}: obstacles block navigation, retrying..."
                                    )

                            except Exception as e:
                                self.progress_update(
                                    f"Attempt {attempt}/{max_attempts}: validation error: {str(e)}, retrying..."
                                )
                        else:
                            raise FileNotFoundError(f"Map file not found: {map_file_path}")

                # If we couldn't find a navigable configuration after all attempts
                if not navigable_config_found:
                    self.progress_update(
                        f"Warning: Could not place {obstacle_config.amount} obstacles for config while maintaining navigation"
                    )
                    raise ValueError("Could not place obstacles while maintaining navigation")

        # Always create variation with parameter, even if obstacle_objects is empty
        # This ensures consistent naming and parameters in scenario.config
        static_objects_parameter_name = self.parameters.name
        result_config = self.update_config(config, {
            static_objects_parameter_name: convert_dataclasses_to_dict(obstacle_objects) if obstacle_objects else []
        })

        resulting_configs.append(result_config)

        return resulting_configs
