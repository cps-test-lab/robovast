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
Object shape definitions for visualizing static objects in GUI components.

This module provides SVG-based shape definitions that can be rendered based on
object types and their parameters.
"""

from __future__ import annotations

from typing import Dict

def parse_xacro_args(xacro_args: str) -> Dict[str, float]:
    """Parse a xacro arguments string (``width:=0.5, height:=1.0``) into a dict.

    All that survives of a class that otherwise drew obstacles onto a QPainter for the
    desktop map view: the parsing is what the placement and planning code actually needs,
    and it never had anything to do with Qt.
    """
    params = {}
    if not xacro_args:
        return params

    for arg in xacro_args.split(","):
        arg = arg.strip()
        if ":=" in arg:
            key, value = arg.split(":=", 1)
            key = key.strip()
            value = value.strip()
            try:
                params[key] = float(value)
            except ValueError:
                # Keep as string if not a number.
                params[key] = value

    return params


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


def get_obstacle_dimensions(xacro_arguments: str) -> Dict[str, float]:
    """
    Extract obstacle dimensions from xacro_arguments.

    Args:
        xacro_arguments: Xacro arguments string

    Returns:
        Dictionary with dimension parameters
    """
    params = parse_xacro_args(xacro_arguments)

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
