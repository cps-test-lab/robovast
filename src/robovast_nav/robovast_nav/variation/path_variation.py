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


def _normalize_pose(value):
    if value is None:
        return None
    if isinstance(value, Pose):
        return value
    if isinstance(value, PoseConfig):
        return Pose(
            position=Position(x=float(value.x), y=float(value.y)),
            orientation=Orientation(yaw=float(value.yaw))
        )
    if isinstance(value, dict):
        if "position" in value and "orientation" in value:
            position = value["position"]
            orientation = value["orientation"]
            return Pose(
                position=Position(x=float(position["x"]), y=float(position["y"])),
                orientation=Orientation(yaw=float(orientation["yaw"]))
            )
        if all(key in value for key in ("x", "y", "yaw")):
            return Pose(
                position=Position(x=float(value["x"]), y=float(value["y"])),
                orientation=Orientation(yaw=float(value["yaw"]))
            )
    if hasattr(value, "position") and hasattr(value, "orientation"):
        return Pose(
            position=Position(x=float(value.position.x), y=float(value.position.y)),
            orientation=Orientation(yaw=float(value.orientation.yaw))
        )
    raise ValueError(f"Unsupported pose type: {type(value)}")


def _normalize_pose_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [_normalize_pose(item) for item in value if item is not None]
    return [_normalize_pose(value)]


def _pose_cache_key(pose):
    if pose is None:
        return None
    return (
        float(pose.position.x),
        float(pose.position.y),
        float(pose.orientation.yaw)
    )


def _pose_list_cache_key(poses):
    return tuple(_pose_cache_key(pose) for pose in poses)


class PathVariationRandomConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_pose: str | PoseConfig
    goal_poses: str | list[dict] | list[PoseConfig]  # Can be a reference like "@goal_poses" or "@goal_pose"
    num_goal_poses: Optional[int] = None  # Number of goal poses to generate (optional, defaults based on target parameter)
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

        # Handle both single goal pose and multiple goal poses
        # Check both possible parameter names
        goal_poses = config.get('config', {}).get('goal_poses', None)
        goal_pose = config.get('config', {}).get('goal_pose', None)
        
        if goal_pose is not None:
            # Single pose parameter
            goal_poses_list = [goal_pose]
            label = 'Goal Pose'
        elif goal_poses is not None:
            # Multiple poses parameter
            if isinstance(goal_poses, list):
                goal_poses_list = goal_poses
                label = 'Goal Poses'
            else:
                goal_poses_list = [goal_poses]
                label = 'Goal Pose'
        else:
            goal_poses_list = []
            label = 'Goal Poses'

        if goal_poses_list:
            # Extract x and y coordinates from Pose objects
            x_coords = [pose.position.x for pose in goal_poses_list]
            y_coords = [pose.position.y for pose in goal_poses_list]
            
            self.gui_object.map_visualizer.ax.scatter(x_coords, y_coords,
                                                      s=10,  # marker size
                                                      c='blue',
                                                      alpha=0.9,
                                                      label=label,
                                                      zorder=10)
                
        # Visualize raster points if available
        raster_points = config.get('_raster_points', None)
        if raster_points:
            # Draw all raster points at once for better performance
            x_coords = [point[0] for point in raster_points]
            y_coords = [point[1] for point in raster_points]
            self.gui_object.map_visualizer.ax.scatter(x_coords, y_coords,
                                                      s=3,  # marker size
                                                      c='gray',
                                                      alpha=0.3,
                                                      label='Raster Points',
                                                      zorder=2)
        
        # Final canvas draw to update display
        self.gui_object.canvas.draw()


