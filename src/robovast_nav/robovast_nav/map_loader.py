#!/usr/bin/env python3
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
Shared utility for loading ROS2 navigation map files.

This module provides a common interface for loading map YAML files
and associated images, extracting map metadata including origin offsets.
Used by path_generator, waypoint_generator, and map_visualizer.
"""

import os
from typing import List, Tuple

import numpy as np
import yaml
from PIL import Image


class Map:
    """
    Container for map data, metadata, and occupancy grid.

    This class encapsulates all map-related information including the raw image data,
    map metadata (resolution, origin), and the processed occupancy grid.
    """

    def __init__(
        self,
        map_array: np.ndarray,
        resolution: float,
        origin: List[float],
        image_path: str,
    ):
        """
        Initialize map container.

        Args:
            map_array: numpy array containing the map image data
            resolution: map resolution in meters/pixel
            origin: map origin [x, y, theta] in world coordinates
            image_path: path to the map image file
        """
        self.map_array = map_array
        self.resolution = resolution
        self.origin = origin
        self.image_path = image_path
        self.height, self.width = map_array.shape

        # Create binary occupancy grid
        # ROS2 nav2 convention: values >= 254 are considered free space
        # Values < 254 are obstacles or unknown (with typical threshold around 250)
        # This handles maps that use 254 or 255 for free space
        self.occupancy_grid = (self.map_array < 254).astype(bool)

    @property
    def origin_x(self) -> float:
        """Get x-coordinate of map origin."""
        return self.origin[0]

    @property
    def origin_y(self) -> float:
        """Get y-coordinate of map origin."""
        return self.origin[1]

    @property
    def origin_theta(self) -> float:
        """Get theta (rotation) of map origin."""
        return self.origin[2] if len(self.origin) > 2 else 0.0

    def world_to_grid(self, x: float, y: float) -> Tuple[int, int]:
        """
        Convert world coordinates to grid coordinates.

        Args:
            x, y: World coordinates in meters

        Returns:
            Tuple of (grid_x, grid_y) in pixel coordinates
        """
        grid_x = int((x - self.origin_x) / self.resolution)
        # Flip Y-axis: image origin is top-left, world origin is bottom-left
        grid_y = int(self.height - (y - self.origin_y) / self.resolution)
        return grid_x, grid_y

    def grid_to_world(self, grid_x: int, grid_y: int) -> Tuple[float, float]:
        """
        Convert grid coordinates to world coordinates.

        Args:
            grid_x, grid_y: Grid coordinates in pixels

        Returns:
            Tuple of (world_x, world_y) in meters
        """
        world_x = grid_x * self.resolution + self.origin_x
        # Flip Y-axis: image origin is top-left, world origin is bottom-left
        world_y = (self.height - grid_y) * self.resolution + self.origin_y
        return world_x, world_y

    def is_valid_grid_position(self, grid_x: int, grid_y: int) -> bool:
        """
        Check if a grid position is valid and free.

        Args:
            grid_x, grid_y: Grid coordinates in pixels

        Returns:
            True if position is within bounds and free, False otherwise
        """
        # Check bounds
        if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
            return False

        # Check if free (not occupied)
        return not self.occupancy_grid[grid_y, grid_x]


def load_map(map_file_path: str) -> Map:
    """
    Load a ROS2 navigation map from a YAML file.

    The YAML file should follow the ROS2 nav2 map format:
    - image: path to map image file (PGM or PNG)
    - resolution: map resolution in meters/pixel
    - origin: [x, y, theta] origin of the map in world coordinates
    - negate: whether to negate the image
    - occupied_thresh: threshold for occupied cells
    - free_thresh: threshold for free cells

    Args:
        map_file_path: Path to the map YAML file

    Returns:
        Map object containing the loaded map and metadata

    Raises:
        FileNotFoundError: If map YAML or image file not found
        ValueError: If map YAML is invalid or image cannot be loaded
    """
    if not os.path.exists(map_file_path):
        raise FileNotFoundError(f"Map YAML file not found: {map_file_path}")

    map_dir = os.path.dirname(map_file_path)

    try:
        # Load map YAML
        with open(map_file_path, "r") as f:
            map_config = yaml.safe_load(f)

        if map_config is None:
            raise ValueError(f"Invalid or empty map YAML file: {map_file_path}")

        # Get map parameters with defaults
        image_file = map_config.get("image", "")
        if not image_file:
            raise ValueError("Map YAML missing required 'image' field")

        # Handle relative paths
        if not os.path.isabs(image_file):
            image_file = os.path.join(map_dir, image_file)

        resolution = map_config.get("resolution", 0.05)
        origin = map_config.get("origin", [0.0, 0.0, 0.0])

        # Ensure origin has at least 3 elements
        if len(origin) < 3:
            origin = list(origin) + [0.0] * (3 - len(origin))

        # Load map image
        if not os.path.exists(image_file):
            raise FileNotFoundError(f"Map image file not found: {image_file}")

        map_image = Image.open(image_file)

        # Convert to grayscale if needed
        if map_image.mode != "L":
            map_image = map_image.convert("L")

        # Convert to numpy array
        map_array = np.array(map_image)

        return Map(
            map_array=map_array,
            resolution=resolution,
            origin=origin,
            image_path=image_file,
        )

    except FileNotFoundError:
        raise
    except Exception as e:
        raise ValueError(f"Error loading map {map_file_path}: {e}") from e
