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

"""Extract localization error (estimated vs ground truth) from ROS2 rosbags."""
import argparse
import csv
import math
import os
import sys
import time
from multiprocessing import Pool, cpu_count
from collections import defaultdict

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, write_provenance_entry
from rosidl_runtime_py.utilities import get_message


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, amcl_topic, gt_topic, csv_filename = args
    return process_rosbag(bag_path, amcl_topic, gt_topic, csv_filename)


def find_nearest_pose(poses_dict, timestamp, max_time_diff=0.5):
    """
    Find the ground truth pose nearest to the given timestamp.
    
    Args:
        poses_dict: Dictionary mapping timestamps to poses
        timestamp: Target timestamp
        max_time_diff: Maximum allowed time difference in seconds
        
    Returns:
        Pose (x, y) tuple or None if no close match found
    """
    timestamps = sorted(poses_dict.keys())
    
    # Find the closest timestamp
    closest_idx = None
    min_diff = float('inf')
    
    for i, ts in enumerate(timestamps):
        diff = abs(float(ts) - float(timestamp))
        if diff < min_diff:
            min_diff = diff
            closest_idx = i
    
    if closest_idx is not None and min_diff <= max_time_diff:
        closest_ts = timestamps[closest_idx]
        return poses_dict[closest_ts]
    
    return None


