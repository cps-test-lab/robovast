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

"""Extract ROS2 action feedback and status messages from rosbags to YAML."""
import argparse
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
import yaml
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, write_provenance_entry
from rosidl_runtime_py.utilities import get_message

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def msg_to_dict(msg):
    """Recursively convert a ROS message to a Python dict/list for YAML serialization.

    Special handling:
    - unique_identifier_msgs/UUID  → hex string
    - builtin_interfaces/Time      → decimal seconds (float)
    """
    if isinstance(msg, bytes):
        return list(msg)
    if _HAS_NUMPY and isinstance(msg, np.ndarray):
        return msg.tolist()
    if isinstance(msg, (bool, int, float, str)) or msg is None:
        return msg
    if hasattr(msg, 'get_fields_and_field_types'):
        fields = set(msg.get_fields_and_field_types().keys())
        if fields == {'uuid'}:
            return bytearray(msg.uuid).hex()
        if fields == {'sec', 'nanosec'}:
            return msg.sec + msg.nanosec / 1_000_000_000.0
        return {
            field: msg_to_dict(getattr(msg, field))
            for field in msg.get_fields_and_field_types()
        }
    try:
        return [msg_to_dict(item) for item in msg]
    except TypeError:
        return msg


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, action, yaml_filename = args
    return process_rosbag(bag_path, action, yaml_filename)


def process_rosbag(bag_path, action, yaml_filename):
    """Process a single rosbag and write action feedback and status to a YAML file."""
    try:
        action_name = action.lstrip('/')
        feedback_topic = f"/{action_name}/_action/feedback"
        status_topic = f"/{action_name}/_action/status"

        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
            rosbag2_py.ConverterOptions(
                input_serialization_format="cdr", output_serialization_format="cdr"
            ),
        )

        topic_types = reader.get_all_topics_and_types()
        topic_type_map = {t.name: t.type for t in topic_types}
        available_topics = set(topic_type_map)

        if feedback_topic not in available_topics and status_topic not in available_topics:
            print(f"✗ {bag_path}: Neither {feedback_topic} nor {status_topic} found")
            action_topics = sorted(t for t in available_topics if "_action" in t)
            if action_topics:
                print(f"  Action topics in bag: {action_topics}")
            return 0

        feedback_entries = []
        status_entries = []

        while reader.has_next():
            topic, data, timestamp = reader.read_next()

            if topic == feedback_topic:
                msg_type = get_message(topic_type_map[topic])
                msg = deserialize_message(data, msg_type)
                entry = {"timestamp": timestamp / 1_000_000_000.0}
                entry.update(msg_to_dict(msg))
                feedback_entries.append(entry)
            elif topic == status_topic:
                msg_type = get_message(topic_type_map[topic])
                msg = deserialize_message(data, msg_type)
                entry = {"timestamp": timestamp / 1_000_000_000.0}
                entry.update(msg_to_dict(msg))
                status_entries.append(entry)

        output = {
            "action": action_name,
            "feedback": feedback_entries,
            "status": status_entries,
        }

        yaml_file_path = os.path.join(os.path.dirname(bag_path), yaml_filename)
        with open(yaml_file_path, 'w', encoding='utf-8') as f:
            yaml.dump(output, f, default_flow_style=False, allow_unicode=True)

        total = len(feedback_entries) + len(status_entries)
        if total > 0:
            print(f"✓ {yaml_file_path}: {len(feedback_entries)} feedback, {len(status_entries)} status messages")
            return total
        else:
            print(f"✗ {bag_path}: No messages found on action topics")
            return 0

    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        return -2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})"
    )
    parser.add_argument(
        "action",
        help=(
            "Action name to extract (e.g. 'navigate_to_pose'). "
            "Reads /<action>/_action/feedback and /<action>/_action/status."
        )
    )
    parser.add_argument(
        "input",
        help="Input directory path to search for rosbags"
    )
    parser.add_argument(
        "--yaml-filename",
        type=str,
        default=None,
        help="Output YAML file name (default: action_<action>.yaml)"
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (output/source paths relative to input dir)"
    )

    args = parser.parse_args()

    action_name = args.action.lstrip('/')
    if args.yaml_filename is None:
        args.yaml_filename = f"action_{action_name}.yaml"

    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(f"Found {len(rosbag_paths)} rosbags to process. Using {args.workers} parallel workers...")
    print(f"Action: {action_name}")
    print(f"Output YAML filename: {args.yaml_filename}")

    start = time.time()
    total_messages = 0
    processed_bags = 0

    process_args = [(bag_path, action_name, args.yaml_filename) for bag_path in rosbag_paths]

    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    failed_bags = 0
    error_bags = 0
    input_root = os.path.abspath(args.input)

    for i, msg_count in enumerate(results):
        if msg_count == -2:
            error_bags += 1
        elif msg_count > 0:
            total_messages += msg_count
            processed_bags += 1
            if args.provenance_file:
                bag_path = rosbag_paths[i]
                yaml_file_path = os.path.join(os.path.dirname(bag_path), args.yaml_filename)
                output_rel = os.path.relpath(yaml_file_path, input_root)
                source_rel = os.path.relpath(bag_path, input_root)
                write_provenance_entry(
                    args.provenance_file,
                    output_rel,
                    [source_rel],
                    "rosbags_action_to_yaml",
                    params={"action": action_name, "yaml_filename": args.yaml_filename},
                )
        elif msg_count == 0:
            failed_bags += 1

    elapsed = time.time() - start
    print(f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, "
          f"{error_bags} errors, {failed_bags} failed), time {elapsed:.2f}s")

    if processed_bags == 0 and not error_bags:
        print(f"✗ Warning: No action messages found for '{action_name}' in any rosbag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
