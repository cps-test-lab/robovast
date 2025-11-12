import os

import numpy as np
import pandas as pd
import yaml


def get_bag_info(bag_path: str) -> dict:
    """
    Extracts information from a ROS2 bag file.

    Args:
        bag_path (str): Path to the ROS2 bag file.

    Returns:
        dict: A dictionary containing the extracted bag information.
    """
    rosbag2_metadata_path = os.path.join(bag_path, "metadata.yaml")
    bag_info = {}
    if os.path.exists(rosbag2_metadata_path):
        try:
            with open(rosbag2_metadata_path, 'r') as f:
                bag_info = yaml.safe_load(f)
        except Exception as e:
            print(f"Error reading bag metadata file {rosbag2_metadata_path}: {e}")
    else:
        print(f"Bag metadata file does not exist: {rosbag2_metadata_path}")
    return bag_info


def print_bag_topics(bag_path: str, bag_dir_name: str = "rosbag2"):
    """
    Retrieves the list of topics from a ROS2 bag file.

    Args:
        bag_path (str): Path to the ROS2 bag file.

    Returns:
        list: A list of topic names.
    """
    bag_info = get_bag_info(os.path.join(bag_path, bag_dir_name))
    if not bag_info:
        raise ValueError(f"Could not retrieve bag info for path: {bag_path}")
    if 'rosbag2_bagfile_information' not in bag_info and 'topics_with_message_count' not in bag_info['rosbag2_bagfile_information']:
        raise ValueError(f"Invalid bag info format for path: {bag_path}")

    topics = bag_info['rosbag2_bagfile_information']['topics_with_message_count']
    print(f"# Topics in bag at {bag_path}:")
    for topic in topics:
        metadata = topic.get('topic_metadata', {})
        topic_name = metadata.get('name', 'unknown')
        topic_type = metadata.get('type', 'unknown')
        topic_message_count = topic.get('message_count', 0)
        print(f"  - Topic: {topic_name}, Type: {topic_type}, Message Count: {topic_message_count}")


def get_behavior_info(behavior_name: str, behavior_dataframe: pd.DataFrame):
    """
    Retrieves information for each instance of a specified behavior.

    Args:
        behavior_name (str): The name of the behavior to filter.
        behavior_dataframe (pd.DataFrame): DataFrame containing columns: timestamp, behavior_name, behavior_id, status_name,

    Returns:
        pd.DataFrame: DataFrame with columns: behavior_name, id, start_time, end_time, duration
    """
    behavior_df = behavior_dataframe[behavior_dataframe['behavior_name'] == behavior_name].copy()

    if behavior_df.empty:
        cols = ['behavior_name', 'id', 'duration', 'test', 'config']
        return pd.DataFrame(columns=cols)

    results = []

    # Group by behavior_id, test, and config to handle multiple instances and configs
    group_cols = ['behavior_id', 'test', 'config']

    for group_keys, group in behavior_df.groupby(group_cols, observed=False):
        # Unpack group_keys depending on grouping columns
        if len(group_cols) == 1:
            behavior_id = group_keys
            test = group['test'].iloc[0] if 'test' in behavior_df.columns else None
            config = group['config'].iloc[0] if 'config' in behavior_df.columns else None
        elif len(group_cols) == 2:
            behavior_id, test = group_keys
            config = group['config'].iloc[0] if 'config' in behavior_df.columns else None
        else:
            behavior_id, test, config = group_keys

        # Find first RUNNING timestamp
        start_rows = group[group['status_name'] == 'RUNNING'].sort_values('timestamp')
        if start_rows.empty:
            continue  # RUNNING not found, skip this behavior instance

        start_time = start_rows.iloc[0]['timestamp']

        # Find first SUCCESS or FAILURE timestamp after start
        end_rows = group[group['status_name'].isin(['SUCCESS', 'FAILURE'])].sort_values('timestamp')
        if end_rows.empty:
            continue  # No terminal state found, skip this behavior instance

        end_time = end_rows.iloc[0]['timestamp']

        record = {
            'behavior_name': behavior_name,
            'id': behavior_id,
            'start_time': start_time,
            'end_time': end_time,
            'duration': end_time - start_time,
            'test': test,
            'config': config
        }

        results.append(record)

    return pd.DataFrame(results)


def calculate_speeds_from_poses(df_groundtruth):
    # Calculate linear and angular speeds from ground truth data
    group_cols = ['test', 'config']
    min_dt = 1e-6

    result_dfs = []

    for _, group in df_groundtruth.groupby(group_cols, observed=False):
        # Need at least 2 data points to calculate speeds
        if len(group) < 2:
            continue

        df_gt_speeds = group[['test', 'config', 'position.x', 'position.y',
                              'orientation.yaw', 'timestamp']].copy()

        # Calculate time differences (dt)
        dt = np.diff(df_gt_speeds['timestamp'].values)

        # Calculate position changes
        dx = np.diff(df_gt_speeds['position.x'].values)
        dy = np.diff(df_gt_speeds['position.y'].values)

        # Calculate yaw changes for angular speed
        dyaw = np.diff(df_gt_speeds['orientation.yaw'].values)
        # Normalize angle differences to [-pi, pi]
        dyaw = np.arctan2(np.sin(dyaw), np.cos(dyaw))

        # Filter out very small time differences to avoid division issues
        valid_mask = dt > min_dt

        # Calculate speeds only for valid time differences
        linear_speed = np.zeros_like(dt)
        angular_speed = np.zeros_like(dt)

        linear_speed[valid_mask] = np.sqrt(dx[valid_mask]**2 + dy[valid_mask]**2) / dt[valid_mask]
        angular_speed[valid_mask] = dyaw[valid_mask] / dt[valid_mask]

        # Add speeds to dataframe (shift by 1 since diff reduces length by 1)
        df_gt_speeds['linear_speed'] = np.append(linear_speed, np.nan)
        df_gt_speeds['angular_speed'] = np.append(angular_speed, np.nan)
        df_gt_speeds['dt'] = np.append(dt, np.nan)

        # Remove the last row with NaN values and rows with invalid dt
        df_gt_speeds = df_gt_speeds[:-1].copy()
        df_gt_speeds = df_gt_speeds[df_gt_speeds['dt'] > min_dt].copy()

        # Only add if we have valid data remaining
        if not df_gt_speeds.empty:
            result_dfs.append(df_gt_speeds)

    # Return empty dataframe with correct columns if no valid data
    if not result_dfs:
        return pd.DataFrame(columns=['test', 'config', 'position.x', 'position.y',
                                     'orientation.yaw', 'timestamp', 'linear_speed',
                                     'angular_speed', 'dt'])

    return pd.concat(result_dfs, ignore_index=True)
