#!/usr/bin/env python3
"""
Analysis Support Package

This package provides tools for analyzing ROS data and visualizing results,
including map visualization capabilities for occupancy grid maps.

Author: Generated for intel_collaboration project
"""

__version__ = "1.0.0"
__author__ = "Intel Collaboration Team"
__email__ = "intel.collaboration@example.com"

from .common import get_variant_data
# Import main classes for easier access
from .map_visualizer import MapVisualizer, load_and_display_map
from .rosbag_parser import get_tf_poses

__all__ = [
    'MapVisualizer',
    'load_and_display_map',
    'get_tf_poses',
    'get_variant_data',
]
