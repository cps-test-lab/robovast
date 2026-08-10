"""Readers for rosbag artifacts on disk.

Membership rule: a function belongs here when it reads a **rosbag artifact** — not when its
input merely happens to have come from ROS. Derivations over a run-data frame, whatever
produced it, go in :mod:`~robovast.common.analysis.metrics`.
"""

import os

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
