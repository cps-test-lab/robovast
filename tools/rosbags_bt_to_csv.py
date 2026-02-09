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

"""Extract behavior start/end times from py-tree behavior tree snapshots in ROS2 rosbags."""
import argparse
import csv
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from py_trees_ros_interfaces.msg import BehaviourTree
from rclpy.serialization import deserialize_message

from rosbags_common import (
    find_rosbags,
    should_skip_processing,
    write_hash_file,
    create_rosbag_prov,
)

# Get script name without extension to use as prefix
SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]


def reconstruct_behavior_timeline(bag_path, output_file):
    """
    Extract behavior status changes from rosbag snapshots and write directly to CSV.

    Args:
        bag_path: Path to the rosbag directory
        output_file: Path to output CSV file

    Returns:
        Number of records written
    """
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )

    status_names = {1: 'INVALID', 2: 'RUNNING', 3: 'SUCCESS', 4: 'FAILURE'}

    # Create UUID to integer mapping
    uuid_to_int = {}
    next_id = 1

    fieldnames = ['timestamp', 'behavior_name', 'behavior_id', 'status', 'status_name', 'class_name']
    record_count = 0

    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        while reader.has_next():
            topic, data, timestamp = reader.read_next()

            # Only process the snapshots topic
            if topic != "/scenario_execution/snapshots":
                continue

            msg = deserialize_message(data, BehaviourTree)

            # Record each behavior's status
            for behavior in msg.behaviours:
                uuid_str = str(behavior.own_id)

                # Assign integer ID if not seen before
                if uuid_str not in uuid_to_int:
                    uuid_to_int[uuid_str] = next_id
                    next_id += 1

                writer.writerow({
                    'timestamp': timestamp / 1000000000.0,
                    'behavior_name': behavior.name,
                    'behavior_id': uuid_to_int[uuid_str],
                    'status': behavior.status,
                    'status_name': status_names.get(behavior.status, 'UNKNOWN'),
                    'class_name': behavior.class_name
                })
                record_count += 1

    return record_count


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    return process_rosbag(*args)


def process_rosbag(bag_path, csv_filename, root_folder):
    """Process a single rosbag and extract behavior status changes to CSV."""
    try:
        # Check if we should skip processing based on hash
        if should_skip_processing(bag_path, prefix=SCRIPT_NAME):
            return -1  # Return -1 to indicate skipped

        parent_folder = os.path.abspath(os.path.dirname(bag_path))
        output_file = os.path.join(parent_folder, csv_filename)

        record_count = reconstruct_behavior_timeline(bag_path, output_file)
        create_rosbag_prov(bag_path, output_file, root_folder)

        # Write hash file after successful processing
        write_hash_file(bag_path, prefix=SCRIPT_NAME)

        if record_count > 0:
            print(f"✓ {output_file}: {record_count} status records")
            return record_count
        else:
            print(f"✗ {bag_path}: No behavior records found")
            return 0
    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        write_hash_file(bag_path, prefix=SCRIPT_NAME)
        return -2  # Return -2 to indicate error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument(
        "--csv-filename",
        type=str,
        default="behaviors.csv",
        help="Output CSV file name (default: <test-dir>/behaviors.csv)"
    )

    args = parser.parse_args()

    # Find all rosbags in subdirectories
    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(f"Found {len(rosbag_paths)} rosbags to process. Using {args.workers} parallel workers...")

    start = time.time()
    total_behaviors = 0
    processed_bags = 0

    # Prepare arguments for parallel processing
    process_args = [
        (bag_path, args.csv_filename, args.input) for bag_path in rosbag_paths
    ]

    # Process rosbags in parallel
    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    # Calculate summary statistics
    skipped_bags = 0
    failed_bags = 0
    error_bags = 0
    for behavior_count in results:
        if behavior_count == -1:
            skipped_bags += 1
        elif behavior_count == -2:
            error_bags += 1
        elif behavior_count > 0:
            total_behaviors += behavior_count
            processed_bags += 1
        elif behavior_count == 0:
            failed_bags += 1

    elapsed = time.time() - start
    print(f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, {
          error_bags} errors, {failed_bags} failed, {skipped_bags} skipped), time {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
