#!/usr/bin/env python3

"""Helpers for analysis notebooks.

Split by where a frame comes from, plus one module for what you compute from one:

* :mod:`db` — a campaign's ``data.db``, scoped to the notebook's ``DATA_DIR``. Start here.
* :mod:`files` — per-run files (``test.xml`` and friends), including what predates the
  postprocessed database or exists outside it.
* :mod:`metrics` — derivations over a frame, whichever of the two produced it.
* :mod:`ros2` — readers for rosbag artifacts on disk.
"""

from .db import (DATA_DB_SCHEMA_VERSION, CampaignDataError, attach_params, campaign_root,
                 config_file, list_tables, open_campaign_db, open_campaign_store, read_runs,
                 read_sql, read_table, run_scope, table_info)
from .files import (for_each_run, get_run_status, get_scenario_parameter, read_output_csv,
                    read_output_files, read_output_yaml_list, read_run_statuses)
from .metrics import calculate_speeds_from_poses, get_behavior_info, run_key_columns
from .ros2 import get_bag_info, print_bag_topics

__all__ = [
    # db
    'read_table',
    'read_runs',
    'read_sql',
    'attach_params',
    'config_file',
    'list_tables',
    'table_info',
    'run_scope',
    'campaign_root',
    'open_campaign_db',
    'open_campaign_store',
    'CampaignDataError',
    'DATA_DB_SCHEMA_VERSION',
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
