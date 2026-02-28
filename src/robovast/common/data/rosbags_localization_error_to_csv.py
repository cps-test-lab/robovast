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

"""Extract localization error (covariance) data from ROS2 rosbags."""
import argparse
import csv
import math
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, write_provenance_entry
from rosidl_runtime_py.utilities import get_message


def quat_to_rpy(x, y, z, w):
    """Convert quaternion (x, y, z, w) to roll, pitch, yaw in radians."""
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(2 * (w * y - z * x))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll, pitch, yaw


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, topic, csv_filename = args
    return process_rosbag(bag_path, topic, csv_filename)


def process_rosbag(bag_path, topic, csv_filename):
    """Process a single rosbag and extract localization error data to CSV."""
    try:
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

        # Check if topic exists
        try:
            msg_type_name = typename(topic)
        except ValueError as e:
            print(f"✗ {bag_path}: {e}")
            return 0

        msg_type = get_message(msg_type_name)
        record_count = 0

        # Covariance matrix is 6x6 (36 values) for [x, y, z, roll, pitch, yaw]
        # We'll extract the diagonal values (variance) and some key correlations
        fieldnames = [
            'timestamp',
            'position.x', 'position.y', 'position.z',
            'orientation.roll', 'orientation.pitch', 'orientation.yaw',
            'covariance.x_x', 'covariance.y_y', 'covariance.z_z',
            'covariance.roll_roll', 'covariance.pitch_pitch', 'covariance.yaw_yaw',
            'covariance.x_y', 'covariance.x_yaw', 'covariance.y_yaw'
        ]

        csv_file_path = os.path.join(os.path.dirname(bag_path), csv_filename)

        with open(csv_file_path, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            while reader.has_next():
                topic_name, data, timestamp = reader.read_next()

                if topic_name != topic:
                    continue

                msg = deserialize_message(data, msg_type)

                # Extract pose
                pose = msg.pose.pose
                position = pose.position
                orientation = pose.orientation

                # Convert quaternion to roll, pitch, yaw
                roll, pitch, yaw = quat_to_rpy(
                    orientation.x, orientation.y, orientation.z, orientation.w
                )

                # Extract covariance matrix (6x6 = 36 values)
                # Row-major order: [x, y, z, roll, pitch, yaw]
                cov = msg.pose.covariance
                
                writer.writerow({
                    'timestamp': timestamp / 1000000000.0,
                    'position.x': position.x,
                    'position.y': position.y,
                    'position.z': position.z,
                    'orientation.roll': roll,
                    'orientation.pitch': pitch,
                    'orientation.yaw': yaw,
                    # Diagonal elements (variances)
                    'covariance.x_x': cov[0],      # [0,0]
                    'covariance.y_y': cov[7],      # [1,1]
                    'covariance.z_z': cov[14],     # [2,2]
                    'covariance.roll_roll': cov[21],  # [3,3]
                    'covariance.pitch_pitch': cov[28], # [4,4]
                    'covariance.yaw_yaw': cov[35], # [5,5]
                    # Key off-diagonal elements (covariances)
                    'covariance.x_y': cov[1],      # [0,1]
                    'covariance.x_yaw': cov[5],    # [0,5]
                    'covariance.y_yaw': cov[11],   # [1,5]
                })
                record_count += 1

        if record_count > 0:
            print(f"✓ {csv_file_path}: {record_count} localization error records")
            return record_count
        else:
            print(f"✗ {bag_path}: No localization error records found")
            return 0

    except Exception as e:
        # Suppress verbose traceback for common errors like missing/corrupt rosbags
        error_msg = str(e)
        if "No storage could be initialized" in error_msg or "Could not open" in error_msg:
            print(f"✗ {bag_path}: Skipped (no valid rosbag data)")
        else:
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
        "--topic",
        type=str,
        default="/amcl_pose",
        help="Topic containing PoseWithCovarianceStamped messages (default: /amcl_pose)"
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
    pool_args = [(bag_path, args.topic, args.csv_filename) for bag_path in rosbag_paths]

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
                    params={"topic": args.topic, "csv_filename": args.csv_filename},
                )

    # Return success if at least one rosbag was processed successfully
    # Only fail if ALL rosbags failed or none were found
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
