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
import math
import os

import numpy as np

from robovast.common import FileCache
from robovast.common.variation import Variation

from .floorplan_generation import generate_floorplan_variations
from .obstacle_placer import ObstaclePlacer
from .path_generator import PathGenerator
from .waypoint_generator import WaypointGenerator


class FloorplanVariation(Variation):
    """Create floorplan variation."""

    def variation(self, _):
        self.progress_update("Running Floorplan Variation...")

        result = generate_floorplan_variations(self.base_path, self.parameters.get("variation_files"), self.parameters.get(
            "num_variations"), self.parameters.get("floorplan_variation_seed"), self.output_dir, self.progress_update)
        if result is None:
            raise ValueError("Floorplan variation failed, no result returned")

        variants = []
        for root, dirs, _ in os.walk(self.output_dir):
            for dir_name in dirs:
                dir_path = os.path.join(root, dir_name)
                # only accept variants that contain a 'map' or 'maps' subdirectory
                if os.path.isdir(os.path.join(dir_path, 'maps')):
                    variants.append({
                        'name': dir_name.replace("_", "-"),
                        'floorplan_variant_path': os.path.join(self.output_dir, dir_name)
                    })

        return variants


class PathVariation(Variation):
    """Create random route variations."""

    def _generate_path_for_variant(self, cache_path, variant, path_index, seed):
        """Generate a single path for a variant. This method is executed in a thread pool."""
        variant_name = f"{variant['name']}-p{path_index + 1}"

        if "floorplan_variant_path" not in variant:
            raise ValueError("Expected variant to contain 'floorplan_variant_path' field")

        map_file_basename = os.path.basename(variant["floorplan_variant_path"]).rsplit("_", 1)[0]
        rel_map_path = os.path.join('maps', map_file_basename + '.yaml')
        rel_mesh_path = os.path.join('3d-mesh', map_file_basename + '.stl')
        map_file_path = os.path.join(variant["floorplan_variant_path"], rel_map_path)
        if not os.path.exists(map_file_path):
            raise ValueError(f"File {map_file_path} does not exist.")

        file_cache = FileCache()
        cache_file_name = f"path_generation_{variant_name}_{seed}"
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
                f"Generating {variant_name} - Attempt {attempt}/{max_attempts}"
            )
            # Use user-defined robot diameter
            robot_diameter = self.general_parameters.get('robot_diameter')
            if robot_diameter is None:
                raise ValueError("'robot_diameter' must be specified in general parameters")
            min_distance = self.parameters.get('min_distance')
            if min_distance is None:
                raise ValueError("'min_distance' must be specified in parameters")

            waypoints = waypoint_generator.generate_waypoints(
                num_waypoints=2,  # Generate 2 waypoints beyond start
                robot_diameter=robot_diameter,
                min_distance=min_distance,  # Minimum distance between waypoints
            )
            start_pose = waypoints[0] if waypoints else None
            goal_poses = waypoints[1:] if len(waypoints) > 1 else []

            if start_pose and goal_poses:
                # Generate path considering any existing static objects
                path = path_generator.generate_path(waypoints, [])

                if not path:
                    attempt += 1
                    continue

                # Enforce path length tolerance
                path_length = self.parameters.get('path_length')
                path_length_tolerance = self.parameters.get('path_length_tolerance')
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
                    attempt += 1
                    continue

                # Path found and valid
                path_found = True

        if not path_found:
            self.progress_update(
                f"Failed to generate {variant_name} after {max_attempts} attempts"
            )
            return None

        self.progress_update(f"Found path: {start_pose} -> {goal_poses}")
        # updated_variant_data = copy.deepcopy(variant_data)
        # updated_variant_data.name = variant_name
        # updated_variant_data.planned_path = path
        # updated_variant_data.variant.start_pose = start_pose
        # updated_variant_data.variant.goal_poses = goal_poses
        file_cache.save_file_to_cache(
            input_files=[map_file_path],
            file_name=cache_file_name,
            file_content=str(attempt),
            strings_for_hash=strings_for_hash)

        variant['variant_name'] = variant_name
        if "variant" not in variant:
            variant['variant'] = {}
        variant['variant']['start_pose'] = start_pose
        variant['variant']['goal_poses'] = goal_poses
        variant['path'] = path
        variant['variant']['map_file'] = rel_map_path  # os.path.relpath(map_file_path, variant["floorplan_variant_path"])
        variant['variant']['mesh_file'] = rel_mesh_path

        return variant

    def variation(self, in_variants):
        self.progress_update("Running Path Variation...")

        # Create a list of tasks for the thread pool
        tasks = []
        for variant in in_variants:
            for path_index in range(self.parameters.get("num_paths")):
                tasks.append((variant, path_index, self.parameters.get("path_generation_seed")))

        # # Process tasks in parallel using ThreadPoolExecutor
        results = []
        # with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        #     # Submit all tasks
        #     future_to_task = {
        #         executor.submit(self._generate_path_for_variant, variant, path_index, seed): (variant, path_index, seed)
        #         for variant, path_index, seed in tasks
        #     }

        #     # Collect results as they complete
        #     for future in as_completed(future_to_task):
        #         variant, path_index, _ = future_to_task[future]
        #         try:
        #             result = future.result()
        #             if result is not None:
        #                 results.append(result)
        #         except Exception as e:
        #             self.progress_update(f"Error generating path for {variant['name']}-p{path_index + 1}: {e}")

        for task in tasks:
            result = self._generate_path_for_variant(self.output_dir, task[0], task[1], task[2])
            if not result:
                return []
            results.append(result)

        return results


