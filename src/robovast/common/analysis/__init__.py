#!/usr/bin/env python3

"""Helpers for analysis notebooks.

A campaign's tables come from :mod:`robovast_data` (``open_data(DATA_DIR)``), which is where
a notebook starts. Beside it:

* :mod:`files` — per-run files (``test.xml`` and friends) read as they are.
* :mod:`metrics` — derivations over a frame, whichever of the two produced it.
* :mod:`ros2` — readers for rosbag artifacts on disk.
"""

from .files import (for_each_run, get_run_status, get_scenario_parameter, read_output_csv,
                    read_output_files, read_output_yaml_list, read_run_statuses)
from .metrics import calculate_speeds_from_poses, get_behavior_info, run_key_columns
from .ros2 import get_bag_info, print_bag_topics

__all__ = [
    # files
    'read_output_files',
    'read_output_csv',
    'read_output_yaml_list',
    'read_run_statuses',
    'get_run_status',
    'for_each_run',
    'get_scenario_parameter',
    # metrics
    'get_behavior_info',
    'calculate_speeds_from_poses',
    'run_key_columns',
    # ros2
    'get_bag_info',
    'print_bag_topics',
]
