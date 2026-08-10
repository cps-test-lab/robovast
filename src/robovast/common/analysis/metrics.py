#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
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

"""Derivations over a run-data frame — pure, no I/O.

Everything here takes a DataFrame and returns a DataFrame, so it works the same on a frame
from :mod:`~robovast.common.analysis.db` (a ``data.db`` table) and on one from
:mod:`~robovast.common.analysis.files` (per-run files).

That is why these are not in ``ros2``. :func:`get_behavior_info` lived there because the
behaviour tree used to arrive as ``behaviors.csv``, converted from the
``/scenario_execution/snapshots`` topic by a rosbag handler — so it really was ROS-only.
``scenario_execution`` now writes ``behaviors.jsonl`` itself, from a logger that imports only
stdlib and ``py_trees``, and ``mode: base`` campaigns produce the same table. Filing these by
where their input happens to come from is what put the function in the wrong module; the rule
for ``ros2`` is now "reads a rosbag artifact", which does not move under us.
"""

from typing import Tuple

import numpy as np
import pandas as pd

#: The two ways a frame identifies which run a row came from. ``data.db`` uses
#: ``(config_name, run_id)`` — the column pair every metric table is keyed by, and the one
#: ``runs`` joins on. ``read_output_files`` attaches ``(config, run)`` instead. Both are
#: current, so these functions accept either rather than forcing a rename on the caller.
_RUN_KEY_PAIRS = (("config_name", "run_id"), ("config", "run"))


def run_key_columns(df: pd.DataFrame) -> Tuple[str, ...]:
    """The ``(config, run)`` identifying columns present in *df*, outermost first.

    Returns an empty tuple for a frame carrying neither — a single run's data read on its own
    is legitimately unkeyed, and grouping then has nothing to group by.
    """
    for config_col, run_col in _RUN_KEY_PAIRS:
        if config_col in df.columns and run_col in df.columns:
            return (config_col, run_col)
    return ()


def get_behavior_info(behavior_name: str, behavior_dataframe: pd.DataFrame) -> pd.DataFrame:
    """Start, end and duration of every instance of one behaviour.

    An instance runs from its first ``RUNNING`` to its first terminal ``SUCCESS``/``FAILURE``;
    an instance that never reached both is skipped, because a duration for it would be invented.

    Args:
        behavior_name: The ``behavior_name`` to filter on.
        behavior_dataframe: A behaviour-tree frame — ``timestamp``, ``behavior_name``,
            ``behavior_id``, ``status_name``, plus whichever run-key pair the source uses
            (see :data:`_RUN_KEY_PAIRS`). ``read_table(DATA_DIR, "behaviors")`` and
            ``read_table(DATA_DIR, "nav2_behaviors")`` both qualify — the ingest gives the
            two tables one schema.

    Returns:
        One row per instance: ``behavior_name``, ``id``, ``start_time``, ``end_time``,
        ``duration``, plus the run-key columns the input carried.
    """
    key_cols = run_key_columns(behavior_dataframe)
    out_cols = ['behavior_name', 'id', 'start_time', 'end_time', 'duration', *key_cols]

    behavior_df = behavior_dataframe[behavior_dataframe['behavior_name'] == behavior_name].copy()
    if behavior_df.empty:
        # Same columns as a populated result. They used to differ (no start_time/end_time
        # here), so a notebook that selected them worked until the day it found no matches.
        return pd.DataFrame(columns=out_cols)

    results = []
    for group_keys, group in behavior_df.groupby(['behavior_id', *key_cols], observed=False):
        if not isinstance(group_keys, tuple):
            group_keys = (group_keys,)
        behavior_id, *key_values = group_keys

        start_rows = group[group['status_name'] == 'RUNNING'].sort_values('timestamp')
        if start_rows.empty:
            continue

        end_rows = group[group['status_name'].isin(['SUCCESS', 'FAILURE'])].sort_values('timestamp')
        if end_rows.empty:
            continue

        start_time = start_rows.iloc[0]['timestamp']
        end_time = end_rows.iloc[0]['timestamp']
        results.append({
            'behavior_name': behavior_name,
            'id': behavior_id,
            'start_time': start_time,
            'end_time': end_time,
            'duration': end_time - start_time,
            **dict(zip(key_cols, key_values)),
        })

    if not results:
        return pd.DataFrame(columns=out_cols)
    return pd.DataFrame(results)


def calculate_speeds_from_poses(df_groundtruth: pd.DataFrame) -> pd.DataFrame:
    """Linear and angular speed per pose sample, differentiated within each run.

    Args:
        df_groundtruth: ``position.x``, ``position.y``, ``orientation.yaw``, ``timestamp``,
            plus whichever run-key pair the source uses. Differentiating across a run boundary
            would produce a spike, so the frame is grouped by that key first.

    Returns:
        The input columns plus ``linear_speed``, ``angular_speed`` and ``dt``, minus the last
        sample of each run (no successor to difference against) and any sample whose ``dt`` is
        too small to divide by.
    """
    key_cols = run_key_columns(df_groundtruth)
    value_cols = ['position.x', 'position.y', 'orientation.yaw', 'timestamp']
    out_cols = [*key_cols, *value_cols, 'linear_speed', 'angular_speed', 'dt']
    min_dt = 1e-6

    # Without a run key the whole frame is one run. Differencing it as such is right for a
    # single run's poses and wrong for a concatenation of several -- which is exactly what the
    # key columns exist to tell us, so there is nothing to guess here.
    groups = (df_groundtruth.groupby(list(key_cols), observed=False)
              if key_cols else [((), df_groundtruth)])

    result_dfs = []
    for _, group in groups:
        if len(group) < 2:
            continue

        df_speeds = group[out_cols[:len(key_cols) + len(value_cols)]].copy()

        dt = np.diff(df_speeds['timestamp'].values)
        dx = np.diff(df_speeds['position.x'].values)
        dy = np.diff(df_speeds['position.y'].values)
        dyaw = np.diff(df_speeds['orientation.yaw'].values)
        # Normalize angle differences to [-pi, pi] -- a wrap at +/-pi is a small turn, not a
        # 2*pi one, and would otherwise read as a huge angular speed.
        dyaw = np.arctan2(np.sin(dyaw), np.cos(dyaw))

        valid_mask = dt > min_dt
        linear_speed = np.zeros_like(dt)
        angular_speed = np.zeros_like(dt)
        linear_speed[valid_mask] = np.sqrt(dx[valid_mask]**2 + dy[valid_mask]**2) / dt[valid_mask]
        angular_speed[valid_mask] = dyaw[valid_mask] / dt[valid_mask]

        # np.diff shortens by one, so pad the tail and drop it below.
        df_speeds['linear_speed'] = np.append(linear_speed, np.nan)
        df_speeds['angular_speed'] = np.append(angular_speed, np.nan)
        df_speeds['dt'] = np.append(dt, np.nan)

        df_speeds = df_speeds[:-1].copy()
        df_speeds = df_speeds[df_speeds['dt'] > min_dt].copy()
        if not df_speeds.empty:
            result_dfs.append(df_speeds)

    if not result_dfs:
        return pd.DataFrame(columns=out_cols)
    return pd.concat(result_dfs, ignore_index=True)