def process_rosbag(bag_path, amcl_topic, gt_topic, csv_filename):
    """Process a single rosbag and extract localization error (AMCL vs ground truth)."""
    try:
        reader = rosbag2_py.SequentialReader()
        try:
            reader.open(
                rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
                rosbag2_py.ConverterOptions(
                    input_serialization_format="cdr", output_serialization_format="cdr"
                ),
            )
        except Exception as e:
            print(f"✗ {bag_path}: Skipped (cannot open rosbag: {e})")
            return 0

        topic_types = reader.get_all_topics_and_types()

        def typename(topic_name):
            for topic_type in topic_types:
                if topic_type.name == topic_name:
                    return topic_type.type
            raise ValueError(f"topic {topic_name} not in bag")

        # Check if both topics exist
        try:
            amcl_type_name = typename(amcl_topic)
            gt_type_name = typename(gt_topic)
        except ValueError as e:
            print(f"✗ {bag_path}: {e}")
            return 0

        amcl_type = get_message(amcl_type_name)
        gt_type = get_message(gt_type_name)
        
        # First pass: collect all poses
        amcl_poses = {}  # timestamp -> (x, y)
        gt_poses = {}    # timestamp -> (x, y)
        
        reader = rosbag2_py.SequentialReader()
        try:
            reader.open(
                rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
                rosbag2_py.ConverterOptions(
                    input_serialization_format="cdr", output_serialization_format="cdr"
                ),
            )
        except Exception as e:
            print(f"✗ {bag_path}: Skipped during read pass (cannot open rosbag: {e})")
            return 0
        
        while reader.has_next():
            topic_name, data, timestamp = reader.read_next()
            ts_sec = timestamp / 1000000000.0  # Convert nanoseconds to seconds
            
            if topic_name == amcl_topic:
                msg = deserialize_message(data, amcl_type)
                pose = msg.pose.pose
                amcl_poses[ts_sec] = (pose.position.x, pose.position.y)
            elif topic_name == gt_topic:
                msg = deserialize_message(data, gt_type)
                pose = msg.pose.pose
                gt_poses[ts_sec] = (pose.position.x, pose.position.y)
        
        if not amcl_poses:
            print(f"✗ {bag_path}: No AMCL poses found")
            return 0
        
        if not gt_poses:
            print(f"✗ {bag_path}: No ground truth poses found")
            return 0
        
        # Compute errors for each AMCL pose matched with nearest ground truth
        fieldnames = ['timestamp', 'error_x_meters', 'error_y_meters', 'error_distance_meters']
        record_count = 0
        
        csv_file_path = os.path.join(os.path.dirname(bag_path), csv_filename)
        
        with open(csv_file_path, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            for amcl_ts in sorted(amcl_poses.keys()):
                amcl_x, amcl_y = amcl_poses[amcl_ts]
                
                # Find nearest ground truth pose
                gt_pose = find_nearest_pose(gt_poses, amcl_ts, max_time_diff=0.5)
                
                if gt_pose is not None:
                    gt_x, gt_y = gt_pose
                    
                    # Calculate error (estimated - ground truth)
                    error_x = amcl_x - gt_x
                    error_y = amcl_y - gt_y
                    error_distance = math.sqrt(error_x**2 + error_y**2)
                    
                    writer.writerow({
                        'timestamp': amcl_ts,
                        'error_x_meters': error_x,
                        'error_y_meters': error_y,
                        'error_distance_meters': error_distance,
                    })
                    record_count += 1
        
        if record_count > 0:
            print(f"✓ {csv_file_path}: {record_count} localization error records")
            return record_count
        else:
            print(f"✗ {bag_path}: No error records generated (mismatched timestamps?)")
            return 0

    except Exception as e:
        # Suppress verbose traceback for common errors
        error_msg = str(e)
        if "No storage could be initialized" in error_msg or "Could not open" in error_msg:
            print(f"✗ {bag_path}: Skipped (no valid rosbag data)")
            return 0
        print(f"✗ {bag_path}: Error - {error_msg}")
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
        "--amcl-topic",
        type=str,
        default="/amcl_pose",
        help="Topic containing AMCL pose (PoseWithCovarianceStamped messages, default: /amcl_pose)"
    )
    parser.add_argument(
        "--gt-topic",
        type=str,
        default="/ground_truth_odom",
        help="Topic containing ground truth odometry (Odometry messages, default: /ground_truth_odom)"
    )
    parser.add_argument(
        "input",
        help="Input directory path to search for rosbags"
    )
    parser.add_argument(
        "--csv-filename",
        type=str,
        default="localization_error.csv",
        help="Output CSV file name (default: localization_error.csv)"
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (output/source paths relative to input dir)"
    )

    args = parser.parse_args()

    # Find all rosbags in subdirectories
    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(f"Found {len(rosbag_paths)} rosbag(s)")

    # Process rosbags in parallel
    start_time = time.time()
    pool_args = [(bag_path, args.amcl_topic, args.gt_topic, args.csv_filename) for bag_path in rosbag_paths]

    if args.workers > 1 and len(rosbag_paths) > 1:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, pool_args)
    else:
        results = [process_rosbag_wrapper(arg) for arg in pool_args]

    elapsed = time.time() - start_time

    # Count successes and errors
    success_count = sum(1 for r in results if r > 0)
    error_count = sum(1 for r in results if r < 0)
    total_records = sum(r for r in results if r > 0)

    print(f"\nProcessed {len(rosbag_paths)} rosbag(s) in {elapsed:.1f}s")
    print(f"  ✓ Success: {success_count}")
    if error_count > 0:
        print(f"  ✗ Skipped/Errors: {error_count}")
    print(f"  Total records: {total_records}")

    # Write provenance if requested
    if args.provenance_file and success_count > 0:
        input_abs = os.path.abspath(args.input)
        for i, bag_path in enumerate(rosbag_paths):
            if results[i] > 0:
                bag_abs = os.path.abspath(bag_path)
                bag_rel = os.path.relpath(bag_abs, input_abs)
                csv_path = os.path.join(bag_abs, args.csv_filename)
                csv_rel = os.path.relpath(csv_path, input_abs)
                write_provenance_entry(
                    args.provenance_file,
                    output_rel=csv_rel,
                    sources_rel=[bag_rel],
                    plugin_name="rosbags_localization_error_to_csv",
                    params={"amcl_topic": args.amcl_topic, "gt_topic": args.gt_topic, "csv_filename": args.csv_filename},
                )

    # Return success for postprocessing even when all rosbags are skipped.
    # Failed or incomplete runs may legitimately have missing/corrupted bag data.
    return 0


if __name__ == "__main__":
    sys.exit(main())
