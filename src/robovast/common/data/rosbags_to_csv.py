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

"""script that reads ROS2 messages using the rosbag2_py API."""
import argparse
import csv
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, gen_msg_values
from rosidl_runtime_py.utilities import get_message


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, skipped_topics = args
    return process_rosbag(bag_path, skipped_topics)


def process_rosbag(bag_path, skipped_topics):
    """Process a single rosbag and save to CSV in the output directory."""
    try:
        records = []
        append = records.append  # Local variable for faster access

        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
            rosbag2_py.ConverterOptions(
                input_serialization_format="cdr", output_serialization_format="cdr"
            ),
        )

        topic_types = reader.get_all_topics_and_types()

        def typename(topic_name):
            for topic_type in topic_types:
                if topic_type.name == topic_name:
                    return topic_type.type
            raise ValueError(f"topic {topic_name} not in bag")

        while reader.has_next():
            topic, data, timestamp = reader.read_next()
            msg_type = get_message(typename(topic))
            msg = deserialize_message(data, msg_type)

            if topic not in skipped_topics:
                fields = dict(gen_msg_values(msg))
                record = {
                    "timestamp": timestamp,
                    "topic": topic,
                    "type": type(msg).__name__,
                    **fields
                }
                append(record)

        if records:
            # Use only the immediate parent folder name for the CSV filename
            parent_folder = os.path.abspath(os.path.dirname(bag_path))
            output_file = os.path.join(parent_folder, os.path.basename(bag_path) + '.csv')

            # Collect all fieldnames from all records (dynamic fields)
            fieldnames_set = set()
            for record in records:
                fieldnames_set.update(record.keys())
            # Sort fieldnames, but keep timestamp, topic, type first
            base_fields = ['timestamp', 'topic', 'type']
            other_fields = sorted(fieldnames_set - set(base_fields))
            fieldnames = base_fields + other_fields

            # Write to CSV using DictWriter
            with open(output_file, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for record in records:
                    writer.writerow(record)

            print(f"✓ {output_file}: {len(records)} messages")
            return len(records)
        else:
            print(f"✗ {bag_path}: No records found")
            return 0
    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        return -2  # Return -2 to indicate error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-topic",
        action="append",
        default=["/scenario_execution/snapshots", "/local_costmap/costmap", "/map"],
        help="Topic to skip (can be specified multiple times)"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})"
    )
    parser.add_argument(
        "input",
        help="input directory path to search for rosbags"
    )

    args = parser.parse_args()
    skipped_topics = set(args.skip_topic)

    # Find all rosbags in subdirectories
    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(f"Found {len(rosbag_paths)} rosbags to process. Using {args.workers} parallel workers...")

    start = time.time()
    total_records = 0
    processed_bags = 0

    # Prepare arguments for parallel processing
    process_args = []
    for bag_path in rosbag_paths:
        process_args.append((bag_path, skipped_topics))

    # Process rosbags in parallel
    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1
    # Calculate summary statistics
    failed_bags = 0
    error_bags = 0
    for records_count in results:
        if records_count == -2:
            error_bags += 1
        elif records_count > 0:
            total_records += records_count
            processed_bags += 1
        elif records_count == 0:
            failed_bags += 1

    elapsed = time.time() - start
    print(f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, "
          f"{error_bags} errors, {failed_bags} failed), time {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
