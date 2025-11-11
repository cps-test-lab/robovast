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

"""
Obstacle placement module for generating obstacle positions near navigation paths.
"""

import math
import random
from typing import List

import numpy as np
from PySide6.QtCore import QObject, Signal

from .data_model import Orientation, Pose, Position, StaticObject
from .map_loader import load_map


class ObstaclePlacer(QObject):
    """Class for placing obstacles near navigation paths."""

    status_update = Signal(str)

    def place_obstacles(
        self,
        path: List[Position],
        max_distance: float,
        amount: int,
        model: str,
        xacro_arguments: str = "",
        robot_diameter: float = 0.354,
        waypoints: List[Pose] = None,
    ) -> List[StaticObject]:
        """Place obstacles near a navigation path as StaticObject instances.

        Args:
            path: List of positions defining the navigation path
            max_distance: Maximum distance from path for obstacle placement (in meters)
            amount: Number of obstacles to place
            model: Name of the obstacle model to use
            xacro_arguments: Optional xacro arguments string for the model
            robot_diameter: Diameter of the robot in meters (default: 0.354m for TurtleBot4)
            waypoints: List of Pose objects to avoid placing obstacles near (e.g., start/goal poses)

        Returns:
            List of StaticObject instances for obstacles
        """
        if not path or len(path) < 2:
            return []

        obstacle_objects: List[StaticObject] = []
        # Define minimum clearance around waypoints (robot diameter + safety
        # margin)
        waypoint_clearance = robot_diameter * 2.0  # 2x robot diameter for safety
        if waypoints is None:
            waypoint_positions: List[Position] = []
        else:
            waypoint_positions = [pose.position for pose in waypoints]
        # Calculate path segments and their lengths
        path_segments = []
        total_length = 0.0

        for i in range(len(path) - 1):
            start = path[i]
            end = path[i + 1]
            length = self._distance(start, end)
            path_segments.append({"start": start, "end": end, "length": length})
            total_length += length
        # Place obstacles with collision avoidance
        max_attempts = amount * 100  # Allow multiple attempts per obstacle
        attempts = 0

        while len(obstacle_objects) < amount and attempts < max_attempts:
            self.status_update.emit(
                f"Attempting to place obstacle: {len(obstacle_objects) + 1}/{amount}, "
                f"try {attempts + 1}/{max_attempts}"
            )
            attempts += 1
            # Select a random segment based on length (longer segments get more
            # obstacles)
            segment = self._select_random_segment(path_segments, total_length)
            # Find a random point along the segment
            t = random.random()  # Random parameter between 0 and 1
            path_point = Position(
                x=segment["start"].x + t * (segment["end"].x - segment["start"].x),
                y=segment["start"].y + t * (segment["end"].y - segment["start"].y),
            )
            # Generate obstacle position near the path point
            obstacle_pos = self._generate_obstacle_position(
                path_point, segment["start"], segment["end"], max_distance
            )
            # Check if obstacle is too close to waypoints
            if self._is_valid_obstacle_position(
                obstacle_pos,
                waypoint_positions,
                waypoint_clearance,
                [obj.spawn_pose.position for obj in obstacle_objects],
                robot_diameter,
            ):
                # Generate random yaw angle (rotation) for the obstacle
                yaw = random.uniform(
                    -math.pi, math.pi
                )  # Random rotation from -180° to +180°
                name = f"obstacle_{len(obstacle_objects)}"

                obstacle = StaticObject(
                    entity_name=name,
                    model=model,
                    spawn_pose=Pose(position=obstacle_pos, orientation=Orientation(yaw=yaw)),
                    xacro_arguments=xacro_arguments,
                )

                obstacle_objects.append(obstacle)
        return obstacle_objects

    def place_obstacles_random(
        self,
        map_file,
        amount: int,
        model: str,
        xacro_arguments: str = "",
        robot_diameter: float = 0.354,
        waypoints: List[Pose] = None,
    ) -> List[StaticObject]:
        """Place obstacles randomly on the map as StaticObject instances.

        Args:
            map_file: Path to the map YAML file
            amount: Number of obstacles to place
            model: Name of the obstacle model to use
            xacro_arguments: Optional xacro arguments string for the model
            robot_diameter: Diameter of the robot in meters (default: 0.354m for TurtleBot4)
            waypoints: List of Pose objects to avoid placing obstacles near (e.g., start/goal poses)

        Returns:
            List of StaticObject instances for obstacles
        """

        # Load map using map_loader
        map_obj = load_map(map_file)

        # Find free space using the map's occupancy grid
        # Invert occupancy_grid (True = occupied) to get free space (True = free)
        free_space_mask = ~map_obj.occupancy_grid

        # Get coordinates of free space (y, x format from numpy)
        free_coords = np.argwhere(free_space_mask)

        if len(free_coords) == 0:
            return []

        obstacle_objects: List[StaticObject] = []
        waypoint_clearance = robot_diameter * 2.0  # 2x robot diameter for safety

        if waypoints is None:
            waypoint_positions: List[Position] = []
        else:
            waypoint_positions = [pose.position for pose in waypoints]

        # Place obstacles with collision avoidance
        max_attempts = amount * 1000  # Allow multiple attempts per obstacle
        attempts = 0

        while len(obstacle_objects) < amount and attempts < max_attempts:
            self.status_update.emit(
                f"Attempting to place obstacle: {len(obstacle_objects) + 1}/{amount}, "
                f"try {attempts + 1}/{max_attempts}"
            )
            attempts += 1

            # Select random free space coordinate
            random_idx = np.random.randint(0, len(free_coords))
            grid_y, grid_x = free_coords[random_idx]

            # Convert grid coordinates to world coordinates using map_loader's conversion
            world_x, world_y = map_obj.grid_to_world(grid_x, grid_y)
            obstacle_pos = Position(x=world_x, y=world_y)

            # Check if obstacle position is valid
            if self._is_valid_obstacle_position(
                obstacle_pos,
                waypoint_positions,
                waypoint_clearance,
                [obj.spawn_pose.position for obj in obstacle_objects],
                robot_diameter,
            ):
                # Generate random yaw angle (rotation) for the obstacle
                yaw = np.random.uniform(-math.pi, math.pi)  # Random rotation from -180° to +180°
                name = f"obstacle_{len(obstacle_objects)}"

                obstacle = StaticObject(
                    entity_name=name,
                    model=model,
                    spawn_pose=Pose(position=obstacle_pos, orientation=Orientation(yaw=yaw)),
                    xacro_arguments=xacro_arguments,
                )

                obstacle_objects.append(obstacle)

        return obstacle_objects

    def _distance(self, p1: Position, p2: Position) -> float:
        """Calculate Euclidean distance between two positions."""
        return math.sqrt((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2)

    def _select_random_segment(self, segments: List[dict], total_length: float) -> dict:
        """Select a random segment weighted by length."""
        if not segments:
            return segments[0]

        # Generate random value between 0 and total_length
        random_length = random.random() * total_length

        # Find the segment corresponding to this length
        current_length = 0.0
        for segment in segments:
            current_length += segment["length"]
            if random_length <= current_length:
                return segment

        # Fallback to last segment
        return segments[-1]

    def _generate_obstacle_position(
        self, path_point: Position, start: Position, end: Position, max_distance: float
    ) -> Position:
        """Generate obstacle position near a path point.

        Args:
            path_point: Point on the path
            start: Start of the path segment
            end: End of the path segment
            max_distance: Maximum distance from path

        Returns:
            Obstacle position
        """
        # Calculate path direction vector
        path_dx = end.x - start.x
        path_dy = end.y - start.y
        path_length = math.sqrt(path_dx**2 + path_dy**2)

        if path_length == 0:
            # Degenerate case - place obstacle randomly around point
            angle = random.random() * 2 * math.pi
            distance = random.random() * max_distance
            return Position(
                x=path_point.x + distance * math.cos(angle),
                y=path_point.y + distance * math.sin(angle),
            )

        # Normalize path direction
        path_dx /= path_length
        path_dy /= path_length

        # Calculate perpendicular direction (normal to path)
        normal_dx = -path_dy
        normal_dy = path_dx

        # Choose random side (left or right of path)
        side = random.choice([-1, 1])

        # Choose random distance from path
        distance = random.random() * max_distance

        # Add some randomness along the path direction as well
        along_path_offset = (random.random() - 0.5) * min(
            max_distance, path_length * 0.3
        )

        # Calculate obstacle position
        obstacle_x = (
            path_point.x + side * distance * normal_dx + along_path_offset * path_dx
        )
        obstacle_y = (
            path_point.y + side * distance * normal_dy + along_path_offset * path_dy
        )

        return Position(x=obstacle_x, y=obstacle_y)

    def validate_obstacle_placement(
        self, obstacles: List[Position], min_obstacle_distance: float = 0.5
    ) -> List[Position]:
        """Validate and filter obstacle positions to avoid overlaps.

        Args:
            obstacles: List of obstacle positions
            min_obstacle_distance: Minimum distance between obstacles

        Returns:
            Filtered list of obstacle positions
        """
        if not obstacles:
            return obstacles

        validated_obstacles = [obstacles[0]]  # Always keep the first obstacle

        for obstacle in obstacles[1:]:
            # Check if this obstacle is too close to any existing obstacle
            is_valid = True
            for existing in validated_obstacles:
                if self._distance(obstacle, existing) < min_obstacle_distance:
                    is_valid = False
                    break

            if is_valid:
                validated_obstacles.append(obstacle)

        return validated_obstacles

    def _is_valid_obstacle_position(
        self,
        obstacle_pos: Position,
        waypoints: List[Position],
        waypoint_clearance: float,
        existing_obstacles: List[Position],
        robot_diameter: float,
    ) -> bool:
        """Check if an obstacle position is valid (not too close to waypoints or other obstacles).

        Args:
            obstacle_pos: Position to validate
            waypoints: List of waypoint positions to avoid
            waypoint_clearance: Minimum distance from waypoints
            existing_obstacles: List of already placed obstacles
            robot_diameter: Diameter of the robot

        Returns:
            True if position is valid, False otherwise
        """
        # Check distance from waypoints
        for waypoint in waypoints:
            if self._distance(obstacle_pos, waypoint) < waypoint_clearance:
                return False

        # Check distance from existing obstacles (prevent overlap)
        min_obstacle_distance = (
            robot_diameter * 1.5
        )  # 1.5x robot diameter between obstacles
        for existing in existing_obstacles:
            if self._distance(obstacle_pos, existing) < min_obstacle_distance:
                return False

        return True
