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

import math
import pickle
from typing import Optional

import numpy as np
from pydantic import BaseModel, ConfigDict

from robovast.common import FileCache
from robovast.common.variation import VariationGuiRenderer

from ..data_model import Orientation, Pose, Position
from ..gui.navigation_gui import NavigationGui
from ..path_generator import PathGenerator
from ..waypoint_generator import WaypointGenerator
from .nav_base_variation import NavVariation


class PoseConfig(BaseModel):
    """Represents a 2D pose with x, y, and yaw."""
    x: float
    y: float
    yaw: float


class PathVariationConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_pose: str | PoseConfig
    goal_pose: str | dict  # Can be a reference like "@goal_pose" or a dict with x, y, yaw
    map_file: Optional[str] = None
    path_length: float
    num_paths: int
    path_length_tolerance: float = 0.5
    min_distance: float
    seed: int
    robot_diameter: float


class PathVariationGuiRenderer(VariationGuiRenderer):

    def update_gui(self, config, path):
        path = config.get('_path', None)
        if path:
            plain_path = [(p.x, p.y) for p in path]
            self.gui_object.draw_path(plain_path,
                                      color='red', linewidth=2.0,
                                      alpha=0.8, label='Path',
                                      show_endpoints=True)


class PathVariation(NavVariation):
    """Create random route variations."""

    CONFIG_CLASS = PathVariationConfig
    GUI_CLASS = NavigationGui
    GUI_RENDERER_CLASS = PathVariationGuiRenderer

    def variation(self, in_configs):
        self.progress_update("Running Path Variation...")
        results = []

        for config in in_configs:
            # calculate all start/goal poses for configuration
            for path_index in range(self.parameters.num_paths):
                current_seed = self.parameters.seed + path_index
                print(f"Generating path for configuration {config['name']}, path index {path_index}, seed {current_seed}")
                start_pose, goal_pose, path, map_file = self.generate_path_for_config(
                    self.base_path, config, path_index, current_seed
                )

                new_config = self.update_config(config, {
                    'start_pose': start_pose,
                    'goal_pose': goal_pose},
                    other_values={
                        '_path': path,
                        '_map_file': map_file
                })
                results.append(new_config)

        return results

    def generate_path_for_config(self, cache_path, config, path_index, seed):
        """Generate a single path for a config."""

        try:
            map_file_path = self.get_map_file(self.parameters.map_file, config)
        except Exception as e:  # pylint: disable=broad-except
            raise ValueError(f"Error determining map file for config {config['name']}: {e}") from e

        path_length_tolerance = self.parameters.path_length_tolerance
        if not self.parameters.path_length_tolerance:
            path_length_tolerance = 0.5
        self.progress_update(f"Using map file: {map_file_path}")
        self.progress_update(f"Using path_length: {self.parameters.path_length}±{path_length_tolerance}")
        self.progress_update(f"Using robot_diameter: {self.parameters.robot_diameter}")

        waypoint_generator = WaypointGenerator(map_file_path)

        if self.parameters.start_pose:
            if isinstance(self.parameters.start_pose, str):
                # Reference to a config parameter
                pose_ref = self.parameters.start_pose.lstrip('@')
                self.check_scenario_parameter_reference(pose_ref)
                start_pose = None
            else:
                # Directly specified pose
                start_pose = Pose(
                    position=Position(
                        x=self.parameters.start_pose.x,
                        y=self.parameters.start_pose.y),
                    orientation=Orientation(
                        yaw=self.parameters.start_pose.yaw
                    )
                )
                if not waypoint_generator.is_valid_position(start_pose.position.x, start_pose.position.y, self.parameters.robot_diameter/2.):
                    raise ValueError(f"Start pose {start_pose} is not valid on the map.")
                self.progress_update(f"Using provided start pose: {start_pose}")
        else:
            start_pose = None

        file_cache = FileCache(cache_path, "robovast_path_generation_", [self.parameters, seed])
        cache = file_cache.get_cached_file([map_file_path], binary=True)
        if cache:
            start_pose, goal_pose, path = pickle.loads(cache)
            self.progress_update(f"Using cached start/goal poses {start_pose} -> {goal_pose}")
            return start_pose, goal_pose, path, map_file_path

        path_generator = PathGenerator(map_file_path)

        attempt = 0
        max_attempts = 1000  # Maximum attempts to find a valid path
        path_found = False

        np.random.seed(seed)
        while attempt < max_attempts and not path_found:

            self.progress_update(
                f"Generating {config['name']}, {path_index} - Attempt {attempt}/{max_attempts}"
            )

            if not start_pose:
                self.progress_update("  Generating start pose")
                start_poses_list = waypoint_generator.generate_waypoints(num_waypoints=1,
                                                                         robot_diameter=self.parameters.robot_diameter)
                start_pose = start_poses_list[0]

            waypoints = [start_pose]
            goal_poses_list = waypoint_generator.generate_waypoints(
                num_waypoints=1,
                robot_diameter=self.parameters.robot_diameter,
                min_distance=self.parameters.min_distance,  # Minimum distance between waypoints
                max_distance=self.parameters.path_length,
                initial_start_pose=start_pose
            )
            if not goal_poses_list:
                self.progress_update("   no valid goal poses found")
                attempt += 1
                continue

            # Take the last goal pose as the final goal
            goal_pose = goal_poses_list[-1]
            waypoints.extend(goal_poses_list)

            self.progress_update(f"  Generated waypoints: {waypoints}")
            # Generate path considering any existing static objects
            path = path_generator.generate_path(waypoints, [])

            if not path:
                self.progress_update(f"   no path found")
                attempt += 1
                continue

            # Enforce path length tolerance
            length = sum(
                math.hypot(
                    path[i].x - path[i - 1].x, path[i].y - path[i - 1].y
                )
                for i in range(1, len(path))
            )
            if abs(length - self.parameters.path_length) > path_length_tolerance:
                self.progress_update(f"   path length {length:.2f} outside tolerance. {
                    abs(length - self.parameters.path_length)} > {path_length_tolerance}")
                attempt += 1
                continue
            else:
                self.progress_update(f"   path length {length:.2f} within tolerance.")

            # Path found and valid
            path_found = True
            break

        if not path_found:
            raise ValueError("Failed to generate valid path within maximum attempts.")

        # Convert numpy types to native Python types
        start_pose = Pose(
            position=Position(
                x=float(start_pose.position.x),
                y=float(start_pose.position.y)
            ),
            orientation=Orientation(
                yaw=float(start_pose.orientation.yaw)
            )
        )
        goal_pose = Pose(
            position=Position(
                x=float(goal_pose.position.x),
                y=float(goal_pose.position.y)
            ),
            orientation=Orientation(
                yaw=float(goal_pose.orientation.yaw)
            )
        )

        self.progress_update(f"  Found path after {attempt} attempts: {start_pose} -> {goal_pose}")
        file_content = pickle.dumps((start_pose, goal_pose, path))
        file_cache.save_file_to_cache(
            input_files=[map_file_path],
            file_content=file_content,
            binary=True)
        return start_pose, goal_pose, path, map_file_path
