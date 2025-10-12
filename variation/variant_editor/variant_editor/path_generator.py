#!/usr/bin/env python3
"""
Standalone path generation utilities with minimal A* algorithm.

This module provides path finding capabilities using A* algorithm
on occupancy grid maps where only white pixels are considered free space.
"""

import heapq
import math
import os
from typing import List, Optional, Tuple

import numpy as np
import scipy.ndimage  # Add this import for distance transform
import yaml
from PIL import Image
from variant_editor.data_models import Pose, Position, StaticObject
from variant_editor.object_shapes import (ObjectShapeRenderer,
                                          get_object_type_from_model_path,
                                          get_obstacle_dimensions)


class PathGenerator:
    """Standalone utility class for generating navigation paths on maps using A* algorithm."""

    def __init__(self, map_file_path: str, robot_diameter: float = 0.4):
        """
        Initialize path generator with a map file and robot diameter.

        Args:
            map_file_path: Path to the map YAML file
            robot_diameter: Diameter of the robot in meters (used for obstacle inflation)
        """
        self.map_file_path = map_file_path
        self.map_data = None
        self.map_resolution = 0.05
        self.map_origin = [0.0, 0.0, 0.0]
        self.occupancy_grid = None
        self.height = 0
        self.width = 0
        self.robot_diameter = robot_diameter
        self.robot_radius = robot_diameter / 2.0
        self.shape_renderer = ObjectShapeRenderer()

        self._load_map()

    def _inflate_obstacles(self):
        """Inflate obstacles in the occupancy grid by the robot's radius."""
        if self.occupancy_grid is None:
            return

        # Compute the number of pixels to inflate
        inflation_radius_px = int(np.ceil(self.robot_radius / self.map_resolution))
        if inflation_radius_px <= 0:
            return

        # Use distance transform to inflate obstacles
        # Occupied cells are True, free are False
        if self.occupancy_grid is not None:
            distance = scipy.ndimage.distance_transform_edt(~self.occupancy_grid)
            inflated_grid = distance <= inflation_radius_px
            self.occupancy_grid = inflated_grid

    def _load_map(self):
        """Load the map file and initialize internal data structures."""
        try:
            map_dir = os.path.dirname(self.map_file_path)
            # Load map YAML
            with open(self.map_file_path, "r") as f:
                map_config = yaml.safe_load(f)

            # Get map parameters
            image_file = map_config.get("image", "")
            if not os.path.isabs(image_file):
                image_file = os.path.join(map_dir, image_file)

            self.map_resolution = map_config.get("resolution", 0.05)
            self.map_origin = map_config.get("origin", [0.0, 0.0, 0.0])

            # Load map image
            if os.path.exists(image_file):
                map_image = Image.open(image_file)
                if map_image.mode != "L":
                    map_image = map_image.convert("L")

                self.map_data = np.array(map_image)
                self.height, self.width = self.map_data.shape

                # Create binary occupancy grid where only white pixels (255) are free
                # Everything else is considered occupied
                self.occupancy_grid = (self.map_data != 255).astype(bool)

                # Inflate obstacles for robot size
                self._inflate_obstacles()

            else:
                raise FileNotFoundError(f"Map image file not found: {image_file}")

        except Exception as e:
            print(f"Error loading map {self.map_file_path}: {e}")
            self.map_data = None
            self.occupancy_grid = None

    def world_to_grid(self, x: float, y: float) -> Tuple[int, int]:
        """Convert world coordinates to grid coordinates."""
        grid_x = int((x - self.map_origin[0]) / self.map_resolution)
        grid_y = int(
            self.height - (y - self.map_origin[1]) / self.map_resolution
        )  # Flip Y
        return grid_x, grid_y

    def grid_to_world(self, grid_x: int, grid_y: int) -> Tuple[float, float]:
        """Convert grid coordinates to world coordinates."""
        world_x = grid_x * self.map_resolution + self.map_origin[0]
        world_y = (self.height - grid_y) * self.map_resolution + self.map_origin[
            1
        ]  # Flip Y
        return world_x, world_y

    def _is_valid_grid_position(self, grid_x: int, grid_y: int) -> bool:
        """Check if grid position is valid and free."""
        if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
            return False
        return not self.occupancy_grid[grid_y, grid_x]

    def _heuristic(self, a: Tuple[int, int], b: Tuple[int, int]) -> float:
        """Calculate Manhattan distance heuristic for A*."""
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def _get_neighbors(
        self, pos: Tuple[int, int]
    ) -> List[Tuple[Tuple[int, int], float]]:
        """Get valid neighboring positions with movement costs."""
        x, y = pos
        neighbors = []

        # 8-directional movement
        directions = [
            (-1, -1, 1.414),
            (-1, 0, 1.0),
            (-1, 1, 1.414),
            (0, -1, 1.0),
            (0, 1, 1.0),
            (1, -1, 1.414),
            (1, 0, 1.0),
            (1, 1, 1.414),
        ]

        for dx, dy, cost in directions:
            new_x, new_y = x + dx, y + dy
            if self._is_valid_grid_position(new_x, new_y):
                neighbors.append(((new_x, new_y), cost))

        return neighbors

    def _a_star(
        self, start: Tuple[int, int], goal: Tuple[int, int]
    ) -> Optional[List[Tuple[int, int]]]:
        """
        Minimal A* pathfinding algorithm.

        Args:
            start: Start grid position (grid_x, grid_y)
            goal: Goal grid position (grid_x, grid_y)

        Returns:
            List of grid positions forming the path, or None if no path found
        """
        if not self._is_valid_grid_position(*start) or not self._is_valid_grid_position(
            *goal
        ):
            return None

        if start == goal:
            return [start]

        # Priority queue: (f_score, position)
        open_set = [(0, start)]
        came_from = {}
        g_score = {start: 0}
        f_score = {start: self._heuristic(start, goal)}

        open_set_hash = {start}

        while open_set:
            current = heapq.heappop(open_set)[1]
            open_set_hash.discard(current)

            if current == goal:
                # Reconstruct path
                path = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.append(start)
                return path[::-1]  # Reverse to get start->goal order

            for neighbor, move_cost in self._get_neighbors(current):
                tentative_g_score = g_score[current] + move_cost

                if neighbor not in g_score or tentative_g_score < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g_score
                    f_score[neighbor] = tentative_g_score + self._heuristic(
                        neighbor, goal
                    )

                    if neighbor not in open_set_hash:
                        heapq.heappush(open_set, (f_score[neighbor], neighbor))
                        open_set_hash.add(neighbor)

        return None  # No path found

    def generate_path(
        self, waypoints: List[Pose], obstacles: List[StaticObject] = None
    ) -> Optional[List[Position]]:
        """
        Generate a path through the given waypoints using A* algorithm.

        Args:
            waypoints: List of Pose objects to traverse
            obstacles: Optional list of dynamic obstacles to consider

        Returns:
            List of Position objects forming a valid path, or None if no path exists
        """
        if self.occupancy_grid is None or not waypoints:
            return None

        if len(waypoints) < 2:
            raise ValueError("At least two waypoints are required to generate a path.")

        # Create a copy of the occupancy grid to avoid modifying the original
        original_grid = self.occupancy_grid.copy()

        try:
            # Add dynamic obstacles if provided
            if obstacles:
                self.add_dynamic_obstacles(obstacles)

            # Convert waypoints to grid coordinates
            grid_waypoints = []
            for pose in waypoints:
                grid_x, grid_y = self.world_to_grid(pose.position.x, pose.position.y)
                if not self._is_valid_grid_position(grid_x, grid_y):
                    return None  # Invalid waypoint
                grid_waypoints.append((grid_x, grid_y))

            # Find path through all waypoints
            full_path = []

            for i in range(len(grid_waypoints) - 1):
                start = grid_waypoints[i]
                goal = grid_waypoints[i + 1]

                segment_path = self._a_star(start, goal)
                if segment_path is None:
                    return None  # No path between waypoints

                # Add segment to full path (avoid duplicating waypoints)
                if i == 0:
                    full_path.extend(segment_path)
                else:
                    full_path.extend(
                        segment_path[1:]
                    )  # Skip first point (already in path)

            # Convert back to Position objects (with default orientation)
            world_path = []
            for grid_x, grid_y in full_path:
                world_x, world_y = self.grid_to_world(grid_x, grid_y)
                # Use default orientation (0,0,0,1) for each pose
                pos = Position(x=world_x, y=world_y)
                world_path.append(pos)

            return world_path

        finally:
            # Restore original occupancy grid
            self.occupancy_grid = original_grid

    def _add_circular_obstacle(self, center_x: int, center_y: int, radius: float):
        """
        Add a circular obstacle to the occupancy grid with robot inflation.

        Args:
            center_x, center_y: Center position in grid coordinates
            radius: Radius of the obstacle in meters
        """
        # Inflate obstacle by robot radius for safe navigation
        inflated_radius = radius + self.robot_radius
        radius_cells = int(np.ceil(inflated_radius / self.map_resolution))

        # Mark grid cells as occupied in a circular area around the obstacle
        for dy in range(-radius_cells, radius_cells + 1):
            for dx in range(-radius_cells, radius_cells + 1):
                if dx * dx + dy * dy <= radius_cells * radius_cells:
                    check_x = center_x + dx
                    check_y = center_y + dy

                    # Check bounds
                    if 0 <= check_x < self.width and 0 <= check_y < self.height:
                        self.occupancy_grid[check_y, check_x] = True

    def _add_rectangular_obstacle(
        self,
        center_x: int,
        center_y: int,
        width: float,
        length: float,
        yaw: float = 0.0,
    ):
        """
        Add a rectangular obstacle to the occupancy grid with robot inflation.

        Args:
            center_x, center_y: Center position in grid coordinates
            width: Width of the obstacle in meters
            length: Length of the obstacle in meters
            yaw: Rotation angle in radians (default: 0.0)
        """
        # Inflate obstacle dimensions by robot radius for safe navigation
        inflated_width = width + 2 * self.robot_radius
        inflated_length = length + 2 * self.robot_radius

        # Convert dimensions to grid cells
        width_cells = inflated_width / self.map_resolution
        length_cells = inflated_length / self.map_resolution

        # Calculate half dimensions
        half_width = width_cells / 2
        half_length = length_cells / 2

        # Pre-calculate trigonometric values
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)

        # Determine the bounding box of the rotated rectangle
        # Corner points in local coordinates
        corners = [
            (-half_width, -half_length),
            (half_width, -half_length),
            (half_width, half_length),
            (-half_width, half_length),
        ]

        # Rotate corners and find bounding box
        rotated_corners = []
        for x, y in corners:
            rx = x * cos_yaw - y * sin_yaw
            ry = x * sin_yaw + y * cos_yaw
            rotated_corners.append((rx, ry))

        # Calculate bounding box
        min_x = min(corner[0] for corner in rotated_corners)
        max_x = max(corner[0] for corner in rotated_corners)
        min_y = min(corner[1] for corner in rotated_corners)
        max_y = max(corner[1] for corner in rotated_corners)

        # Expand search area to grid cells
        search_min_x = int(np.floor(center_x + min_x))
        search_max_x = int(np.ceil(center_x + max_x))
        search_min_y = int(np.floor(center_y + min_y))
        search_max_y = int(np.ceil(center_y + max_y))

        # Check each grid cell in the bounding box
        for grid_y in range(search_min_y, search_max_y + 1):
            for grid_x in range(search_min_x, search_max_x + 1):
                # Check bounds
                if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
                    continue

                # Transform grid cell to obstacle local coordinates
                local_x = grid_x - center_x
                local_y = grid_y - center_y

                # Rotate to align with obstacle orientation
                rotated_x = local_x * cos_yaw + local_y * sin_yaw
                rotated_y = -local_x * sin_yaw + local_y * cos_yaw

                # Check if point is inside rectangle
                if abs(rotated_x) <= half_width and abs(rotated_y) <= half_length:
                    self.occupancy_grid[grid_y, grid_x] = True

    def add_dynamic_obstacles(self, obstacles: List[StaticObject]):
        """
        Add dynamic obstacles to the occupancy grid using correct shapes.

        Args:
            obstacles: List of StaticObject instances to add as obstacles
        """
        if self.occupancy_grid is None or not obstacles:
            return

        for obstacle in obstacles:
            # Get obstacle position in grid coordinates
            obs_grid_x, obs_grid_y = self.world_to_grid(
                obstacle.pose.position.x, obstacle.pose.position.y
            )

            # Get obstacle type and dimensions
            obstacle_type = get_object_type_from_model_path(obstacle.model)
            dimensions = get_obstacle_dimensions(
                obstacle.xacro_arguments, self.shape_renderer
            )

            if obstacle_type == "cylinder":
                # Use circular shape for cylinders
                radius = dimensions["radius"]
                self._add_circular_obstacle(obs_grid_x, obs_grid_y, radius)

            elif obstacle_type == "box":
                # Use rectangular shape for boxes
                width = dimensions["width"]
                length = dimensions["length"]
                yaw = obstacle.pose.yaw  # Use the pose orientation
                self._add_rectangular_obstacle(
                    obs_grid_x, obs_grid_y, width, length, yaw
                )

            else:
                # Unknown type - fall back to circular shape with conservative radius
                # Use the maximum dimension to be safe and add robot radius for
                # inflation
                base_radius = max(
                    dimensions.get("radius", 0.25),
                    dimensions.get("width", 0.5) / 2,
                    dimensions.get("length", 0.5) / 2,
                )
                self._add_circular_obstacle(obs_grid_x, obs_grid_y, base_radius)

    def get_costmap_with_obstacles(
        self, obstacles: List[StaticObject] = None
    ) -> Optional[np.ndarray]:
        """
        Generate a costmap that includes dynamic obstacles.

        Args:
            obstacles: Optional list of dynamic obstacles to include

        Returns:
            Costmap as numpy array where 0=free, 255=occupied, or None if no map loaded
        """
        if self.occupancy_grid is None:
            return None

        # Create a copy of the occupancy grid to avoid modifying the original
        original_grid = self.occupancy_grid.copy()

        try:
            # Add dynamic obstacles if provided
            if obstacles:
                self.add_dynamic_obstacles(obstacles)

            # Convert boolean occupancy grid to costmap values
            # True (occupied) -> 255 (black), False (free) -> 0 (white)
            costmap = self.occupancy_grid.astype(np.uint8) * 255

            return costmap

        finally:
            # Restore original occupancy grid
            self.occupancy_grid = original_grid
