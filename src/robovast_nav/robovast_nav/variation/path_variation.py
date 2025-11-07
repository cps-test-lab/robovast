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
import os

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator

from robovast.common import FileCache
from robovast.common.variation import Variation

from ..path_generator import PathGenerator
from ..waypoint_generator import WaypointGenerator


class PathVariationConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: list[str]
    path_length: float
    num_paths: int
    path_length_tolerance: float = 0.5
    min_distance: float
    seed: int
    robot_diameter: float

    @field_validator('name')
    @classmethod
    def validate_name_list(cls, v):
        if not v or len(v) != 2:
            raise ValueError('name must contain exactly two elements, 1. for start_pose, 2. for goal_poses')
        return v


class PathVariation(Variation):
    """Create random route variations."""

    CONFIG_CLASS = PathVariationConfig

    def variation(self, in_variants):
        self.progress_update("Running Path Variation...")

        # tasks = []
        # for variant in in_variants:
        #     for path_index in range(self.parameters.num_paths):
        #         tasks.append((variant, path_index, self.parameters.seed))
        results = []

        # for task in tasks:
        #     result = self.generate_path_for_variant(self.output_dir, task[0], task[1], task[2])
        #     if not result:
        #         return []
        #     results.append(result)

        start_pose_parameter_name = self.parameters.name[0]
        goal_poses_parameter_name = self.parameters.name[1]
        for variant in in_variants:

            # calculate all start/goal poses for variant
            for path_index in range(self.parameters.num_paths):
                start_pose, goal_poses = self.generate_path_for_variant(
                    self.output_dir, variant, path_index, self.parameters.seed
                )

                new_variant = self.update_variant(variant, {
                    start_pose_parameter_name: start_pose,
                    goal_poses_parameter_name: goal_poses})
                results.append(new_variant)

        return results

    def generate_path_for_variant(self, cache_path, variant, path_index, seed):
        """Generate a single path for a variant."""

        if "floorplan_variant_path" not in variant:
            raise ValueError("Expected variant to contain 'floorplan_variant_path' field")

        map_file_basename = os.path.basename(variant["floorplan_variant_path"]).rsplit("_", 1)[0]
        rel_map_path = os.path.join('maps', map_file_basename + '.yaml')
        map_file_path = os.path.join(self.output_dir, variant["floorplan_variant_path"], rel_map_path)
        if not os.path.exists(map_file_path):
            raise ValueError(f"File {map_file_path} does not exist.")

        file_cache = FileCache()
        cache_file_name = f"path_generation_{variant['name']}_{path_index}_{seed}"
        file_cache.set_current_data_directory(cache_path)
        strings_for_hash = [str(path_index), str(seed)]
        cached_attempt = file_cache.get_cached_file([map_file_path], cache_file_name, strings_for_hash=strings_for_hash)
        if cached_attempt:
            attempt = int(cached_attempt)
            self.progress_update(f"Using cached attempt {attempt}")
        else:
            attempt = 0

        path_generator = PathGenerator(map_file_path)

        max_attempts = 1000  # Maximum attempts to find a valid path
        path_found = False

        while attempt < max_attempts and not path_found:
            current_seed = attempt + (max_attempts * path_index) + seed

            np.random.seed(current_seed)
            waypoint_generator = WaypointGenerator(map_file_path)
            self.progress_update(
                f"Generating {variant['name']}, {path_index} - Attempt {attempt}/{max_attempts}"
            )
            waypoints = waypoint_generator.generate_waypoints(
                num_waypoints=2,  # Generate 2 waypoints beyond start
                robot_diameter=self.parameters.robot_diameter,
                min_distance=self.parameters.min_distance,  # Minimum distance between waypoints
            )
            start_pose = waypoints[0] if waypoints else None
            goal_poses = waypoints[1:] if len(waypoints) > 1 else []

            if start_pose and goal_poses:
                # Generate path considering any existing static objects
                path = path_generator.generate_path(waypoints, [])

                if not path:
                    self.progress_update(f"   no path found")
                    attempt += 1
                    continue

                # Enforce path length tolerance
                path_length = self.parameters.path_length
                path_length_tolerance = self.parameters.path_length_tolerance
                if path_length is None:
                    raise ValueError("'path_length' must be specified in parameters")
                if path_length_tolerance is None:
                    raise ValueError("'path_length_tolerance' must be specified in parameters")

                length = sum(
                    math.hypot(
                        path[i].x - path[i - 1].x, path[i].y - path[i - 1].y
                    )
                    for i in range(1, len(path))
                )
                if abs(length - path_length) > path_length_tolerance:
                    self.progress_update(f"   path length {length:.2f} outside tolerance. {
                                         abs(length - path_length)} > {path_length_tolerance}")
                    attempt += 1
                    continue

                # Path found and valid
                path_found = True
            attempt += 1

        if not path_found:
            self.progress_update(
                f"Failed to generate {variant['name']}, {path_index} after {max_attempts} attempts"
            )
            return None

        self.progress_update(f"  Found path after {attempt} attempts: {start_pose} -> {goal_poses}")
        file_cache.save_file_to_cache(
            input_files=[map_file_path],
            file_name=cache_file_name,
            file_content=str(attempt),
            strings_for_hash=strings_for_hash)
        return start_pose, goal_poses
