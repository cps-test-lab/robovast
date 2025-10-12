#!/usr/bin/env python3
"""
Map visualization widget for scenario editing applications.

This module provides the MapWidget class which handles:
- Map loading and display
- Waypoint visualization and interaction
- Mouse-based waypoint editing with drag-and-drop
- Zoom and pan functionality
- Real-time path visualization
- Custom path display using set_path() method
"""

import math
import os
from typing import List, Optional, Tuple

import numpy as np
import yaml
from PIL import Image
from PySide2.QtCore import QPoint, Qt, Signal
from PySide2.QtGui import (QBrush, QColor, QFont, QImage, QMouseEvent,
                           QPainter, QPaintEvent, QPen, QPixmap, QPolygon,
                           QWheelEvent)
from PySide2.QtWidgets import QToolTip, QWidget
from variant_editor.collision_checker import CollisionChecker
from variant_editor.data_models import Pose, Position
from variant_editor.object_shapes import (ObjectShapeRenderer,
                                          get_object_type_from_model_path)


class MapWidget(QWidget):
    """Widget for displaying and editing the map with waypoints."""

    waypoint_clicked = Signal(int)  # Signal emitted when a waypoint is clicked
    map_clicked = Signal(float, float)  # Signal emitted when map is clicked
    waypoint_moved = Signal(
        int, float, float
    )  # Signal emitted when a waypoint is dragged (index, x, y)
    waypoint_deleted = Signal(
        int
    )  # Signal emitted when a waypoint is right-clicked for deletion

    def __init__(self, waypoint_movement_enabled=True, parent=None):
        super().__init__(parent)
        self.setMinimumSize(600, 400)
        self.setMouseTracking(True)

        # Map data
        self.map_image = None
        self.map_data = None  # Raw map data for collision checking
        self.map_resolution = 0.05
        self.map_origin = [0.0, 0.0, 0.0]
        self.map_width = 0
        self.map_height = 0
        self.collision_checker = None

        # Display parameters
        self.scale_factor = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.min_scale = 0.1
        self.max_scale = 5.0

        # Waypoints and visualization
        self.start_pose = None
        self.goal_poses = []
        self.static_objects = []
        self.selected_waypoint = -1
        self.waypoint_radius = 8
        self.robot_diameter_pixels = 0
        self.robot_diameter = 0.3

        # Mouse hover feedback
        self.hover_position = None
        self.hover_valid = True

        # Waypoint dragging
        self.dragging_waypoint = (
            -2
        )  # -2 = not dragging, -1 = start pose, 0+ = goal index
        self.drag_start_pos = None
        self.drag_threshold = 5  # Minimum pixels to move before starting drag
        self.original_waypoint_pos = (
            None  # Store original position when dragging starts
        )

        # Waypoint validity status
        self.waypoint_validity = []

        # Computed paths between waypoints
        # List of paths, each path is a list of (x, y) tuples
        self.computed_paths = []

        # Path to display (list of Position objects)
        self.display_path = []

        # Costmap overlay
        self.costmap_overlay = None
        self.show_costmap = False

        # Object shape renderer for custom shapes
        self.object_renderer = ObjectShapeRenderer()

        # Colors
        self.start_color = QColor(0, 255, 0)  # Green
        self.goal_color = QColor(0, 0, 255)  # Blue
        self.selected_color = QColor(255, 255, 0)  # Yellow
        self.robot_color = QColor(0, 0, 255, 100)  # Semi-transparent blue
        # Light red for invalid positions
        self.invalid_color = QColor(255, 100, 100)
        self.hover_valid_color = QColor(0, 255, 0, 100)  # Semi-transparent green
        self.hover_invalid_color = QColor(255, 0, 0, 100)  # Semi-transparent red
        self.object_color = QColor(255, 165, 0)  # Orange for static objects

        self.setStyleSheet("border: 1px solid gray;")

        # Waypoint movement flag
        self._waypoint_movement_enabled = waypoint_movement_enabled

    def load_map(self, map_file_path: str):
        """Load a map from the given YAML file path."""
        try:
            map_dir = os.path.dirname(map_file_path)

            with open(map_file_path, "r") as file:
                map_data = yaml.safe_load(file)

            # Extract map parameters
            self.map_resolution = map_data.get("resolution", 0.05)
            self.map_origin = map_data.get("origin", [0.0, 0.0, 0.0])

            # Load the image
            image_path = os.path.join(map_dir, map_data["image"])
            if os.path.exists(image_path):
                pil_image = Image.open(image_path)

                # Store original map data for collision checking
                if pil_image.mode == "L":  # Grayscale
                    self.map_data = np.array(pil_image)
                elif pil_image.mode == "RGB":
                    # Convert to grayscale for collision checking
                    gray_image = pil_image.convert("L")
                    self.map_data = np.array(gray_image)
                else:
                    pil_image = pil_image.convert("L")
                    self.map_data = np.array(pil_image)

                # Create collision checker
                self.collision_checker = CollisionChecker(
                    self.map_data, self.map_resolution, self.map_origin
                )

                # Convert to RGB for display
                if pil_image.mode != "RGB":
                    pil_image = pil_image.convert("RGB")

                # Convert PIL image to QPixmap
                image_array = np.array(pil_image)
                height, width, _channel = image_array.shape
                bytes_per_line = 3 * width

                q_image = QImage(
                    image_array.data,
                    width,
                    height,
                    bytes_per_line,
                    QImage.Format_RGB888,
                )
                self.map_image = QPixmap.fromImage(q_image)

                self.map_width = width
                self.map_height = height

                # Clear computed paths since we have a new map
                self.computed_paths = []

                # Reset view
                self.reset_view()
                self.update()
                return True
            else:
                print(f"Map image not found: {image_path}")
                return False

        except Exception as e:
            print(f"Error loading map: {e}")
            return False

    def reset_view(self):
        """Reset the view to fit the entire map."""
        if self.map_image:
            widget_width = self.width()
            widget_height = self.height()

            scale_x = widget_width / self.map_width
            scale_y = widget_height / self.map_height
            self.scale_factor = min(scale_x, scale_y) * 0.9

            self.offset_x = (widget_width - self.map_width * self.scale_factor) / 2
            self.offset_y = (widget_height - self.map_height * self.scale_factor) / 2

    def set_waypoints(self, start_pose: Optional[Pose], goal_poses: List[Pose]):
        """Set the waypoints to display."""
        self.start_pose = start_pose
        self.goal_poses = goal_poses
        self.selected_waypoint = -1
        self.update()

    def set_static_objects(self, static_objects: List):
        """Set the static objects to display."""
        self.static_objects = static_objects if static_objects else []
        self.update()

    def set_path(self, path: List[Position]):
        """Set the path to display on the map."""
        self.display_path = path if path else []
        self.update()

    def set_costmap(self, costmap: Optional[np.ndarray], show_overlay: bool = True):
        """
        Set the costmap to display as an overlay on the map.

        Args:
            costmap: 2D numpy array with costmap values (0=free, 255=occupied)
            show_overlay: Whether to show the costmap overlay
        """
        if costmap is not None:
            # Convert costmap to colored overlay
            # Free space (0) -> transparent
            # Occupied space (255) -> red with transparency
            height, width = costmap.shape
            overlay_array = np.zeros((height, width, 4), dtype=np.uint8)

            # gray overlay for occupied areas
            occupied_mask = costmap > 0
            overlay_array[occupied_mask] = [
                128,
                128,
                128,
                100,
            ]  # gray with 100/255 alpha

            # Convert to QPixmap
            bytes_per_line = 4 * width
            q_image = QImage(
                overlay_array.data,
                width,
                height,
                bytes_per_line,
                QImage.Format_RGBA8888,
            )
            self.costmap_overlay = QPixmap.fromImage(q_image)
        else:
            self.costmap_overlay = None

        self.show_costmap = show_overlay
        self.update()

    def toggle_costmap_overlay(self):
        """Toggle the costmap overlay visibility."""
        self.show_costmap = not self.show_costmap
        self.update()

    def set_robot_diameter(self, diameter: float):
        """Set the robot diameter for visualization."""
        self.robot_diameter = diameter
        self.robot_diameter_pixels = diameter / self.map_resolution
        self.update()

    def update_waypoint_validity(self):
        """Update the validity status of all waypoints."""
        if not self.collision_checker:
            self.waypoint_validity = []
            return

        waypoints = []
        if self.start_pose:
            waypoints.append((self.start_pose.position.x, self.start_pose.position.y))

        for goal in self.goal_poses:
            waypoints.append((goal.position.x, goal.position.y))

        if not waypoints:
            self.waypoint_validity = []
            return

        robot_radius = self.robot_diameter / 2

        # Only check individual position validity for now (much faster)
        # Path connectivity checking is expensive, so we'll skip it for
        # real-time updates
        position_validity = []
        for x, y in waypoints:
            valid = self.collision_checker.is_valid_position(x, y, robot_radius)
            position_validity.append(valid)

        # For now, assume all positions are reachable if they're valid positions
        # This greatly improves UI responsiveness
        self.waypoint_validity = position_validity

        # Compute paths for visualization (but don't block UI)
        self.compute_paths_between_waypoints()

    def update_waypoint_validity_full(self):
        """Update the validity status of all waypoints with full path checking (slower)."""
        if not self.collision_checker:
            self.waypoint_validity = []
            return

        waypoints = []
        if self.start_pose:
            waypoints.append((self.start_pose.position.x, self.start_pose.position.y))

        for goal in self.goal_poses:
            waypoints.append((goal.position.x, goal.position.y))

        if not waypoints:
            self.waypoint_validity = []
            return

        robot_radius = self.robot_diameter / 2

        # Check individual position validity
        position_validity = []
        for x, y in waypoints:
            valid = self.collision_checker.is_valid_position(x, y, robot_radius)
            position_validity.append(valid)

        # Check path connectivity (expensive operation)
        path_validity = self.collision_checker.check_path_sequence(
            waypoints, robot_radius
        )

        # Combine results (waypoint is valid if both position and path are
        # valid)
        self.waypoint_validity = [
            pos and path for pos, path in zip(position_validity, path_validity)
        ]

        # Compute paths for visualization
        self.compute_paths_between_waypoints()

    def is_pixel_white(self, x: float, y: float) -> bool:
        """Check if the map pixel at world coordinates (x, y) is white (free space)."""
        if self.map_image is None or self.map_data is None:
            return True  # Allow if map not loaded
        # Convert world coordinates to map pixel coordinates
        map_x = int((x - self.map_origin[0]) / self.map_resolution)
        map_y = int(
            self.map_height - (y - self.map_origin[1]) / self.map_resolution
        )  # Flip Y
        if 0 <= map_x < self.map_width and 0 <= map_y < self.map_height:
            # For grayscale maps, white is 255
            pixel_value = self.map_data[map_y, map_x]
            return pixel_value >= 250  # Allow a small tolerance for white
        return False

    def is_position_valid(self, x: float, y: float) -> bool:
        """Check if a position is valid for the robot and is on a white (free) area."""
        if not self.collision_checker:
            return True
        robot_radius = self.robot_diameter / 2
        collision_free = self.collision_checker.is_valid_position(x, y, robot_radius)
        white_pixel = self.is_pixel_white(x, y)
        return collision_free and white_pixel

    def select_waypoint(self, index: int):
        """Select a waypoint by index (-1 for start, 0+ for goals)."""
        self.selected_waypoint = index
        self.update()

    def world_to_pixel(self, x: float, y: float) -> Tuple[float, float]:
        """Convert world coordinates to pixel coordinates."""
        # Convert world coordinates to map pixel coordinates
        map_x = (x - self.map_origin[0]) / self.map_resolution
        map_y = (
            self.map_height - (y - self.map_origin[1]) / self.map_resolution
        )  # Flip Y

        # Apply current view transformation
        pixel_x = map_x * self.scale_factor + self.offset_x
        pixel_y = map_y * self.scale_factor + self.offset_y

        return pixel_x, pixel_y

    def pixel_to_world(self, pixel_x: float, pixel_y: float) -> Tuple[float, float]:
        """Convert pixel coordinates to world coordinates."""
        # Remove view transformation
        map_x = (pixel_x - self.offset_x) / self.scale_factor
        map_y = (pixel_y - self.offset_y) / self.scale_factor

        # Convert map pixel coordinates to world coordinates
        world_x = map_x * self.map_resolution + self.map_origin[0]
        world_y = (self.map_height - map_y) * self.map_resolution + self.map_origin[
            1
        ]  # Flip Y

        return world_x, world_y

    def draw_yaw_arrow(
        self, painter: QPainter, px: float, py: float, yaw: float, color: QColor
    ):
        """Draw a yaw arrow at the given pixel position."""
        arrow_length = self.waypoint_radius * 2
        arrow_head_size = 4

        # Calculate arrow end point (negate y because Qt coordinate system is
        # flipped)
        end_x = px + arrow_length * math.cos(yaw)
        end_y = py - arrow_length * math.sin(yaw)  # Negate for Qt coordinates

        # Draw arrow shaft
        painter.setPen(QPen(color, 2))
        painter.drawLine(QPoint(int(px), int(py)), QPoint(int(end_x), int(end_y)))

        # Calculate arrow head points
        head_angle1 = yaw + math.pi - math.pi / 6  # 150 degrees from arrow direction
        head_angle2 = yaw + math.pi + math.pi / 6  # 210 degrees from arrow direction

        head_x1 = end_x + arrow_head_size * math.cos(head_angle1)
        head_y1 = end_y - arrow_head_size * math.sin(
            head_angle1
        )  # Negate for Qt coordinates
        head_x2 = end_x + arrow_head_size * math.cos(head_angle2)
        head_y2 = end_y - arrow_head_size * math.sin(
            head_angle2
        )  # Negate for Qt coordinates

        # Draw arrow head
        arrow_head = QPolygon(
            [
                QPoint(int(end_x), int(end_y)),
                QPoint(int(head_x1), int(head_y1)),
                QPoint(int(head_x2), int(head_y2)),
            ]
        )
        painter.setBrush(QBrush(color))
        painter.drawPolygon(arrow_head)

    def paintEvent(self, event: QPaintEvent):
        """Paint the map and waypoints."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Clear background
        painter.fillRect(self.rect(), self.palette().window())

        if not self.map_image:
            return

        # Draw map
        target_rect = self.rect()
        target_rect.setWidth(int(self.map_width * self.scale_factor))
        target_rect.setHeight(int(self.map_height * self.scale_factor))
        target_rect.moveLeft(int(self.offset_x))
        target_rect.moveTop(int(self.offset_y))

        painter.drawPixmap(target_rect, self.map_image)

        # Draw costmap overlay if enabled
        if self.show_costmap and self.costmap_overlay:
            painter.setOpacity(0.6)  # Make overlay semi-transparent
            painter.drawPixmap(target_rect, self.costmap_overlay)
            painter.setOpacity(1.0)  # Reset opacity for other elements

        # Draw hover feedback (but not while dragging)
        if self.hover_position and self.dragging_waypoint == -2:
            hx, hy = self.hover_position
            hover_color = (
                self.hover_valid_color if self.hover_valid else self.hover_invalid_color
            )
            painter.setPen(QPen(hover_color, 2))
            painter.setBrush(QBrush(hover_color))

            if self.robot_diameter_pixels > 0:
                # Draw robot footprint at hover position
                radius = self.robot_diameter_pixels * self.scale_factor / 2
                painter.drawEllipse(QPoint(int(hx), int(hy)), int(radius), int(radius))
            else:
                # Draw small circle at hover position
                painter.drawEllipse(QPoint(int(hx), int(hy)), 4, 4)

        # Draw robot diameter visualization for selected waypoint
        if self.robot_diameter_pixels > 0 and self.selected_waypoint >= -1:
            painter.setPen(QPen(self.robot_color, 2, Qt.DashLine))
            painter.setBrush(QBrush(self.robot_color))

            if self.selected_waypoint == -1 and self.start_pose:
                # Draw around start pose
                px, py = self.world_to_pixel(
                    self.start_pose.position.x, self.start_pose.position.y
                )
                radius = self.robot_diameter_pixels * self.scale_factor / 2
                painter.drawEllipse(QPoint(int(px), int(py)), int(radius), int(radius))
            elif self.selected_waypoint >= 0 and self.selected_waypoint < len(
                self.goal_poses
            ):
                # Draw around selected goal pose
                goal = self.goal_poses[self.selected_waypoint]
                px, py = self.world_to_pixel(goal.position.x, goal.position.y)
                radius = self.robot_diameter_pixels * self.scale_factor / 2
                painter.drawEllipse(QPoint(int(px), int(py)), int(radius), int(radius))

        # Draw start pose
        if self.start_pose:
            px, py = self.world_to_pixel(
                self.start_pose.position.x, self.start_pose.position.y
            )
            # Always use green for start pose (except when being dragged)
            if self.dragging_waypoint == -1:
                color = QColor(255, 165, 0)  # Orange for dragging
            else:
                color = QColor(0, 255, 0)  # Always green
            painter.setPen(QPen(color, 3))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(
                QPoint(int(px), int(py)), self.waypoint_radius, self.waypoint_radius
            )
            # Draw 'S' for start
            painter.setPen(QPen(Qt.white, 2))
            painter.setFont(QFont("Arial", 8, QFont.Bold))
            painter.drawText(int(px - 4), int(py + 3), "S")
            # Draw yaw arrow
            self.draw_yaw_arrow(painter, px, py, self.start_pose.yaw, color)

        # Draw goal poses
        start_offset = 1 if self.start_pose else 0
        for i, goal in enumerate(self.goal_poses):
            px, py = self.world_to_pixel(goal.position.x, goal.position.y)

            # Determine color based on validity and dragging state
            if self.dragging_waypoint == i:
                # Being dragged - use a special dragging color
                color = QColor(255, 165, 0)  # Orange for dragging
            else:
                validity_index = i + start_offset
                if validity_index < len(self.waypoint_validity):
                    is_valid = self.waypoint_validity[validity_index]
                    if self.selected_waypoint == i:
                        color = self.selected_color
                    elif is_valid:
                        color = self.goal_color
                    else:
                        color = self.invalid_color
                else:
                    color = (
                        self.selected_color
                        if self.selected_waypoint == i
                        else self.goal_color
                    )

            painter.setPen(QPen(color, 3))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(
                QPoint(int(px), int(py)), self.waypoint_radius, self.waypoint_radius
            )

            # Draw goal number
            painter.setPen(QPen(Qt.white, 2))
            painter.setFont(QFont("Arial", 8, QFont.Bold))
            text = str(i + 1)
            painter.drawText(int(px - 4), int(py + 3), text)

            # Draw yaw arrow
            self.draw_yaw_arrow(painter, px, py, goal.yaw, color)

        # Draw path between waypoints
        if self.display_path and len(self.display_path) > 1:
            # Draw the robot footprint along the path as a light gray area
            if self.robot_diameter > 0 and self.map_resolution > 0:
                # Compute robot diameter in pixels (screen coordinates)
                robot_diameter_pixels = (
                    self.robot_diameter / self.map_resolution
                ) * self.scale_factor
                radius = max(robot_diameter_pixels / 2, 2)  # Ensure minimum visibility
                path_points = [
                    (self.world_to_pixel(pos.x, pos.y)) for pos in self.display_path
                ]
                n = len(path_points)
                if n > 1:
                    points_left = []
                    points_right = []
                    for i in range(n):
                        if i == 0:
                            dx = path_points[1][0] - path_points[0][0]
                            dy = path_points[1][1] - path_points[0][1]
                        elif i == n - 1:
                            dx = path_points[-1][0] - path_points[-2][0]
                            dy = path_points[-1][1] - path_points[-2][1]
                        else:
                            dx = path_points[i + 1][0] - path_points[i - 1][0]
                            dy = path_points[i + 1][1] - path_points[i - 1][1]
                        length = math.hypot(dx, dy)
                        if length == 0:
                            offset_x, offset_y = 0, 0
                        else:
                            offset_x = -dy / length * radius
                            offset_y = dx / length * radius
                        points_left.append(
                            QPoint(
                                int(path_points[i][0] + offset_x),
                                int(path_points[i][1] + offset_y),
                            )
                        )
                        points_right.append(
                            QPoint(
                                int(path_points[i][0] - offset_x),
                                int(path_points[i][1] - offset_y),
                            )
                        )
                    path_polygon = QPolygon(points_left + points_right[::-1])
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(
                        QColor(200, 200, 200, 160)
                    )  # More visible light gray
                    painter.drawPolygon(path_polygon)
            # Draw the planned path as thin black lines
            painter.setPen(QPen(QColor(0, 0, 0), 1, Qt.SolidLine))  # Thin black line
            for i in range(len(self.display_path) - 1):
                current_pos = self.display_path[i]
                next_pos = self.display_path[i + 1]
                current_px, current_py = self.world_to_pixel(
                    current_pos.x, current_pos.y
                )
                next_px, next_py = self.world_to_pixel(next_pos.x, next_pos.y)
                painter.drawLine(
                    QPoint(int(current_px), int(current_py)),
                    QPoint(int(next_px), int(next_py)),
                )
        elif self.start_pose and self.goal_poses:
            # Fallback: draw simple direct lines between consecutive waypoints
            # if no planned path
            painter.setPen(QPen(QColor(0, 0, 0), 1, Qt.SolidLine))  # Thin black line

            # Line from start to first goal
            start_px, start_py = self.world_to_pixel(
                self.start_pose.position.x, self.start_pose.position.y
            )
            first_goal = self.goal_poses[0]
            first_px, first_py = self.world_to_pixel(
                first_goal.position.x, first_goal.position.y
            )
            painter.drawLine(
                QPoint(int(start_px), int(start_py)),
                QPoint(int(first_px), int(first_py)),
            )

            # Lines between consecutive goals
            for i in range(len(self.goal_poses) - 1):
                current_goal = self.goal_poses[i]
                next_goal = self.goal_poses[i + 1]
                current_px, current_py = self.world_to_pixel(
                    current_goal.position.x, current_goal.position.y
                )
                next_px, next_py = self.world_to_pixel(
                    next_goal.position.x, next_goal.position.y
                )
                painter.drawLine(
                    QPoint(int(current_px), int(current_py)),
                    QPoint(int(next_px), int(next_py)),
                )

        # Draw static objects
        for obj in self.static_objects:
            px, py = self.world_to_pixel(obj.pose.position.x, obj.pose.position.y)

            # Get object type from model path
            object_type = get_object_type_from_model_path(obj.model)

            # Render the object shape based on its type and parameters
            # Calculate correct scale: meters to pixels
            meters_to_pixels = self.scale_factor / self.map_resolution
            self.object_renderer.render_object_shape(
                painter,
                object_type,
                px,
                py,
                obj.pose.yaw,
                obj.xacro_arguments,
                meters_to_pixels,
                self.object_color,
            )

    def mousePressEvent(self, event: QMouseEvent):
        """Handle mouse press events."""
        if not self._waypoint_movement_enabled:
            return
        # Handle right-click to delete waypoint
        if event.button() == Qt.RightButton:
            clicked_waypoint = self.get_waypoint_at_position(event.pos())
            if clicked_waypoint is not None:
                self.waypoint_deleted.emit(clicked_waypoint)
            return
        if event.button() == Qt.LeftButton:
            # Check if clicking on a waypoint
            clicked_waypoint = self.get_waypoint_at_position(event.pos())

            if clicked_waypoint is not None:
                # Start potential drag operation
                self.dragging_waypoint = clicked_waypoint
                self.drag_start_pos = event.pos()

                # Store original position for potential revert
                if clicked_waypoint == -1 and self.start_pose:
                    self.original_waypoint_pos = (
                        self.start_pose.position.x,
                        self.start_pose.position.y,
                    )
                elif 0 <= clicked_waypoint < len(self.goal_poses):
                    goal = self.goal_poses[clicked_waypoint]
                    self.original_waypoint_pos = (goal.position.x, goal.position.y)
                else:
                    self.original_waypoint_pos = None

                self.waypoint_clicked.emit(clicked_waypoint)
            else:
                # Click on map to add waypoint (only if position is valid)
                world_x, world_y = self.pixel_to_world(event.x(), event.y())
                if self.is_position_valid(world_x, world_y):
                    self.map_clicked.emit(world_x, world_y)

    def mouseMoveEvent(self, event: QMouseEvent):
        """Handle mouse move events for hover feedback and waypoint dragging."""
        if not self._waypoint_movement_enabled:
            return
        if self.map_image:
            # Check if we're dragging a waypoint
            if self.dragging_waypoint != -2 and self.drag_start_pos is not None:
                # Check if we've moved far enough to start dragging
                drag_distance = (event.pos() - self.drag_start_pos).manhattanLength()

                if drag_distance >= self.drag_threshold:
                    # We're dragging - update waypoint position if it's valid
                    world_x, world_y = self.pixel_to_world(event.x(), event.y())

                    if self.is_position_valid(world_x, world_y):
                        # Update waypoint position temporarily for visual
                        # feedback
                        if self.dragging_waypoint == -1 and self.start_pose:
                            # Dragging start pose
                            self.start_pose.position.x = world_x
                            self.start_pose.position.y = world_y
                        elif 0 <= self.dragging_waypoint < len(self.goal_poses):
                            # Dragging goal pose
                            self.goal_poses[self.dragging_waypoint].position.x = world_x
                            self.goal_poses[self.dragging_waypoint].position.y = world_y

                        # Update validity and redraw
                        self.update_waypoint_validity()
                        self.update()
                        return

            # Update hover position for normal hover feedback
            self.hover_position = (event.x(), event.y())

            # Check if hover position is valid (but not while dragging)
            if self.dragging_waypoint == -2:
                world_x, world_y = self.pixel_to_world(event.x(), event.y())
                self.hover_valid = self.is_position_valid(world_x, world_y)

            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        """Handle mouse release events to finalize waypoint dragging."""
        if not self._waypoint_movement_enabled:
            return
        if event.button() == Qt.LeftButton and self.dragging_waypoint != -2:
            # Check if we actually moved the waypoint
            if self.drag_start_pos is not None:
                drag_distance = (event.pos() - self.drag_start_pos).manhattanLength()

                if drag_distance >= self.drag_threshold:
                    # We dragged the waypoint - validate the new position
                    world_x, world_y = self.pixel_to_world(event.x(), event.y())

                    if self.is_position_valid(world_x, world_y):
                        # Emit the move signal to update the data model without
                        # full path validation
                        self.waypoint_moved.emit(
                            self.dragging_waypoint, world_x, world_y
                        )
                    else:
                        # Position is invalid, revert to original position
                        self._revert_waypoint_position()
                        QToolTip.showText(
                            event.globalPos(),
                            "Waypoint reverted: Invalid position (collision)",
                            self,
                            self.rect(),
                            3000,
                        )

            # Reset dragging state
            self.dragging_waypoint = -2
            self.drag_start_pos = None
            self.original_waypoint_pos = None

    def leaveEvent(self, event):
        """Handle mouse leave event."""
        self.hover_position = None
        # Reset dragging state if mouse leaves the widget
        if self.dragging_waypoint != -2:
            self.dragging_waypoint = -2
            self.drag_start_pos = None
        self.update()

    def get_waypoint_at_position(self, pos: QPoint) -> Optional[int]:
        """Get the waypoint index at the given position, or None if no waypoint."""
        # Check start pose
        if self.start_pose:
            px, py = self.world_to_pixel(
                self.start_pose.position.x, self.start_pose.position.y
            )
            if (pos.x() - px) ** 2 + (pos.y() - py) ** 2 <= self.waypoint_radius**2:
                return -1

        # Check goal poses
        for i, goal in enumerate(self.goal_poses):
            px, py = self.world_to_pixel(goal.position.x, goal.position.y)
            if (pos.x() - px) ** 2 + (pos.y() - py) ** 2 <= self.waypoint_radius**2:
                return i

        return None

    def wheelEvent(self, event: QWheelEvent):
        """Handle zoom with mouse wheel."""
        if self.map_image:
            # Get mouse position
            mouse_x = event.x()
            mouse_y = event.y()

            # Get world coordinate at mouse position before zoom
            old_world_x, old_world_y = self.pixel_to_world(mouse_x, mouse_y)

            # Update scale factor
            zoom_factor = 1.2 if event.angleDelta().y() > 0 else 1.0 / 1.2
            new_scale = self.scale_factor * zoom_factor
            new_scale = max(self.min_scale, min(self.max_scale, new_scale))

            if new_scale != self.scale_factor:
                self.scale_factor = new_scale

                # Adjust offset to keep mouse position at same world coordinate
                new_pixel_x, new_pixel_y = self.world_to_pixel(old_world_x, old_world_y)
                self.offset_x += mouse_x - new_pixel_x
                self.offset_y += mouse_y - new_pixel_y

                self.update()

    def generate_meaningful_waypoints(
        self, num_waypoints: int = 5, min_distance: float = 2.0
    ) -> List[Tuple[float, float]]:
        """
        Generate meaningful waypoints automatically based on the map.

        Args:
            num_waypoints: Number of waypoints to generate
            min_distance: Minimum distance between waypoints in meters

        Returns:
            List of (x, y) world coordinates
        """
        if not self.collision_checker:
            return []

        robot_radius = self.robot_diameter / 2
        return self.collision_checker.generate_meaningful_waypoints(
            robot_radius, num_waypoints, min_distance
        )

    def compute_paths_between_waypoints(self):
        """Compute actual paths between consecutive waypoints."""
        self.computed_paths = []

        if not self.collision_checker or not self.start_pose or not self.goal_poses:
            return

        # Build list of all waypoints
        waypoints = [(self.start_pose.position.x, self.start_pose.position.y)]
        waypoints.extend(
            [(goal.position.x, goal.position.y) for goal in self.goal_poses]
        )

        if len(waypoints) < 2:
            return

        robot_radius = self.robot_diameter / 2

        # Compute path for each consecutive pair of waypoints
        for i in range(len(waypoints) - 1):
            start_x, start_y = waypoints[i]
            end_x, end_y = waypoints[i + 1]

            # Find path using A* algorithm
            path = self.collision_checker.find_path(
                start_x, start_y, end_x, end_y, robot_radius
            )

            if path is not None:
                self.computed_paths.append(path)
            else:
                # No path found, use direct line as fallback
                self.computed_paths.append([(start_x, start_y), (end_x, end_y)])

    def _validate_full_path_after_move(
        self, waypoint_index: int, new_x: float, new_y: float
    ) -> bool:
        """
        Validate that moving a waypoint to a new position maintains valid paths.

        Args:
            waypoint_index: Index of waypoint being moved (-1 for start, 0+ for goals)
            new_x, new_y: New position coordinates

        Returns:
            True if all paths remain valid after the move
        """
        if not self.collision_checker:
            return True

        # Build current waypoint sequence with the proposed change
        waypoints = []

        if self.start_pose:
            if waypoint_index == -1:
                # Use new position for start pose
                waypoints.append((new_x, new_y))
            else:
                waypoints.append(
                    (self.start_pose.position.x, self.start_pose.position.y)
                )

        for i, goal in enumerate(self.goal_poses):
            if waypoint_index == i:
                # Use new position for this goal
                waypoints.append((new_x, new_y))
            else:
                waypoints.append((goal.position.x, goal.position.y))

        if len(waypoints) < 2:
            return True  # Single waypoint is always valid

        # Check path sequence with the new position
        robot_radius = self.robot_diameter / 2
        path_validity = self.collision_checker.check_path_sequence(
            waypoints, robot_radius
        )

        # Return True only if all paths are valid
        return all(path_validity)

    def _revert_waypoint_position(self):
        """Revert the dragged waypoint to its original position."""
        if self.original_waypoint_pos is None or self.dragging_waypoint == -2:
            return

        orig_x, orig_y = self.original_waypoint_pos

        if self.dragging_waypoint == -1 and self.start_pose:
            # Revert start pose
            self.start_pose.position.x = orig_x
            self.start_pose.position.y = orig_y
        elif 0 <= self.dragging_waypoint < len(self.goal_poses):
            # Revert goal pose
            goal = self.goal_poses[self.dragging_waypoint]
            goal.position.x = orig_x
            goal.position.y = orig_y

        # Update display without full path validation (just position
        # validation)
        self.update_waypoint_validity()
        self.update()

    def clear(self):
        """Clear the map preview and reset state."""
        self.map_image = None
        self.map_data = None
        self.map_width = 0
        self.map_height = 0
        self.collision_checker = None
        self.start_pose = None
        self.goal_poses = []
        self.static_objects = []
        self.selected_waypoint = -1
        self.display_path = []
        self.computed_paths = []
        self.update()