class PathVariationRandom(NavVariation):
    """Create random route variations."""

    CONFIG_CLASS = PathVariationRandomConfig
    GUI_CLASS = NavigationGui
    GUI_RENDERER_CLASS = PathVariationGuiRenderer

    def variation(self, in_configs):
        self.progress_update("Running Path Variation...")
        results = []
        
        for config in in_configs:
            # Detect if we should output single pose or multiple poses based on parameter name
            # Use the configuration reference to determine target parameter
            goal_param_name = 'goal_poses'  # Default
            if isinstance(self.parameters.goal_poses, str):
                ref_name = self.parameters.goal_poses.lstrip('@')
                goal_param_name = ref_name
            
            single_pose_mode = goal_param_name == 'goal_pose'
            
            # Set default num_goal_poses if not specified
            if self.parameters.num_goal_poses is None:
                self.parameters.num_goal_poses = 1 
                
            self.progress_update(f"Detected target parameter '{goal_param_name}' - generating {self.parameters.num_goal_poses} goal pose(s)")

            # calculate all start/goal poses for configuration
            for path_index in range(self.parameters.num_paths):
                current_seed = self.parameters.seed + path_index
                print(f"Generating path for configuration {config['name']}, path index {path_index}, seed {current_seed}")
                start_pose, goal_poses, path, map_file = self.generate_path_for_config(
                    self.base_path, config, path_index, current_seed
                )

                # Format goal_poses based on the target parameter
                if single_pose_mode and len(goal_poses) >= 1:
                    # Single pose mode: output the first pose directly (not in a list)
                    formatted_goal_poses = goal_poses[0]
                    target_param = 'goal_pose'
                else:
                    # Multiple poses mode: output as list
                    formatted_goal_poses = goal_poses
                    target_param = 'goal_poses'

                new_config = self.update_config(config, {
                    'start_pose': start_pose,
                    target_param: formatted_goal_poses},
                    other_values={
                        '_path': path,
                        '_map_file': map_file
                })
                results.append(new_config)

        return results

    def generate_path_for_config(self, cache_path, config, path_index, seed):
        """Generate a single path with multiple goal poses for a config.

        Args:
            cache_path: Path for caching results
            config: Configuration dictionary
            path_index: Index of the path being generated
            seed: Random seed for generation

        Returns:
            Tuple of (start_pose, goal_poses, path, map_file_path)
        """
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

        config_values = config.get("config", {})
        provided_start_pose = _normalize_pose(config_values.get("start_pose"))
        if provided_start_pose is None and not isinstance(self.parameters.start_pose, str):
            provided_start_pose = _normalize_pose(self.parameters.start_pose)

        if config_values.get("goal_pose") is not None:
            provided_goal_poses = _normalize_pose_list(config_values.get("goal_pose"))
        elif config_values.get("goal_poses") is not None:
            provided_goal_poses = _normalize_pose_list(config_values.get("goal_poses"))
        elif not isinstance(self.parameters.goal_poses, str):
            provided_goal_poses = _normalize_pose_list(self.parameters.goal_poses)
        else:
            provided_goal_poses = []

        if provided_start_pose and not waypoint_generator.is_valid_position(
            provided_start_pose.position.x,
            provided_start_pose.position.y,
            self.parameters.robot_diameter / 2.0
        ):
            raise ValueError(f"Start pose {provided_start_pose} is not valid on the map.")

        for goal_pose in provided_goal_poses:
            if not waypoint_generator.is_valid_position(
                goal_pose.position.x,
                goal_pose.position.y,
                self.parameters.robot_diameter / 2.0
            ):
                raise ValueError(f"Goal pose {goal_pose} is not valid on the map.")

        if provided_goal_poses and len(provided_goal_poses) > self.parameters.num_goal_poses:
            raise ValueError(
                f"Provided {len(provided_goal_poses)} goal poses, but num_goal_poses is {self.parameters.num_goal_poses}."
            )

        cache_key = [
            self.parameters,
            seed,
            _pose_cache_key(provided_start_pose),
            _pose_list_cache_key(provided_goal_poses)
        ]
        file_cache = FileCache(cache_path, "robovast_path_generation_", cache_key)
        cache = file_cache.get_cached_file([map_file_path], binary=True)
        if cache:
            cached_start_pose, cached_goal_poses, cached_path = pickle.loads(cache)
            self.progress_update(f"Using cached start/goal poses {cached_start_pose} -> {cached_goal_poses}")
            return cached_start_pose, cached_goal_poses, cached_path, map_file_path

        path_generator = PathGenerator(map_file_path)

        attempt = 0
        max_attempts = 1000  # Maximum attempts to find a valid path
        path_found = False

        if provided_start_pose and len(provided_goal_poses) == self.parameters.num_goal_poses:
            max_attempts = 1

        np.random.seed(seed)
        while attempt < max_attempts and not path_found:

            self.progress_update(
                f"Generating {config['name']}, {path_index} - Attempt {attempt}/{max_attempts}"
            )

            target_distance_per_segment = self.parameters.path_length / self.parameters.num_goal_poses

            # Determine start pose
            if provided_start_pose is not None:
                start_pose = provided_start_pose
            else:
                self.progress_update("  Generating start pose")
                if provided_goal_poses:
                    start_poses_list = waypoint_generator.generate_waypoints(
                        num_waypoints=1,
                        robot_diameter=self.parameters.robot_diameter,
                        min_distance=self.parameters.min_distance,
                        max_distance=target_distance_per_segment,
                        initial_start_pose=provided_goal_poses[0]
                    )
                else:
                    start_poses_list = waypoint_generator.generate_waypoints(
                        num_waypoints=1,
                        robot_diameter=self.parameters.robot_diameter
                    )
                if not start_poses_list:
                    self.progress_update("   failed to generate start pose")
                    attempt += 1
                    continue
                start_pose = start_poses_list[0]

            waypoints = [start_pose]

            goal_poses_list = list(provided_goal_poses)
            previous_pose = goal_poses_list[-1] if goal_poses_list else start_pose

            # Generate remaining goal poses sequentially within target radii
            if len(goal_poses_list) < self.parameters.num_goal_poses:
                self.progress_update(f"  Generating {self.parameters.num_goal_poses} goal poses sequentially")
                for goal_idx in range(len(goal_poses_list), self.parameters.num_goal_poses):
                    self.progress_update(f"    Generating goal pose {goal_idx + 1}/{self.parameters.num_goal_poses}")

                    next_goal_poses = waypoint_generator.generate_waypoints(
                        num_waypoints=1,
                        robot_diameter=self.parameters.robot_diameter,
                        min_distance=self.parameters.min_distance,
                        max_distance=target_distance_per_segment,
                        initial_start_pose=previous_pose
                    )

                    if not next_goal_poses:
                        self.progress_update(f"    Failed to generate goal pose {goal_idx + 1}")
                        break

                    next_goal_pose = next_goal_poses[0]
                    goal_poses_list.append(next_goal_pose)
                    previous_pose = next_goal_pose

            if len(goal_poses_list) < self.parameters.num_goal_poses:
                self.progress_update(
                    f"   not enough valid goal poses found (got {len(goal_poses_list)}, needed {self.parameters.num_goal_poses})"
                )
                attempt += 1
                continue

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
                self.progress_update(
                    f"   path length {length:.2f} outside tolerance. "
                    f"{abs(length - self.parameters.path_length)} > {path_length_tolerance}"
                )
                attempt += 1
                continue
            else:
                self.progress_update(f"   path length {length:.2f} within tolerance.")

            # Path found and valid
            path_found = True
            break

        if not path_found:
            raise ValueError("Failed to generate valid path within maximum attempts.")

        # Convert numpy types to native Python types for start pose
        start_pose = Pose(
            position=Position(
                x=float(start_pose.position.x),
                y=float(start_pose.position.y)
            ),
            orientation=Orientation(
                yaw=float(start_pose.orientation.yaw)
            )
        )
        
        # Convert numpy types to native Python types for goal poses
        goal_poses = []
        for goal_pose in goal_poses_list:
            goal_poses.append(Pose(
                position=Position(
                    x=float(goal_pose.position.x),
                    y=float(goal_pose.position.y)
                ),
                orientation=Orientation(
                    yaw=float(goal_pose.orientation.yaw)
                )
            ))

        self.progress_update(f"  Found path after {attempt} attempts: {start_pose} -> {goal_poses}")
        file_content = pickle.dumps((start_pose, goal_poses, path))
        file_cache.save_file_to_cache(
            input_files=[map_file_path],
            file_content=file_content,
            binary=True)
        return start_pose, goal_poses, path, map_file_path


class PathVariationRasterizedConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_pose: Optional[str | PoseConfig] = None
    num_goal_poses: Optional[int] = 1  # Number of goal poses to generate
    map_file: Optional[str] = None
    raster_size: float  # Grid spacing for square rasterization in meters
    raster_offset_x: float = 0.0  # Offset for raster grid in x direction (meters)
    raster_offset_y: float = 0.0  # Offset for raster grid in y direction (meters)
    path_length: float
    path_length_tolerance: float = 0.5
    robot_diameter: float


class PathVariationRasterized(NavVariation):
    """Create route variations covering all areas of the map using a square grid."""

    CONFIG_CLASS = PathVariationRasterizedConfig
    GUI_CLASS = NavigationGui
    GUI_RENDERER_CLASS = PathVariationGuiRenderer

    def variation(self, in_configs):
        self.progress_update("Running Rasterized Path Variation...")
        results = []

        for config in in_configs:
            # Get the map file and generate raster points
            try:
                map_file_path = self.get_map_file(self.parameters.map_file, config)
            except Exception as e:  # pylint: disable=broad-except
                raise ValueError(f"Error determining map file for config {config['name']}: {e}") from e

            # Generate raster points for the map (square grid)
            waypoint_generator = WaypointGenerator(map_file_path)
            raster_points = self._generate_raster_points(waypoint_generator)

            self.progress_update(f"Generated {len(raster_points)} valid raster points")

            config_values = config.get("config", {})
            provided_start_pose = _normalize_pose(config_values.get("start_pose"))
            if provided_start_pose is None and self.parameters.start_pose and not isinstance(self.parameters.start_pose, str):
                provided_start_pose = _normalize_pose(self.parameters.start_pose)

            if config_values.get("goal_pose") is not None:
                provided_goal_poses = _normalize_pose_list(config_values.get("goal_pose"))
            elif config_values.get("goal_poses") is not None:
                provided_goal_poses = _normalize_pose_list(config_values.get("goal_poses"))
            else:
                provided_goal_poses = []

            provided_goal_pose = provided_goal_poses[0] if provided_goal_poses else None

            # Determine start poses
            if provided_start_pose is not None:
                if not waypoint_generator.is_valid_position(
                    provided_start_pose.position.x,
                    provided_start_pose.position.y,
                    self.parameters.robot_diameter / 2.
                ):
                    raise ValueError(f"Start pose {provided_start_pose} is not valid on the map.")
                start_poses = [provided_start_pose]
                self.progress_update(f"Using provided start pose: {provided_start_pose}")
            elif self.parameters.start_pose and isinstance(self.parameters.start_pose, str):
                pose_ref = self.parameters.start_pose.lstrip('@')
                self.check_scenario_parameter_reference(pose_ref)
                start_poses = [
                    Pose(
                        position=Position(x=x, y=y),
                        orientation=Orientation(yaw=0.0)
                    )
                    for x, y in raster_points
                ]
            else:
                start_poses = [
                    Pose(
                        position=Position(x=x, y=y),
                        orientation=Orientation(yaw=0.0)
                    )
                    for x, y in raster_points
                ]

            for goal_pose in provided_goal_poses:
                if not waypoint_generator.is_valid_position(
                    goal_pose.position.x,
                    goal_pose.position.y,
                    self.parameters.robot_diameter / 2.0
                ):
                    raise ValueError(f"Goal pose {goal_pose} is not valid on the map.")

            # Choose behavior based on num_goal_poses
            if self.parameters.num_goal_poses == 1:
                # Original behavior: all points to all other points
                results.extend(self._generate_single_goal_configs(
                    config, start_poses, raster_points,
                    map_file_path, provided_goal_pose
                ))
            else:
                # New behavior: multiple goal poses with search radius algorithm
                results.extend(self._generate_multi_goal_configs(
                    config, start_poses, raster_points,
                    map_file_path, provided_goal_poses
                ))

        return results

    def _generate_single_goal_configs(self, config, start_poses, raster_points,
                                    map_file_path, provided_goal_pose=None):
        """Original behavior: generate paths from each start pose to all reachable raster points."""
        results = []
        path_index = 0
        path_generator = PathGenerator(map_file_path)

        if provided_goal_pose is not None:
            raster_points = [(provided_goal_pose.position.x, provided_goal_pose.position.y)]

        single_result_only = provided_goal_pose is not None or len(start_poses) == 1
        if single_result_only:
            raster_points = list(raster_points)
            np.random.shuffle(raster_points)
            if len(start_poses) > 1:
                start_poses = list(start_poses)
                np.random.shuffle(start_poses)
        
        for start_idx, start_pose in enumerate(start_poses):
            for goal_idx, (goal_x, goal_y) in enumerate(raster_points):
                # Skip if start and goal are the same raster point
                if start_pose is not None and \
                   abs(start_pose.position.x - goal_x) < self.parameters.raster_size / 2 and \
                   abs(start_pose.position.y - goal_y) < self.parameters.raster_size / 2:
                    continue

                # Create goal pose from raster point
                goal_pose = Pose(
                    position=Position(x=goal_x, y=goal_y),
                    orientation=Orientation(yaw=0.0)
                )

                self.progress_update(
                    f"Generating path {path_index}: start {start_idx} -> goal raster point {goal_idx}"
                )

                # Generate path directly
                waypoints = [start_pose, goal_pose]
                path = path_generator.generate_path(waypoints, [])

                if path:
                    # Calculate path length
                    path_length = self._calculate_path_length(path)

                    # Check if path length is within tolerance
                    min_length = self.parameters.path_length - self.parameters.path_length_tolerance
                    max_length = self.parameters.path_length + self.parameters.path_length_tolerance

                    if min_length <= path_length <= max_length:
                        # Format goal_poses based on num_goal_poses
                        if self.parameters.num_goal_poses == 1:
                            formatted_goal_poses = goal_pose
                            target_param = 'goal_pose'
                        else:
                            formatted_goal_poses = [goal_pose]
                            target_param = 'goal_poses'
                        
                        new_config = self.update_config(config, {
                            'start_pose': start_pose,
                            target_param: formatted_goal_poses},
                            other_values={
                                '_path': path,
                                '_map_file': map_file_path,
                                '_raster_points': raster_points,
                                '_path_length': path_length
                        })
                        results.append(new_config)
                        if single_result_only:
                            return results
                    else:
                        self.progress_update(
                            f'  Path length {path_length:.2f}m outside tolerance '
                            f'[{min_length:.2f}, {max_length:.2f}]'
                        )
                else:
                    self.progress_update(f"  No path found from {start_pose} to {goal_pose}")

                path_index += 1
        
        return results

    def _generate_multi_goal_configs(self, config, start_poses, raster_points,
                                   map_file_path, provided_goal_poses=None):
        """New behavior: generate multiple goal poses using search radius algorithm."""
        results = []
        path_generator = PathGenerator(map_file_path)

        provided_goal_poses = provided_goal_poses or []
        if provided_goal_poses and len(provided_goal_poses) > self.parameters.num_goal_poses:
            raise ValueError(
                f"Provided {len(provided_goal_poses)} goal poses, but num_goal_poses is {self.parameters.num_goal_poses}."
            )
        
        for start_idx, start_pose in enumerate(start_poses):
            self.progress_update(f"Generating multi-goal path for start pose {start_idx}")
            
            # Calculate optimal search radius and bonus distance
            search_radius, bonus_distance = self._calculate_search_parameters()
            
            # Generate goal poses iteratively
            goal_poses_list = list(provided_goal_poses)
            current_pose = goal_poses_list[-1] if goal_poses_list else start_pose
            waypoints = [start_pose] + goal_poses_list
            
            remaining_goal_count = self.parameters.num_goal_poses - len(goal_poses_list)
            for goal_idx in range(remaining_goal_count):
                is_final_goal = goal_idx == remaining_goal_count - 1
                search_dist = search_radius + bonus_distance if is_final_goal else search_radius
                
                # Find next goal pose within search distance
                next_goal = self._find_goal_within_distance(
                    current_pose, raster_points, search_dist, goal_poses_list
                )
                
                if next_goal is None:
                    self.progress_update(f"  No valid goal pose found within distance {search_dist:.2f}m for goal {goal_idx + 1}")
                    break
                    
                goal_poses_list.append(next_goal)
                waypoints.append(next_goal)
                current_pose = next_goal
            
            # Only proceed if we found all required goal poses
            if len(goal_poses_list) == self.parameters.num_goal_poses:
                # Generate path through all waypoints
                path = path_generator.generate_path(waypoints, [])
                
                if path:
                    # Calculate path length
                    path_length = self._calculate_path_length(path)

                    # Check if path length is within tolerance
                    min_length = self.parameters.path_length - self.parameters.path_length_tolerance
                    max_length = self.parameters.path_length + self.parameters.path_length_tolerance

                    if min_length <= path_length <= max_length:
                        # Format goal_poses based on num_goal_poses
                        if self.parameters.num_goal_poses == 1:
                            formatted_goal_poses = goal_poses_list[0] if goal_poses_list else None
                            target_param = 'goal_pose'
                        else:
                            formatted_goal_poses = goal_poses_list
                            target_param = 'goal_poses'
                        
                        new_config = self.update_config(config, {
                            'start_pose': start_pose,
                            target_param: formatted_goal_poses},
                            other_values={
                                '_path': path,
                                '_map_file': map_file_path,
                                '_raster_points': raster_points,
                                '_path_length': path_length
                        })
                        results.append(new_config)
                        self.progress_update(f"  Generated valid multi-goal path with {len(goal_poses_list)} goals")
                    else:
                        self.progress_update(
                            f'  Path length {path_length:.2f}m outside tolerance '
                            f'[{min_length:.2f}, {max_length:.2f}]'
                        )
                else:
                    self.progress_update(f"  No path found through waypoints")
            else:
                self.progress_update(f"  Could not find all {self.parameters.num_goal_poses} goal poses")
        
        return results

    def _calculate_search_parameters(self):
        """Calculate optimal search radius and bonus distance."""
        # Calculate with tolerances
        max_path_length = self.parameters.path_length + self.parameters.path_length_tolerance
        min_path_length = self.parameters.path_length - self.parameters.path_length_tolerance
        
        max_required_points = max_path_length / self.parameters.raster_size
        min_required_points = min_path_length / self.parameters.raster_size
        
        total_points = self.parameters.num_goal_poses + 1  # +1 for start pose
        
        # Calculate search radius and bonus for both cases
        max_search_radius = int(max_required_points // total_points)
        max_bonus = max_required_points % total_points
        
        min_search_radius = int(min_required_points // total_points)
        min_bonus = min_required_points % total_points
        
        # Choose the one with smaller bonus distance
        if max_bonus <= min_bonus:
            search_radius = max_search_radius * self.parameters.raster_size
            bonus_distance = max_bonus * self.parameters.raster_size
        else:
            search_radius = min_search_radius * self.parameters.raster_size
            bonus_distance = min_bonus * self.parameters.raster_size
        
        self.progress_update(f"  Search radius: {search_radius:.2f}m, bonus distance: {bonus_distance:.2f}m")
        return search_radius, bonus_distance

    def _find_goal_within_distance(self, current_pose, raster_points, max_distance, existing_goals):
        """Find a raster point within max_distance of current_pose that hasn't been used."""
        for x, y in raster_points:
            # Skip if this point is too close to current pose (same raster point)
            if abs(current_pose.position.x - x) < self.parameters.raster_size / 2 and \
               abs(current_pose.position.y - y) < self.parameters.raster_size / 2:
                continue
            
            # Skip if this point is already used as a goal
            point_used = False
            for existing_goal in existing_goals:
                if abs(existing_goal.position.x - x) < self.parameters.raster_size / 2 and \
                   abs(existing_goal.position.y - y) < self.parameters.raster_size / 2:
                    point_used = True
                    break
            if point_used:
                continue
            
            # Check if within distance
            distance = math.sqrt((current_pose.position.x - x)**2 + (current_pose.position.y - y)**2)
            if distance <= max_distance:
                return Pose(
                    position=Position(x=x, y=y),
                    orientation=Orientation(yaw=0.0)
                )
        
        return None

    def _generate_raster_points(self, waypoint_generator):
        """Generate valid raster points covering the map using a square grid.

        Args:
            waypoint_generator: WaypointGenerator instance for the map

        Returns:
            List of (x, y) tuples representing valid raster points
        """
        # Get map boundaries
        map_data = waypoint_generator.map.map_array
        resolution = waypoint_generator.map.resolution
        origin = waypoint_generator.map.origin

        height, width = map_data.shape

        # Calculate map bounds in world coordinates
        min_x = origin[0]
        max_x = origin[0] + width * resolution
        min_y = origin[1]
        max_y = origin[1] + height * resolution

        self.progress_update(f"Map bounds: x=[{min_x:.2f}, {max_x:.2f}], y=[{min_y:.2f}, {max_y:.2f}]")

        # Generate square grid
        # Points are uniformly spaced by raster_size in both x and y directions

        raster_points = []
        grid_spacing = self.parameters.raster_size

        # Normalize offsets to be within [0, grid_spacing) range
        # This shifts the grid alignment without skipping map areas
        normalized_offset_x = self.parameters.raster_offset_x % grid_spacing
        normalized_offset_y = self.parameters.raster_offset_y % grid_spacing

        # Adjust starting point to account for normalized offset
        start_x = min_x + normalized_offset_x
        start_y = min_y + normalized_offset_y

        # Calculate the number of grid points needed to cover the entire map
        num_x_points = int(np.ceil((max_x - start_x) / grid_spacing)) + 1
        num_y_points = int(np.ceil((max_y - start_y) / grid_spacing)) + 1

        self.progress_update(f"Grid: {num_x_points}x{num_y_points} points, spacing={grid_spacing:.2f}m, "
                             f"offset=({normalized_offset_x:.2f}, {normalized_offset_y:.2f})m")

        checked_points = 0
        valid_points = 0
        for iy in range(num_y_points):
            y = start_y + iy * grid_spacing
            if y > max_y:
                continue

            for ix in range(num_x_points):
                x = start_x + ix * grid_spacing
                if x > max_x:
                    continue

                checked_points += 1
                # Check if this point is valid (not in obstacle)
                if waypoint_generator.is_valid_position(x, y, self.parameters.robot_diameter / 2.):
                    raster_points.append((float(x), float(y)))
                    valid_points += 1

        if not valid_points:
            raise ValueError(f"Checked {checked_points} grid points, {valid_points} valid. All points are occupied.")
        return raster_points

    def _calculate_path_length(self, path):
        """Calculate the total length of a path.

        Args:
            path: List of Position objects representing the path

        Returns:
            Total path length in meters
        """
        if not path or len(path) < 2:
            return 0.0

        total_length = 0.0
        for i in range(len(path) - 1):
            dx = path[i + 1].x - path[i].x
            dy = path[i + 1].y - path[i].y
            total_length += math.sqrt(dx * dx + dy * dy)

        return total_length
