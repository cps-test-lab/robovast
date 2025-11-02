#!/usr/bin/env python3
"""
Map Visualizer Module for ROS Map Files

This module provides functionality to load and visualize ROS map files (map.yaml + .pgm)
in Jupyter notebooks with support for drawing paths on top of the map.

Author: Generated for intel_collaboration project
"""

import os
from typing import List, Tuple

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import yaml
from PIL import Image


class MapVisualizer:
    """
    A class for visualizing ROS maps from YAML configuration files.

    Supports loading occupancy grid maps and drawing paths on top of them.
    """

    def __init__(self):
        """Initialize the MapVisualizer."""
        self.map_data = None
        self.map_info = None
        self.fig = None
        self.ax = None

    def load_map(self, yaml_path: str) -> bool:
        """
        Load a map from a YAML file.

        Args:
            yaml_path: Path to the map.yaml file

        Returns:
            True if successful, False otherwise
        """
        try:
            # Load YAML configuration
            with open(yaml_path, 'r') as file:
                self.map_info = yaml.safe_load(file)

            # Get the directory of the YAML file
            yaml_dir = os.path.dirname(yaml_path)

            # Load the image file (relative to YAML file)
            image_path = os.path.join(yaml_dir, self.map_info['image'])

            if not os.path.exists(image_path):
                print(f"Error: Image file not found: {image_path}")
                return False

            # Load the image
            img = Image.open(image_path)

            # Convert to grayscale numpy array
            self.map_data = np.array(img)

            # Handle different image formats
            if len(self.map_data.shape) == 3:
                # Convert RGB to grayscale
                self.map_data = np.mean(self.map_data, axis=2)

            # print(f"Map loaded successfully:")
            # print(f"  Image: {self.map_info['image']}")
            # print(f"  Resolution: {self.map_info['resolution']} m/pixel")
            # print(f"  Origin: {self.map_info['origin']}")
            # print(f"  Size: {self.map_data.shape[1]} x {self.map_data.shape[0]} pixels")
            # print(f"  Free threshold: {self.map_info.get('free_thresh', 'N/A')}")
            # print(f"  Occupied threshold: {self.map_info.get('occupied_thresh', 'N/A')}")

            return True

        except Exception as e:
            print(f"Error loading map: {str(e)}")
            return False

    def pixel_to_world(self, pixel_x: int, pixel_y: int) -> Tuple[float, float]:
        """
        Convert pixel coordinates to world coordinates.

        Args:
            pixel_x: X coordinate in pixels
            pixel_y: Y coordinate in pixels

        Returns:
            Tuple of (world_x, world_y) coordinates
        """
        if self.map_info is None:
            raise ValueError("No map loaded")

        resolution = self.map_info['resolution']
        origin_x, origin_y, _ = self.map_info['origin']

        # Note: Image coordinates have Y axis flipped compared to world coordinates
        world_x = origin_x + pixel_x * resolution
        world_y = origin_y + (self.map_data.shape[0] - pixel_y) * resolution

        return world_x, world_y

    def world_to_pixel(self, world_x: float, world_y: float) -> Tuple[int, int]:
        """
        Convert world coordinates to pixel coordinates.

        Args:
            world_x: X coordinate in world frame
            world_y: Y coordinate in world frame

        Returns:
            Tuple of (pixel_x, pixel_y) coordinates
        """
        if self.map_info is None:
            raise ValueError("No map loaded")

        resolution = self.map_info['resolution']
        origin_x, origin_y, _ = self.map_info['origin']

        # Note: Image coordinates have Y axis flipped compared to world coordinates
        pixel_x = int((world_x - origin_x) / resolution)
        pixel_y = int(self.map_data.shape[0] - (world_y - origin_y) / resolution)

        return pixel_x, pixel_y

    def create_figure(self, figsize: Tuple[int, int] = (12, 10), ax: plt.Axes = None) -> Tuple[plt.Figure, plt.Axes]:
        """
        Create a matplotlib figure for the map.

        Args:
            figsize: Figure size as (width, height). Ignored if ax is provided.
            ax: Optional matplotlib Axes object to use. If provided, the map will be drawn
                on this axes instead of creating a new figure.

        Returns:
            Tuple of (figure, axes) objects
        """
        if self.map_data is None:
            raise ValueError("No map data loaded")

        if ax is not None:
            # Use the provided axes
            self.ax = ax
            self.fig = ax.get_figure()
        else:
            # Create a new figure and axes
            self.fig, self.ax = plt.subplots(figsize=figsize)

        # Display the map
        # Flip vertically to match ROS coordinate convention
        map_display = np.flipud(self.map_data)

        # Calculate extent in world coordinates
        origin_x, origin_y, _ = self.map_info['origin']
        resolution = self.map_info['resolution']
        height, width = self.map_data.shape

        extent = [
            origin_x,  # left
            origin_x + width * resolution,  # right
            origin_y,  # bottom
            origin_y + height * resolution  # top
        ]

        # Display map with proper coordinate system
        self.ax.imshow(map_display, cmap='gray', extent=extent, origin='lower')

        self.ax.set_xlabel('X (meters)')
        self.ax.set_ylabel('Y (meters)')
        # self.ax.set_title(f'Map: {self.map_info["image"]}')
        self.ax.grid(True, alpha=0.3)

        return self.fig, self.ax

    def draw_path(self, path_points: List[Tuple[float, float]],
                  color: str = 'red', linewidth: float = 2.0,
                  alpha: float = 0.8, label: str = 'Path',
                  show_endpoints: bool = True) -> None:
        """
        Draw a path on the map.

        Args:
            path_points: List of (x, y) coordinates in world frame
            color: Color of the path line
            linewidth: Width of the path line
            alpha: Transparency of the path line
            label: Label for the path (for legend)
            show_endpoints: Whether to show start/end markers (default: True)
        """
        if self.ax is None:
            raise ValueError("No figure created. Call create_figure() first")

        if len(path_points) < 2:
            print("Warning: Path must have at least 2 points")
            return

        # Extract x and y coordinates
        x_coords = [point[0] for point in path_points]
        y_coords = [point[1] for point in path_points]

        # Draw the path
        self.ax.plot(x_coords, y_coords, color=color, linewidth=linewidth,
                     alpha=alpha, label=label)

        # Mark start and end points (without labels to avoid cluttering legend)
        if show_endpoints:
            self.ax.plot(x_coords[0], y_coords[0], 'go', markersize=8)
            self.ax.plot(x_coords[-1], y_coords[-1], 'ro', markersize=8)

    def draw_waypoints(self, waypoints: List[Tuple[float, float]],
                       color: str = 'blue', marker: str = 'o',
                       markersize: float = 6, alpha: float = 0.8,
                       label: str = 'Waypoints') -> None:
        """
        Draw waypoints on the map.

        Args:
            waypoints: List of (x, y) coordinates in world frame
            color: Color of the waypoint markers
            marker: Marker style
            markersize: Size of the markers
            alpha: Transparency of the markers
            label: Label for the waypoints (for legend)
        """
        if self.ax is None:
            raise ValueError("No figure created. Call create_figure() first")

        if not waypoints:
            print("Warning: No waypoints provided")
            return

        # Extract x and y coordinates
        x_coords = [point[0] for point in waypoints]
        y_coords = [point[1] for point in waypoints]

        # Draw waypoints
        self.ax.plot(x_coords, y_coords, color=color, marker=marker,
                     markersize=markersize, alpha=alpha, label=label,
                     linestyle='None')

    def add_robot_pose(self, x: float, y: float, theta: float = 0.0,
                       color: str = 'green', size: float = 0.2) -> None:
        """
        Add a robot pose visualization to the map.

        Args:
            x: Robot x coordinate in world frame
            y: Robot y coordinate in world frame  
            theta: Robot orientation in radians
            color: Color of the robot visualization
            size: Size of the robot visualization
        """
        if self.ax is None:
            raise ValueError("No figure created. Call create_figure() first")

        # Draw robot as a circle with orientation arrow
        circle = plt.Circle((x, y), size/2, color=color, alpha=0.7)
        self.ax.add_patch(circle)

        # Draw orientation arrow
        arrow_length = size * 0.8
        dx = arrow_length * np.cos(theta)
        dy = arrow_length * np.sin(theta)

        self.ax.arrow(x, y, dx, dy, head_width=size*0.2, head_length=size*0.2,
                      fc=color, ec=color, alpha=0.9)

    def draw_obstacle(self, x: float, y: float, draw_args: dict,
                      yaw: float = 0.0, shape: str = 'circle',
                      color: str = 'red', alpha: float = 0.7,
                      label: str = 'Obstacle') -> None:
        """
        Add an obstacle visualization to the map.

        Args:
            x: Obstacle x coordinate in world frame
            y: Obstacle y coordinate in world frame
            draw_args: Dictionary containing obstacle dimensions
                      For 'circle': {'diameter': float}
                      For 'box': {'width': float, 'length': float, 'height': float}
            yaw: Obstacle orientation in radians (only affects box obstacles)
            shape: Shape of the obstacle ('circle' or 'box')
            color: Color of the obstacle
            alpha: Transparency of the obstacle
            label: Label for the obstacle (for legend)
        """
        if self.ax is None:
            raise ValueError("No figure created. Call create_figure() first")

        if shape.lower() == 'circle':
            # Draw circular obstacle
            if 'diameter' not in draw_args:
                raise ValueError("draw_args must include 'diameter' for circle shape")
            diameter = draw_args['diameter']
            circle = plt.Circle((x, y), diameter/2, color=color, alpha=alpha, label=label)
            self.ax.add_patch(circle)

        elif shape.lower() == 'box':
            # Draw rectangular obstacle
            # Get dimensions from draw_args
            if 'width' not in draw_args or 'length' not in draw_args:
                print(f"draw_args must include 'width' and 'length' for box shape. Got: {draw_args}. Using default 1.0m")
                width = 1.0
                length = 1.0
            else:
                width = draw_args['width']
                length = draw_args['length']
            # height is available but not used in 2D visualization

            # Create a rectangle centered at (x, y)
            half_width = width / 2
            half_length = length / 2

            # Define rectangle corners relative to center
            corners = np.array([
                [-half_width, -half_length],
                [half_width, -half_length],
                [half_width, half_length],
                [-half_width, half_length]
            ])

            # Apply rotation if yaw is specified
            if yaw != 0.0:
                cos_yaw = np.cos(yaw)
                sin_yaw = np.sin(yaw)
                rotation_matrix = np.array([
                    [cos_yaw, -sin_yaw],
                    [sin_yaw, cos_yaw]
                ])
                corners = corners @ rotation_matrix.T

            # Translate to obstacle position
            corners[:, 0] += x
            corners[:, 1] += y

            # Create and add the rectangle patch
            rectangle = patches.Polygon(corners, closed=True, color=color,
                                        alpha=alpha, label=label)
            self.ax.add_patch(rectangle)

        else:
            raise ValueError(f"Unsupported obstacle shape: {shape}. Use 'circle' or 'box'")

    def show_legend(self) -> None:
        """Show the legend for all drawn elements."""
        if self.ax is None:
            raise ValueError("No figure created. Call create_figure() first")

        self.ax.legend()

    def save_figure(self, filename: str, dpi: int = 300) -> None:
        """
        Save the current figure to a file.

        Args:
            filename: Output filename
            dpi: Resolution in dots per inch
        """
        if self.fig is None:
            raise ValueError("No figure created. Call create_figure() first")

        self.fig.savefig(filename, dpi=dpi, bbox_inches='tight')
        print(f"Figure saved as: {filename}")

    def get_map_bounds(self) -> Tuple[float, float, float, float]:
        """
        Get the bounds of the map in world coordinates.

        Returns:
            Tuple of (min_x, max_x, min_y, max_y)
        """
        if self.map_info is None:
            raise ValueError("No map loaded")

        origin_x, origin_y, _ = self.map_info['origin']
        resolution = self.map_info['resolution']
        height, width = self.map_data.shape

        min_x = origin_x
        max_x = origin_x + width * resolution
        min_y = origin_y
        max_y = origin_y + height * resolution

        return min_x, max_x, min_y, max_y
