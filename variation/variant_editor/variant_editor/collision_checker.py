#!/usr/bin/env python3
"""
Collision checking and pathfinding utilities for scenario GUI components.

This module provides the CollisionChecker class which handles:
- Map-based collision checking
- A* pathfinding algorithm
- Automatic waypoint generation based on map topology
- Path sequence validation
"""

import math
from typing import List, Optional, Tuple

import numpy as np


class CollisionChecker:
    """Utility class for checking collisions and path validity."""

    def __init__(self, map_data: np.ndarray, resolution: float, origin: List[float]):
        """
        Initialize collision checker with map data.

        Args:
            map_data: 2D numpy array where 0=free, 255=occupied, 127=unknown
            resolution: Map resolution in meters per pixel
            origin: Map origin [x, y, theta] in world coordinates
        """
        self.map_data = map_data
        self.resolution = resolution
        self.origin = origin
        self.height, self.width = map_data.shape

        # Create binary occupancy grid (True = occupied, False = free)
        # In occupancy grids: 255 = free (white), 127 = unknown (gray), 0 = occupied (black)
        # We treat values below 200 as occupied (black/dark gray)
        self.occupancy_grid = (map_data < 200).astype(bool)

    def world_to_grid(self, x: float, y: float) -> Tuple[int, int]:
        """Convert world coordinates to grid coordinates."""
        grid_x = int((x - self.origin[0]) / self.resolution)
        grid_y = int(self.height - (y - self.origin[1]) / self.resolution)  # Flip Y
        return grid_x, grid_y

    def grid_to_world(self, grid_x: int, grid_y: int) -> Tuple[float, float]:
        """Convert grid coordinates to world coordinates."""
        world_x = grid_x * self.resolution + self.origin[0]
        world_y = (self.height - grid_y) * self.resolution + self.origin[1]  # Flip Y
        return world_x, world_y

    def is_valid_position(self, x: float, y: float, robot_radius: float) -> bool:
        """
        Check if a robot can be placed at the given world position.

        Args:
            x, y: World coordinates
            robot_radius: Robot radius in meters

        Returns:
            True if position is valid (free space), False otherwise
        """
        grid_x, grid_y = self.world_to_grid(x, y)

        # Check if center is within map bounds
        if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
            return False

        # Calculate robot radius in grid cells (limit max radius for
        # performance)
        radius_cells = min(int(np.ceil(robot_radius / self.resolution)), 10)

        # Check circular area around robot center
        for dy in range(-radius_cells, radius_cells + 1):
            for dx in range(-radius_cells, radius_cells + 1):
                # Check if point is within robot's circular footprint
                if dx * dx + dy * dy <= radius_cells * radius_cells:
                    check_x = grid_x + dx
                    check_y = grid_y + dy

                    # Check bounds
                    if not (0 <= check_x < self.width and 0 <= check_y < self.height):
                        return False

                    # Check if occupied
                    if self.occupancy_grid[check_y, check_x]:
                        return False

        return True

    def find_path(
        self,
        start_x: float,
        start_y: float,
        goal_x: float,
        goal_y: float,
        robot_radius: float,
    ) -> Optional[List[Tuple[float, float]]]:
        """
        Fast path checking using simplified reachability test.

        Args:
            start_x, start_y: Start position in world coordinates
            goal_x, goal_y: Goal position in world coordinates
            robot_radius: Robot radius in meters

        Returns:
            Simple path as [start, goal] if reachable, or None if not reachable
        """
        # Check if start and goal are valid
        if not self.is_valid_position(start_x, start_y, robot_radius):
            return None
        if not self.is_valid_position(goal_x, goal_y, robot_radius):
            return None

        # Fast reachability check using simplified flood fill
        if self._is_reachable_fast(start_x, start_y, goal_x, goal_y, robot_radius):
            return [(start_x, start_y), (goal_x, goal_y)]

        return None

    def check_path_sequence(
        self, waypoints: List[Tuple[float, float]], robot_radius: float
    ) -> List[bool]:
        """
        Fast check if a sequence of waypoints is routable.

        Args:
            waypoints: List of (x, y) world coordinates
            robot_radius: Robot radius in meters

        Returns:
            List of booleans indicating if each waypoint is reachable from the previous one
        """
        if len(waypoints) < 2:
            return [True] * len(waypoints)

        results = [True]  # First waypoint is always "reachable"

        for i in range(1, len(waypoints)):
            prev_x, prev_y = waypoints[i - 1]
            curr_x, curr_y = waypoints[i]

            # Check if current position is valid (use fast version)
            if not self._is_valid_position_fast(curr_x, curr_y, robot_radius):
                results.append(False)
                continue

            # Use fast reachability check instead of full pathfinding
            reachable = self._is_reachable_fast(
                prev_x, prev_y, curr_x, curr_y, robot_radius
            )
            results.append(reachable)

        return results

    def generate_meaningful_waypoints(
        self, robot_radius: float, num_waypoints: int = 5, min_distance: float = 2.0
    ) -> List[Tuple[float, float]]:
        """
        Generate meaningful waypoints automatically based on map topology.

        Args:
            robot_radius: Robot radius in meters
            num_waypoints: Target number of waypoints to generate
            min_distance: Minimum distance between waypoints in meters

        Returns:
            List of (x, y) world coordinates for meaningful waypoints
        """
        if not self.map_data.size:
            return []

        # Find all free space positions (sample less densely for performance)
        free_positions = []
        step_size = max(
            2, int(robot_radius / self.resolution * 2)
        )  # Larger step size for performance

        for y in range(step_size, self.height - step_size, step_size):
            for x in range(step_size, self.width - step_size, step_size):
                world_x, world_y = self.grid_to_world(x, y)
                if self.is_valid_position(world_x, world_y, robot_radius):
                    free_positions.append((world_x, world_y))

        if len(free_positions) < num_waypoints:
            return free_positions

        # Use a strategic selection approach to find meaningful waypoints
        selected_waypoints = []

        # 1. Find corners and interesting topology features
        corner_positions = self._find_topology_features(robot_radius)

        # 2. Start with the most central position
        if free_positions:
            center_x = sum(pos[0] for pos in free_positions) / len(free_positions)
            center_y = sum(pos[1] for pos in free_positions) / len(free_positions)

            # Find closest valid position to center
            center_pos = min(
                free_positions,
                key=lambda pos: (pos[0] - center_x) ** 2 + (pos[1] - center_y) ** 2,
            )
            selected_waypoints.append(center_pos)

        # 3. Add corner/feature positions that are far enough apart
        for corner in corner_positions:
            if self._is_far_enough(corner, selected_waypoints, min_distance):
                selected_waypoints.append(corner)
                if len(selected_waypoints) >= num_waypoints:
                    break

        # 4. Fill remaining slots with well-distributed positions
        remaining_slots = num_waypoints - len(selected_waypoints)
        if remaining_slots > 0:
            # Use k-means-like clustering to find well-distributed points
            additional_points = self._find_distributed_points(
                free_positions, selected_waypoints, remaining_slots, min_distance
            )
            selected_waypoints.extend(additional_points)

        return selected_waypoints[:num_waypoints]

    def _find_topology_features(self, robot_radius: float) -> List[Tuple[float, float]]:
        """Find interesting topology features like corners, room centers, and corridor junctions."""
        features = []

        # Create a larger-scale grid for feature detection
        feature_step = max(5, int(2 * robot_radius / self.resolution))

        for y in range(feature_step, self.height - feature_step, feature_step):
            for x in range(feature_step, self.width - feature_step, feature_step):
                world_x, world_y = self.grid_to_world(x, y)

                if not self.is_valid_position(world_x, world_y, robot_radius):
                    continue

                # Check if this is an interesting feature
                feature_score = self._calculate_feature_score(x, y, robot_radius)

                if feature_score > 0.3:  # Threshold for interesting features
                    features.append((world_x, world_y))

        # Sort by feature score (most interesting first)
        features.sort(
            key=lambda pos: self._calculate_feature_score(
                *self.world_to_grid(pos[0], pos[1]), robot_radius
            ),
            reverse=True,
        )

        return features[:10]  # Return top 10 features

    def _calculate_feature_score(
        self, grid_x: int, grid_y: int, robot_radius: float
    ) -> float:
        """Calculate how interesting a position is as a navigation waypoint."""
        radius_cells = int(np.ceil(robot_radius / self.resolution))

        # Check the local neighborhood
        free_directions = 0
        obstacle_directions = 0

        # Check 8 main directions
        directions = [
            (-1, -1),
            (-1, 0),
            (-1, 1),
            (0, -1),
            (0, 1),
            (1, -1),
            (1, 0),
            (1, 1),
        ]

        for dx, dy in directions:
            check_distance = radius_cells * 3  # Look 3 robot radii away
            check_x = grid_x + dx * check_distance
            check_y = grid_y + dy * check_distance

            if 0 <= check_x < self.width and 0 <= check_y < self.height:
                if self.occupancy_grid[check_y, check_x]:
                    obstacle_directions += 1
                else:
                    free_directions += 1

        # Calculate openness (how open the space is)
        openness = free_directions / len(directions)

        # Calculate complexity (mix of free and occupied nearby)
        complexity = min(free_directions, obstacle_directions) / len(directions)

        # Corner detection (positions with good balance of open and blocked
        # directions)
        corner_score = complexity * 2

        # Room center detection (very open areas)
        room_center_score = openness if openness > 0.7 else 0

        # Junction detection (areas with multiple free paths)
        junction_score = 0
        if free_directions >= 6:  # Mostly open with some structure
            junction_score = 0.5

        # Combine scores
        feature_score = max(corner_score, room_center_score, junction_score)

        # Bonus for positions that are well inside free space (not too close to
        # walls)
        distance_to_wall = self._distance_to_nearest_obstacle(grid_x, grid_y)
        if distance_to_wall > robot_radius * 2:
            feature_score += 0.2

        return feature_score

    def _distance_to_nearest_obstacle(self, grid_x: int, grid_y: int) -> float:
        """Calculate distance to nearest obstacle in grid cells."""
        max_search = 20  # Limit search radius for performance

        for radius in range(1, max_search):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if abs(dx) == radius or abs(dy) == radius:  # Only check perimeter
                        check_x = grid_x + dx
                        check_y = grid_y + dy

                        if (
                            0 <= check_x < self.width
                            and 0 <= check_y < self.height
                            and self.occupancy_grid[check_y, check_x]
                        ):
                            return radius * self.resolution

        return max_search * self.resolution

    def _is_far_enough(
        self,
        position: Tuple[float, float],
        existing_positions: List[Tuple[float, float]],
        min_distance: float,
    ) -> bool:
        """Check if a position is far enough from existing positions."""
        x, y = position
        for ex_x, ex_y in existing_positions:
            distance = math.sqrt((x - ex_x) ** 2 + (y - ex_y) ** 2)
            if distance < min_distance:
                return False
        return True

    def _find_distributed_points(
        self,
        candidates: List[Tuple[float, float]],
        existing: List[Tuple[float, float]],
        num_points: int,
        min_distance: float,
    ) -> List[Tuple[float, float]]:
        """Find well-distributed points using a greedy approach."""
        selected = []
        remaining_candidates = [
            pos
            for pos in candidates
            if self._is_far_enough(pos, existing, min_distance)
        ]

        while len(selected) < num_points and remaining_candidates:
            if not selected and not existing:
                # If no existing points, pick a random starting point
                next_point = remaining_candidates[len(remaining_candidates) // 2]
            else:
                # Pick the point that maximizes minimum distance to all
                # existing points
                def min_distance_to_existing(pos):
                    all_existing = existing + selected
                    if not all_existing:
                        return float("inf")
                    return min(
                        math.sqrt((pos[0] - ex[0]) ** 2 + (pos[1] - ex[1]) ** 2)
                        for ex in all_existing
                    )

                next_point = max(remaining_candidates, key=min_distance_to_existing)

            selected.append(next_point)
            # Remove points too close to the selected one
            remaining_candidates = [
                pos
                for pos in remaining_candidates
                if self._is_far_enough(pos, [next_point], min_distance)
            ]

        return selected

    def _is_reachable_fast(
        self,
        start_x: float,
        start_y: float,
        goal_x: float,
        goal_y: float,
        robot_radius: float,
    ) -> bool:
        """
        Fast reachability check using simplified flood fill algorithm.
        Much faster than A* but only checks if goal is reachable, not optimal path.
        """
        start_grid = self.world_to_grid(start_x, start_y)
        goal_grid = self.world_to_grid(goal_x, goal_y)

        # If start and goal are very close, just check line of sight
        distance = math.sqrt((goal_x - start_x) ** 2 + (goal_y - start_y) ** 2)
        if distance < robot_radius * 3:
            return self._has_line_of_sight(
                start_x, start_y, goal_x, goal_y, robot_radius
            )

        # Use simplified BFS with larger step size for speed
        visited = set()
        queue = [start_grid]
        visited.add(start_grid)

        # Larger step size for faster search (trade accuracy for speed)
        step_size = max(1, int(robot_radius / self.resolution))

        # Limit search area and iterations for performance
        max_distance = int(distance / self.resolution) + 20
        max_iterations = min(1000, self.width * self.height // 100)
        iterations = 0

        while queue and iterations < max_iterations:
            iterations += 1
            current = queue.pop(0)

            # Check if we reached the goal area (with some tolerance)
            goal_tolerance = max(2, step_size)
            if (
                abs(current[0] - goal_grid[0]) <= goal_tolerance
                and abs(current[1] - goal_grid[1]) <= goal_tolerance
            ):
                return True

            # Explore neighbors with larger steps
            for dx in range(-step_size, step_size + 1, step_size):
                for dy in range(-step_size, step_size + 1, step_size):
                    if dx == 0 and dy == 0:
                        continue

                    neighbor = (current[0] + dx, current[1] + dy)

                    # Skip if already visited
                    if neighbor in visited:
                        continue

                    # Check bounds
                    if not (
                        0 <= neighbor[0] < self.width and 0 <= neighbor[1] < self.height
                    ):
                        continue

                    # Skip if too far from start (limit search area)
                    if (
                        abs(neighbor[0] - start_grid[0]) > max_distance
                        or abs(neighbor[1] - start_grid[1]) > max_distance
                    ):
                        continue

                    # Quick collision check (simplified)
                    neighbor_world = self.grid_to_world(neighbor[0], neighbor[1])
                    if self._is_valid_position_fast(
                        neighbor_world[0], neighbor_world[1], robot_radius
                    ):
                        visited.add(neighbor)
                        queue.append(neighbor)

        return False

    def _has_line_of_sight(
        self,
        start_x: float,
        start_y: float,
        goal_x: float,
        goal_y: float,
        robot_radius: float,
    ) -> bool:
        """Check if there's a direct line of sight between two points."""
        start_grid = self.world_to_grid(start_x, start_y)
        goal_grid = self.world_to_grid(goal_x, goal_y)

        # Use Bresenham's line algorithm to check points along the line
        dx = abs(goal_grid[0] - start_grid[0])
        dy = abs(goal_grid[1] - start_grid[1])

        steps = max(dx, dy)
        if steps == 0:
            return True

        x_step = (goal_grid[0] - start_grid[0]) / steps
        y_step = (goal_grid[1] - start_grid[1]) / steps

        # Check fewer points along the line for speed
        check_interval = max(1, int(robot_radius / self.resolution))

        for i in range(0, steps + 1, check_interval):
            check_x = int(start_grid[0] + i * x_step)
            check_y = int(start_grid[1] + i * y_step)

            check_world = self.grid_to_world(check_x, check_y)
            if not self._is_valid_position_fast(
                check_world[0], check_world[1], robot_radius
            ):
                return False

        return True

    def _is_valid_position_fast(self, x: float, y: float, robot_radius: float) -> bool:
        """
        Faster version of position validation with reduced accuracy.
        Uses fewer check points around the robot for speed.
        """
        grid_x, grid_y = self.world_to_grid(x, y)

        # Check if center is within map bounds
        if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
            return False

        # Use smaller radius check for speed (less conservative)
        radius_cells = max(
            1, int(robot_radius / self.resolution * 0.8)
        )  # 80% of actual radius

        # Check fewer points around robot center for speed
        check_points = [
            (0, 0),  # Center
            (-radius_cells, 0),
            (radius_cells, 0),  # Left, Right
            (0, -radius_cells),
            (0, radius_cells),  # Up, Down
            (-radius_cells, -radius_cells),
            (radius_cells, radius_cells),  # Diagonals
            (-radius_cells, radius_cells),
            (radius_cells, -radius_cells),
        ]

        for dx, dy in check_points:
            check_x = grid_x + dx
            check_y = grid_y + dy

            # Check bounds
            if not (0 <= check_x < self.width and 0 <= check_y < self.height):
                return False

            # Check if occupied
            if self.occupancy_grid[check_y, check_x]:
                return False

        return True
