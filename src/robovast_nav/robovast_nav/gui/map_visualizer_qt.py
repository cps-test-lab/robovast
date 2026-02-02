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


from typing import List, Optional, Tuple

from matplotlib.backends.backend_qt5agg import \
    FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .map_visualizer import MapVisualizer


class MapVisualizerWidget(QWidget):
    """
    A Qt Widget wrapper for MapVisualizer that embeds matplotlib in PySide6.

    This widget provides an interactive map visualization with pan/zoom capabilities
    using matplotlib's navigation toolbar. It wraps the existing MapVisualizer class
    to maintain backward compatibility with Jupyter notebook usage.
    """

    def __init__(self, parent: Optional[QWidget] = None,
                 figsize: Tuple[int, int] = None,
                 show_toolbar: bool = False):
        """
        Initialize the MapVisualizerWidget.

        Args:
            parent: Parent QWidget (optional)
            figsize: Figure size as (width, height) in inches. If None, will be calculated from widget size.
            show_toolbar: Whether to show the matplotlib navigation toolbar
        """
        super().__init__(parent)
        self.yaml_path = None
        # Create the underlying MapVisualizer instance
        self.map_visualizer = MapVisualizer()
        self.show_toolbar = show_toolbar

        # Create matplotlib Figure and Canvas
        # If no figsize provided, use a default that will be updated in resizeEvent
        if figsize is None:
            figsize = self._calculate_figsize()
        self.figure = Figure(figsize=figsize)
        self.canvas = FigureCanvas(self.figure)

        # Create navigation toolbar for pan/zoom
        self.toolbar = NavigationToolbar(self.canvas, self) if show_toolbar else None

        # Set up the layout
        self.init_ui()

        # Apply dark theme styling to match existing widgets
        self.apply_dark_theme()

    def _calculate_figsize(self) -> Tuple[float, float]:
        """
        Calculate figure size in inches based on widget size.

        Returns:
            Tuple of (width, height) in inches
        """
        # Get widget size in pixels
        width_px = self.width() if self.width() > 0 else 800  # Default to 800px
        height_px = self.height() if self.height() > 0 else 600  # Default to 600px

        # Reserve space for toolbar if present (approximately 40 pixels)
        if self.show_toolbar:
            height_px -= 40

        # Convert pixels to inches (assuming 100 DPI)
        dpi = 100
        width_inches = width_px / dpi
        height_inches = height_px / dpi

        return (width_inches, height_inches)

    def resizeEvent(self, event):
        """
        Handle widget resize events by updating the figure size.

        Args:
            event: QResizeEvent
        """
        super().resizeEvent(event)

        # Calculate new figure size based on widget dimensions
        new_figsize = self._calculate_figsize()

        # Update the figure size
        self.figure.set_size_inches(new_figsize[0], new_figsize[1])

        # Redraw the canvas
        self.canvas.draw()

    def init_ui(self):
        """Initialize the user interface layout."""
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        # Add toolbar if enabled
        if self.toolbar:
            layout.addWidget(self.toolbar)

        # Add the matplotlib canvas
        layout.addWidget(self.canvas)

        self.setLayout(layout)

    def apply_dark_theme(self):
        """Apply dark theme styling to match the existing GUI widgets."""
        # Set dark background for the figure to match the dark theme
        self.figure.patch.set_facecolor('#2b2b2b')

        # Style the toolbar if present
        if self.toolbar:
            self.toolbar.setStyleSheet("""
                QToolBar {
                    background-color: #2b2b2b;
                    border: none;
                    spacing: 3px;
                }
                QToolButton {
                    background-color: #3c3c3c;
                    border: 1px solid #555555;
                    border-radius: 3px;
                    padding: 5px;
                    color: #ffffff;
                }
                QToolButton:hover {
                    background-color: #4a4a4a;
                    border: 1px solid #6a6a6a;
                }
                QToolButton:pressed {
                    background-color: #2a2a2a;
                }
            """)

    def load_map(self, yaml_path: str) -> bool:
        """
        Load a map from a YAML file.

        Args:
            yaml_path: Path to the map.yaml file

        Returns:
            True if successful, False otherwise
        """
        if yaml_path == self.yaml_path:
            return True  # Map already loaded
        success = self.map_visualizer.load_map(yaml_path)
        if success:
            self.yaml_path = yaml_path
            # Create the figure with the loaded map
            self.refresh()
        return success

    def refresh(self):
        """
        Refresh the map visualization.

        This clears the current figure and recreates it with the loaded map data.
        Call this after loading a map or when you want to redraw from scratch.
        """
        if self.map_visualizer.map is None:
            return

        # Clear the figure
        self.figure.clear()

        # Create a new axes on the figure
        ax = self.figure.add_subplot(111)

        # Use the MapVisualizer to create the map on our axes
        self.map_visualizer.create_figure(ax=ax)

        # Update the canvas
        self.canvas.draw()

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
            show_endpoints: Whether to show start/end markers
        """
        path = self.map_visualizer.draw_path(path_points, color, linewidth, alpha, label, show_endpoints)
        self.canvas.draw()
        return path

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
        self.map_visualizer.draw_waypoints(waypoints, color, marker, markersize, alpha, label)
        self.canvas.draw()

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
        self.map_visualizer.add_robot_pose(x, y, theta, color, size)
        self.canvas.draw()

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
            yaw: Obstacle orientation in radians
            shape: Shape of the obstacle ('circle' or 'box')
            color: Color of the obstacle
            alpha: Transparency of the obstacle
            label: Label for the obstacle (for legend)
        """
        obstacle = self.map_visualizer.draw_obstacle(x, y, draw_args, yaw, shape, color, alpha, label)
        self.canvas.draw()
        return obstacle

    def draw_circle(self, x: float, y: float, radius: float = 0.1,
                    color: str = 'blue', alpha: float = 0.5,
                    label: Optional[str] = None) -> None:
        """
        Draw a simple circle on the map.

        Args:
            x: Circle x coordinate in world frame
            y: Circle y coordinate in world frame
            radius: Radius of the circle in meters
            color: Color of the circle
            alpha: Transparency of the circle
            label: Label for the circle (for legend)
        """
        draw_args = {'diameter': radius * 2}
        obstacle = self.map_visualizer.draw_obstacle(x, y, draw_args, yaw=0.0, 
                                                     shape='circle', color=color, 
                                                     alpha=alpha, label=label)
        self.canvas.draw()
        return obstacle

    def show_legend(self) -> None:
        """Show the legend for all drawn elements."""
        self.map_visualizer.show_legend()
        self.canvas.draw()

    def clear(self):
        """Clear all drawings and reload the base map."""
        self.refresh()

    def save_figure(self, filename: str, dpi: int = 300) -> None:
        """
        Save the current figure to a file.

        Args:
            filename: Output filename
            dpi: Resolution in dots per inch
        """
        self.figure.savefig(filename, dpi=dpi, bbox_inches='tight')
        print(f"Figure saved as: {filename}")

    def get_map_bounds(self) -> Tuple[float, float, float, float]:
        """
        Get the bounds of the map in world coordinates.

        Returns:
            Tuple of (min_x, max_x, min_y, max_y)
        """
        return self.map_visualizer.get_map_bounds()

    def pixel_to_world(self, pixel_x: int, pixel_y: int) -> Tuple[float, float]:
        """
        Convert pixel coordinates to world coordinates.

        Args:
            pixel_x: X coordinate in pixels
            pixel_y: Y coordinate in pixels

        Returns:
            Tuple of (world_x, world_y) coordinates
        """
        return self.map_visualizer.pixel_to_world(pixel_x, pixel_y)

    def world_to_pixel(self, world_x: float, world_y: float) -> Tuple[int, int]:
        """
        Convert world coordinates to pixel coordinates.

        Args:
            world_x: X coordinate in world frame
            world_y: Y coordinate in world frame

        Returns:
            Tuple of (pixel_x, pixel_y) coordinates
        """
        return self.map_visualizer.world_to_pixel(world_x, world_y)
