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

"""Extract ROS2 action feedback and status messages from rosbags to CSV."""
import argparse
import csv
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, write_provenance_entry
from rosidl_runtime_py.utilities import get_message

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def msg_to_dict(msg):  # pylint: disable=too-many-return-statements
    """Recursively convert a ROS message to a Python dict/list for flattening.

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


def flatten_to_columns(obj, prefix="", sep="_"):
    """Recursively flatten nested dicts and lists to flat key-value pairs for CSV.

    Dict keys become column names via prefix + key.
    List elements become prefix_0, prefix_1, etc.
    Primitives are leaf values.
    """
    if isinstance(obj, dict):
        result = {}
        for key, val in obj.items():
            result.update(flatten_to_columns(val, f"{prefix}{sep}{key}" if prefix else key, sep))
        return result
    if isinstance(obj, list):
        result = {}
        for i, item in enumerate(obj):
            result.update(flatten_to_columns(item, f"{prefix}{sep}{i}", sep))
        return result
    return {prefix: obj}


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, action, filename_prefix = args
    return process_rosbag(bag_path, action, filename_prefix)


def process_rosbag(bag_path, action, filename_prefix):
    """Process a single rosbag and write action feedback and status to CSV files."""
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
            return (0, [])

        feedback_rows = []
        status_rows = []

        while reader.has_next():
            topic, data, timestamp = reader.read_next()

            if topic == feedback_topic:
                msg_type = get_message(topic_type_map[topic])
                msg = deserialize_message(data, msg_type)
                entry = {"timestamp": timestamp / 1_000_000_000.0}
                entry.update(msg_to_dict(msg))
                feedback_rows.append(flatten_to_columns(entry))
            elif topic == status_topic:
                msg_type = get_message(topic_type_map[topic])
                msg = deserialize_message(data, msg_type)
                entry = {"timestamp": timestamp / 1_000_000_000.0}
                entry.update(msg_to_dict(msg))
                status_rows.append(flatten_to_columns(entry))

        parent_dir = os.path.dirname(bag_path)
        feedback_path = os.path.join(parent_dir, f"{filename_prefix}_feedback.csv")
        status_path = os.path.join(parent_dir, f"{filename_prefix}_status.csv")

        total = 0
        if feedback_rows:
            all_keys = sorted(set().union(*(r.keys() for r in feedback_rows)))
            with open(feedback_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(feedback_rows)
            total += len(feedback_rows)

        if status_rows:
            all_keys = sorted(set().union(*(r.keys() for r in status_rows)))
            with open(status_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(status_rows)
            total += len(status_rows)

        if total > 0:
            print(f"✓ {feedback_path if feedback_rows else status_path}: "
                  f"{len(feedback_rows)} feedback, {len(status_rows)} status messages")
            created = []
            if feedback_rows:
                created.append(feedback_path)
            if status_rows:
                created.append(status_path)
            return (total, created)
        else:
            print(f"✗ {bag_path}: No messages found on action topics")
            return (0, [])

    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        return (-2, [])


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
        "--filename-prefix",
        type=str,
        default=None,
        help="Output filename prefix (default: action_<action>). Produces <prefix>_feedback.csv and <prefix>_status.csv"
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (output/source paths relative to input dir)"
    )

    args = parser.parse_args()

    action_name = args.action.lstrip('/')
    if args.filename_prefix is None:
        args.filename_prefix = f"action_{action_name}"

    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(f"Found {len(rosbag_paths)} rosbags to process. Using {args.workers} parallel workers...")
    print(f"Action: {action_name}")
    print(f"Output: {args.filename_prefix}_feedback.csv, {args.filename_prefix}_status.csv")

    start = time.time()
    total_messages = 0
    processed_bags = 0

    process_args = [(bag_path, action_name, args.filename_prefix) for bag_path in rosbag_paths]

    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    failed_bags = 0
    error_bags = 0
    input_root = os.path.abspath(args.input)

    for i, (msg_count, created_files) in enumerate(results):
        if msg_count == -2:
            error_bags += 1
        elif msg_count > 0:
            total_messages += msg_count
            processed_bags += 1
            if args.provenance_file and created_files:
                bag_path = rosbag_paths[i]
                source_rel = os.path.relpath(bag_path, input_root)
                for output_path in created_files:
                    output_rel = os.path.relpath(output_path, input_root)
                    write_provenance_entry(
                        args.provenance_file,
                        output_rel,
                        [source_rel],
                        "rosbags_action_to_csv",
                        params={"action": action_name, "filename_prefix": args.filename_prefix},
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