class ObstacleVariation(Variation):
    """Placement of random obstacles in the environment."""

    def _generate_obstacles_for_variant(self, base_path, variant, obstacle_configs, seed):
        self.progress_update(f"Generating obstacles for {variant['name']}, {obstacle_configs}, {seed}...")

        np.random.seed(seed)
        resulting_variants = []
        for obstacle_config in obstacle_configs:
            if obstacle_config["amount"] > 0:
                max_attempts = 10
                attempt = 0
                navigable_variant_found = False

                result_variant = None
                while (
                    attempt < max_attempts
                    and not navigable_variant_found
                ):
                    attempt += 1

                    # Reset variant for this attempt
                    result_variant = copy.deepcopy(variant)

                    # Create obstacle placer without setting seed (use
                    # global random state)
                    placer = ObstaclePlacer()

                    waypoints = [
                        variant["variant"]["start_pose"],
                    ] + variant["variant"]["goal_poses"]

                    robot_diameter = float(self.general_parameters["robot_diameter"])
                    try:
                        obstacle_objects = placer.place_obstacles(
                            variant["path"],
                            obstacle_config["max_distance"],
                            obstacle_config["amount"],
                            obstacle_config["model"],
                            obstacle_config.get("xacro_arguments", ""),
                            robot_diameter=robot_diameter,
                            waypoints=waypoints,
                        )
                    except Exception as e:
                        self.progress_update(f"Error placing obstacles: {e}")
                        obstacle_objects = []

                    # Add static objects to variant
                    if obstacle_objects:
                        if 'variant' not in result_variant:
                            result_variant['variant'] = {}
                        result_variant["variant"]["static_objects"] = obstacle_objects
                    else:
                        navigable_variant_found = True

                    # Validate navigation with the placed obstacles
                    if obstacle_objects and variant['variant']["map_file"]:
                        map_path = os.path.join(variant["floorplan_variant_path"],
                                                variant['variant']["map_file"])
                        print(f"Validating navigation on map {map_path} with {len(obstacle_objects)} obstacles")
                        if os.path.exists(map_path):
                            try:
                                generator = PathGenerator(
                                    map_path, robot_diameter
                                )

                                # Check if navigation is still possible
                                path = generator.generate_path(
                                    waypoints,
                                    result_variant['variant']["static_objects"],
                                )

                                if path:
                                    # Success! Navigation is still possible
                                    variant["planned_path"] = path
                                    navigable_variant_found = True
                                    self.progress_update(
                                        f"Successfully placed {obstacle_config['amount']} obstacles for variant"
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
                            print(f"Warning: Map file not found: {map_path}")

                    # If we couldn't find a navigable configuration after
                    # all attempts
                    if not navigable_variant_found:
                        if obstacle_config["amount"] > 0:
                            self.progress_update(
                                f"Warning: Could not place {obstacle_config['amount']} obstacles for variant while maintaining navigation"
                            )
                            raise ValueError("Could not place obstacles while maintaining navigation")

                # Update variant name to include obstacle info
                short_model_name = os.path.basename(obstacle_config["model"]).replace(
                    ".sdf.xacro", ""
                ).replace(".sdf", "")
                result_variant["name"] = (
                    f"{result_variant["name"]}-o{obstacle_config['amount']}-{short_model_name}"
                )

                resulting_variants.append(result_variant)

        return resulting_variants

    def variation(self, in_variants):
        self.progress_update("Running Obstacle Variation...")

        # Create a list of tasks for the thread pool
        tasks = []

        for variant in in_variants:
            tasks.append((variant, self.parameters.get("obstacle_configs"), self.parameters.get("obstacle_placement_seed")))

        # Process tasks in parallel using ThreadPoolExecutor
        results = []
        # with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        #     # Submit all tasks
        #     future_to_task = {
        #         executor.submit(self._generate_obstacles_for_variant, variant, obstacle_configs, idx, seed): (variant, obstacle_config, idx, seed)
        #         for variant, obstacle_configs, idx, seed in tasks
        #     }

        #     # Collect results as they complete
        #     for future in as_completed(future_to_task):
        #         variant, obstacle_config, idx, _ = future_to_task[future]
        #         try:
        #             result = future.result()
        #             if result is not None:
        #                 results.append(result)
        #         except Exception as e:
        #             self.progress_update(f"Error generating path for {variant['name']} {obstacle_config}: {e}")

        for task in tasks:
            result = self._generate_obstacles_for_variant(self.base_path, task[0], task[1], task[2])
            if not result:
                return []
            results.extend(result)

        return results
