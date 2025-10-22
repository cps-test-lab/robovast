import math
import os
from typing import List, Tuple

import numpy as np
import yaml
from PIL import Image

from ..data_model import Orientation, Pose, Position


class WaypointGenerator:
    """Class for generating valid waypoints within a map considering robot size."""

    def __init__(self, map_file_path: str):
        """
        Initialize waypoint generator with a map file.

        Args:
            map_file_path: Path to the map YAML file
        """
        self.map_file_path = map_file_path
        self.map_data = None
        self.map_resolution = 0.05
        self.map_origin = [0.0, 0.0, 0.0]
        self.occupancy_grid = None
        self.height = 0
        self.width = 0

        self.load_map()

    def load_map(self):
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

                # Create binary occupancy grid (True = occupied, False = free)
                # In occupancy grids: 255 = free (white), 127 = unknown (gray), 0 = occupied (black)
                # Only allow waypoints in white areas (255), treat everything
                # else as occupied
                self.occupancy_grid = (self.map_data < 250).astype(bool)

            else:
                raise FileNotFoundError(f"Map image file not found: {image_file}")

        except Exception as e:
            print(f"Error loading map {self.map_file_path}: {e}")
            self.map_data = None
            self.occupancy_grid = None

    def generate_waypoints(
        self, num_waypoints: int, robot_diameter: float, min_distance: float = 2.0
    ) -> List[dict]:
        """
        Generate random valid waypoints within the map considering robot size.

        Args:
            num_waypoints: Number of waypoints to generate
            robot_diameter: Robot diameter in meters (will be converted to radius)
            min_distance: Minimum distance between waypoints in meters

        Returns:
            List of Pose objects with position and random yyaw
        """
        if self.occupancy_grid is None:
            return []

        robot_radius = robot_diameter / 2.0
        waypoints = self._generate_random_waypoints(
            num_waypoints, robot_radius, min_distance
        )

        # Convert to Pose objects with default yaw=0.0
        result = []
        for x, y in waypoints:
            yaw = np.random.uniform(-math.pi, math.pi)
            result.append(Pose(position=Position(x=x, y=y), orientation=Orientation(yaw=yaw)))
        return result

    def _generate_random_waypoints(
        self, num_waypoints: int, robot_radius: float, min_distance: float
    ) -> List[Tuple[float, float]]:
        """Generate random valid waypoints."""
        waypoints = []
        max_attempts = num_waypoints * 50  # Limit total attempts

        # Get map bounds in world coordinates
        min_x = self.map_origin[0]
        max_x = min_x + self.width * self.map_resolution
        min_y = self.map_origin[1]
        max_y = min_y + self.height * self.map_resolution

        attempts = 0
        while len(waypoints) < num_waypoints and attempts < max_attempts:
            attempts += 1

            # Generate random position within map bounds
            x = np.random.uniform(min_x, max_x)
            y = np.random.uniform(min_y, max_y)

            # Check if position is valid
            if self.is_valid_position(x, y, robot_radius) and self._is_far_enough(
                (x, y), waypoints, min_distance
            ):
                waypoints.append((x, y))

        return waypoints

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

    def is_valid_position(self, x: float, y: float, robot_radius: float) -> bool:
        """
        Check if a robot can be placed at the given world position.
        Only allows positions in white areas of the map.

        Args:
            x, y: World coordinates
            robot_radius: Robot radius in meters

        Returns:
            True if position is valid (free white space), False otherwise
        """
        grid_x, grid_y = self.world_to_grid(x, y)

        # Check if center is within map bounds
        if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
            return False

        # Calculate robot radius in grid cells
        radius_cells = min(int(np.ceil(robot_radius / self.map_resolution)), 10)

        # Check circular area around robot center - all must be in white areas
        for dy in range(-radius_cells, radius_cells + 1):
            for dx in range(-radius_cells, radius_cells + 1):
                # Check if point is within robot's circular footprint
                if dx * dx + dy * dy <= radius_cells * radius_cells:
                    check_x = grid_x + dx
                    check_y = grid_y + dy

                    # Check bounds
                    if not (0 <= check_x < self.width and 0 <= check_y < self.height):
                        return False

                    # Check if this pixel is white (>= 250)
                    if self.map_data[check_y, check_x] < 250:
                        return False

        return True
