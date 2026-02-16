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
from typing import List

import numpy as np

from .data_model import Orientation, Pose, Position
from .map_loader import Map, load_map


class WaypointGenerator:
    """Class for generating valid waypoints within a map considering robot size."""

    def __init__(self, map_file_path: str):
        """
        Initialize waypoint generator with a map file.

        Args:
            map_file_path: Path to the map YAML file
        """
        self.map_file_path = map_file_path
        self.map: Map = None

        self.load_map()

    def load_map(self):
        """Load the map file and initialize internal data structures."""
        try:
            # Load map using shared map_loader utility
            self.map = load_map(self.map_file_path)

        except Exception as e:
            print(f"Error loading map {self.map_file_path}: {e}")
            self.map = None

    def generate_waypoints(
        self, num_waypoints: int, robot_diameter: float, min_distance: float = 0.0, max_distance: float = None, initial_start_pose=None
    ) -> List[Pose]:
        """Generate random valid waypoints."""
        if self.map is None or self.map.occupancy_grid is None:
            raise ValueError("Occupancy grid not loaded")

        waypoints = []
        max_attempts = num_waypoints * 50  # Limit total attempts

        # Get map bounds in world coordinates
        min_x = self.map.origin_x
        max_x = min_x + self.map.width * self.map.resolution
        min_y = self.map.origin_y
        max_y = min_y + self.map.height * self.map.resolution

        attempts = 0
        while len(waypoints) < num_waypoints and attempts < max_attempts:
            attempts += 1

            # Generate random position within map bounds
            if (waypoints or initial_start_pose is not None) and max_distance is not None:
                # Determine reference point (last waypoint or initial start pose)
                if waypoints:
                    ref_x, ref_y = waypoints[-1]
                else:
                    ref_x = initial_start_pose.position.x
                    ref_y = initial_start_pose.position.y

                # Generate point within annular region between min_distance and max_distance
                # Use polar coordinates: random angle and random radius
                angle = np.random.uniform(0, 2 * math.pi)
                # Generate radius between min_distance and max_distance with uniform distribution
                # Use sqrt for uniform distribution in annular region
                radius = math.sqrt(np.random.uniform(min_distance**2, max_distance**2))

                x = ref_x + radius * math.cos(angle)
                y = ref_y + radius * math.sin(angle)

                # Clamp to map bounds
                x = np.clip(x, min_x, max_x)
                y = np.clip(y, min_y, max_y)
            else:
                # Generate first waypoint or no max_distance constraint
                x = np.random.uniform(min_x, max_x)
                y = np.random.uniform(min_y, max_y)

            # Check if position is valid (only collision checking needed now)
            if self.is_valid_position(x, y, robot_diameter/2.0):
                waypoints.append((x, y))

        result = []
        for x, y in waypoints:
            yaw = np.random.uniform(-math.pi, math.pi)
            result.append(Pose(position=Position(x=x, y=y), orientation=Orientation(yaw=yaw)))
        return result

    def is_valid_position(self, x: float, y: float, robot_radius: float) -> bool:
        """
        Check if a robot can be placed at the given world position.
        Uses the occupancy grid to determine if space is free.

        Args:
            x, y: World coordinates
            robot_radius: Robot radius in meters

        Returns:
            True if position is valid (free space), False otherwise
        """
        grid_x, grid_y = self.map.world_to_grid(x, y)

        # Check if center is within map bounds
        if not (0 <= grid_x < self.map.width and 0 <= grid_y < self.map.height):
            return False

        # Calculate robot radius in grid cells
        radius_cells = min(int(np.ceil(robot_radius / self.map.resolution)), 10)

        # Check circular area around robot center - all must be free space
        for dy in range(-radius_cells, radius_cells + 1):
            for dx in range(-radius_cells, radius_cells + 1):
                # Check if point is within robot's circular footprint
                if dx * dx + dy * dy <= radius_cells * radius_cells:
                    check_x = grid_x + dx
                    check_y = grid_y + dy

                    # Check bounds
                    if not (0 <= check_x < self.map.width and 0 <= check_y < self.map.height):
                        return False

                    # Check if this cell is occupied using the occupancy grid
                    # occupancy_grid[y, x] = True means occupied
                    if self.map.occupancy_grid[check_y, check_x]:
                        return False

        return True
