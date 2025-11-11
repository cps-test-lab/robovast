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
from pathlib import Path

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


def gen_msg_values(msg, prefix=""):
    if isinstance(msg, list):
        for i, val in enumerate(msg):
            yield from gen_msg_values(val, f"{prefix}[{i}]")
    elif hasattr(msg, "get_fields_and_field_types"):
        for field, type_ in msg.get_fields_and_field_types().items():
            val = getattr(msg, field)
            full_field_name = prefix + "." + field if prefix else field
            if type_.startswith("sequence<"):
                for i, aval in enumerate(val):
                    yield from gen_msg_values(aval, f"{full_field_name}[{i}]")
            else:
                yield from gen_msg_values(val, full_field_name)
    else:
        yield prefix, msg


def find_rosbags(directory):
    """Find all rosbag directories in subdirectories."""
    rosbag_dirs = []
    for root, _, files in os.walk(directory):
        # Check if this directory contains .mcap files or metadata.yaml (rosbag indicators)
        has_mcap = any(f.endswith('.mcap') for f in files)
        has_metadata = 'metadata.yaml' in files

        if has_mcap or has_metadata:
            rosbag_dirs.append(root)

    return rosbag_dirs


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, skipped_topics = args
    return process_rosbag(bag_path, skipped_topics)


def process_rosbag(bag_path, skipped_topics):
    """Process a single rosbag and save to CSV in the output directory."""
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

        # Collect all unique fieldnames from all records
        fieldnames = []
        fieldnames_set = set()
        for record in records:
            for key in record.keys():
                if key not in fieldnames_set:
                    fieldnames.append(key)
                    fieldnames_set.add(key)

        # Write to CSV using DictWriter
        with open(output_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

        print(f"✓ {output_file}: {len(records)} messages")
        return len(records)
    else:
        print(f"✗ {Path(bag_path).name}: No records found")
        return 0


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
        return

    print(f"Found {len(rosbag_paths)} rosbags to process:")
    for path in rosbag_paths:
        print(f"  - {path}")
    print(f"Using {args.workers} parallel workers")
    print()

    start = time.time()
    total_records = 0
    processed_bags = 0

    # Prepare arguments for parallel processing
    process_args = []
    for bag_path in rosbag_paths:
        process_args.append((bag_path, skipped_topics))

    # Process rosbags in parallel
    print("Processing rosbags...")
    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except Exception as e:
        print(f"✗ Error during rosbag processing: {str(e)}")
        sys.exit(1)
    print()  # Add a blank line after all processing output

    # Calculate summary statistics
    for records_count in results:
        total_records += records_count
        if records_count > 0:
            processed_bags += 1

    elapsed = time.time() - start
    print(f"\nSummary:")
    print(f"Processed {processed_bags}/{len(rosbag_paths)} rosbags successfully")
    print(f"Total records: {total_records}")
    print(f"Total time: {elapsed:.2f} seconds")
    if elapsed > 0:
        print(f"Average processing rate: {total_records/elapsed:.0f} records/second")


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
