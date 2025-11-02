#!/usr/bin/env python3

"""Extract behavior start/end times from py-tree behavior tree snapshots in ROS2 rosbags."""
import argparse
import csv
import os
import sys
from multiprocessing import Pool, cpu_count
from pathlib import Path

import rosbag2_py
from py_trees_ros_interfaces.msg import BehaviourTree
from rclpy.serialization import deserialize_message


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


def find_rosbags(directory):
    """Find all rosbag directories in subdirectories."""
    rosbag_dirs = []
    for root, dirs, files in os.walk(directory):
        # Check if this directory contains .mcap files or metadata.yaml (rosbag indicators)
        has_mcap = any(f.endswith('.mcap') for f in files)
        has_metadata = 'metadata.yaml' in files

        if has_mcap or has_metadata:
            rosbag_dirs.append(root)

    return rosbag_dirs


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    return process_rosbag(*args)


def process_rosbag(bag_path, csv_filename):
    """Process a single rosbag and extract behavior status changes to CSV."""
    try:
        parent_folder = os.path.abspath(os.path.dirname(bag_path))
        output_file = os.path.join(parent_folder, csv_filename)

        record_count = reconstruct_behavior_timeline(bag_path, output_file)

        if record_count > 0:
            print(f"✓ {output_file}: {record_count} status records")
            return record_count
        else:
            print(f"✗ {Path(bag_path).name}: No behavior records found")
            return 0
    except Exception as e:
        print(f"✗ Error processing {Path(bag_path).name}: {str(e)}")
        return 0


def main():
    import time

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
        return

    print(f"Found {len(rosbag_paths)} rosbags to process:")
    for path in rosbag_paths:
        print(f"  - {path}")
    print(f"Using {args.workers} parallel workers")
    print()

    start = time.time()
    total_behaviors = 0
    processed_bags = 0

    # Prepare arguments for parallel processing
    process_args = [(bag_path, args.csv_filename) for bag_path in rosbag_paths]

    # Process rosbags in parallel
    print("Extracting behavior timelines...")
    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except Exception as e:
        print(f"✗ Error during processing: {str(e)}")
        sys.exit(1)
    print()

    # Calculate summary statistics
    for behavior_count in results:
        total_behaviors += behavior_count
        if behavior_count > 0:
            processed_bags += 1

    elapsed = time.time() - start
    print(f"\nSummary:")
    print(f"Processed {processed_bags}/{len(rosbag_paths)} rosbags successfully")
    print(f"Total status records extracted: {total_behaviors}")
    print(f"Total time: {elapsed:.2f} seconds")


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
