#!/usr/bin/env python3
"""
Object shape definitions for visualizing static objects in GUI components.

This module provides SVG-based shape definitions that can be rendered based on
object types and their parameters.
"""

import math
from typing import Any, Dict

from PySide2.QtCore import QPointF
from PySide2.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF


class ObjectShapeRenderer:
    """Renders object shapes based on type and parameters."""

    def __init__(self):
        self.shape_definitions = {
            "box": self._render_box,
            "cylinder": self._render_cylinder,
        }

    def render_object_shape(
        self,
        painter: QPainter,
        object_type: str,
        center_x: float,
        center_y: float,
        yaw: float,
        xacro_args: str,
        scale_factor: float,
        color: QColor,
    ):
        """
        Render an object shape at the specified location.

        Args:
            painter: QPainter instance
            object_type: Type of object ('box', 'cylinder', etc.)
            center_x, center_y: Center position in pixel coordinates
            yaw: Rotation angle in radians
            xacro_args: Xacro arguments string (e.g., "width:=0.5, length:=0.8")
            scale_factor: Scale factor for converting from meters to pixels
            color: Color to render the shape
        """
        if object_type in self.shape_definitions:
            # Parse xacro arguments
            params = self._parse_xacro_args(xacro_args)

            # Call the appropriate shape renderer
            self.shape_definitions[object_type](
                painter, center_x, center_y, yaw, params, scale_factor, color
            )
        else:
            # Fallback to simple circle for unknown types
            self._render_fallback_circle(
                painter, center_x, center_y, scale_factor, color
            )

    def _parse_xacro_args(self, xacro_args: str) -> Dict[str, float]:
        """Parse xacro arguments string into a dictionary of parameters."""
        params = {}
        if not xacro_args:
            return params

        # Split by comma and parse key:=value pairs
        for arg in xacro_args.split(","):
            arg = arg.strip()
            if ":=" in arg:
                key, value = arg.split(":=", 1)
                key = key.strip()
                value = value.strip()
                try:
                    # Try to convert to float
                    params[key] = float(value)
                except ValueError:
                    # Keep as string if not a number
                    params[key] = value

        return params

    def _render_box(
        self,
        painter: QPainter,
        center_x: float,
        center_y: float,
        yaw: float,
        params: Dict[str, Any],
        scale_factor: float,
        color: QColor,
    ):
        """Render a box shape."""
        # Get dimensions with defaults
        width = params.get("width", 0.5)  # meters
        length = params.get("length", width)  # meters, default to square
        # height not used for 2D visualization

        # Convert to pixels
        width_px = width * scale_factor
        length_px = length * scale_factor

        # Create rectangle points (before rotation)
        half_width = width_px / 2
        half_length = length_px / 2

        corners = [
            QPointF(-half_width, -half_length),
            QPointF(half_width, -half_length),
            QPointF(half_width, half_length),
            QPointF(-half_width, half_length),
        ]

        # Rotate and translate points
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)

        rotated_corners = []
        for corner in corners:
            # Rotate around origin
            rotated_x = corner.x() * cos_yaw - corner.y() * sin_yaw
            rotated_y = corner.x() * sin_yaw + corner.y() * cos_yaw

            # Translate to center position
            rotated_corners.append(QPointF(center_x + rotated_x, center_y + rotated_y))

        # Draw the box
        polygon = QPolygonF(rotated_corners)
        painter.setPen(QPen(color, 2))
        painter.setBrush(QBrush(color))
        painter.drawPolygon(polygon)

    def _render_cylinder(
        self,
        painter: QPainter,
        center_x: float,
        center_y: float,
        yaw: float,
        params: Dict[str, Any],
        scale_factor: float,
        color: QColor,
    ):
        """Render a cylinder shape (circle in 2D)."""
        # Get parameters
        diameter = params.get("diameter", 0.5)  # meters
        radius = params.get("radius", diameter / 2)  # meters

        # Convert to pixels
        radius_px = radius * scale_factor

        # Draw circle (yaw doesn't affect circular shapes)
        painter.setPen(QPen(color, 2))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(QPointF(center_x, center_y), radius_px, radius_px)

    def _render_fallback_circle(
        self,
        painter: QPainter,
        center_x: float,
        center_y: float,
        scale_factor: float,
        color: QColor,
    ):
        """Render a fallback circle for unknown object types."""
        radius_px = 0.25 * scale_factor  # Default 0.25m radius

        painter.setPen(QPen(color, 2))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(QPointF(center_x, center_y), radius_px, radius_px)


def get_object_type_from_model_path(model_path: str) -> str:
    """
    Extract object type from model path.

    Args:
        model_path: Path to the model file

    Returns:
        Object type string (e.g., 'box', 'cylinder')
    """
    if not model_path:
        return "unknown"

    # Extract filename from path
    filename = model_path.split("/")[-1]

    # Remove file extensions
    base_name = (
        filename.replace(".sdf.xacro", "").replace(".sdf", "").replace(".urdf", "")
    )

    # Map common model names to types
    type_mapping = {"box": "box", "cylinder": "cylinder"}

    return type_mapping.get(base_name.lower(), base_name.lower())


def get_default_xacro_args_for_type(object_type: str) -> str:
    """
    Get default xacro arguments for an object type.

    Args:
        object_type: Type of object

    Returns:
        Default xacro arguments string
    """
    defaults = {
        "box": "width:=0.5, length:=0.5, height:=1.0",
        "cylinder": "diameter:=0.5, height:=1.0",
    }

    return defaults.get(object_type, "width:=0.5, length:=0.5")


def get_obstacle_dimensions(
    xacro_arguments: str, shape_renderer=None
) -> Dict[str, float]:
    """
    Extract obstacle dimensions from xacro_arguments.

    Args:
        xacro_arguments: Xacro arguments string
        shape_renderer: Optional ObjectShapeRenderer instance (if None, creates new one)

    Returns:
        Dictionary with dimension parameters
    """
    if shape_renderer is None:
        shape_renderer = ObjectShapeRenderer()

    # Use the shape renderer to parse xacro arguments
    # pylint: disable=protected-access
    params = shape_renderer._parse_xacro_args(xacro_arguments)

    # Normalize parameter names and provide defaults
    dimensions = {}

    # Handle radius/diameter for cylinders
    if "radius" in params:
        dimensions["radius"] = params["radius"]
    elif "diameter" in params:
        dimensions["radius"] = params["diameter"] / 2.0
    else:
        dimensions["radius"] = 0.25  # Default radius

    # Handle width/length for boxes
    if "width" in params:
        dimensions["width"] = params["width"]
    elif "box_width" in params:
        dimensions["width"] = params["box_width"]
    else:
        dimensions["width"] = 0.5  # Default width

    if "length" in params:
        dimensions["length"] = params["length"]
    elif "box_length" in params:
        dimensions["length"] = params["box_length"]
    else:
        dimensions["length"] = dimensions["width"]  # Default to square

    return dimensions
