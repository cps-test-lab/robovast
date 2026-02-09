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

"""script that get positions in map frame from ROS2 bags tf-messages."""
import argparse
import csv
import math
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import (
    find_rosbags,
    should_skip_processing,
    write_hash_file,
    create_rosbag_prov,
)
from rosidl_runtime_py.utilities import get_message
from tf2_py import (ConnectivityException, ExtrapolationException,
                    LookupException)
from tf2_ros import Buffer

# Get script name without extension to use as prefix
SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, frames, csv_filename, root_folder = args
    return process_rosbag(bag_path, frames, csv_filename, root_folder)


def quat_to_rpy(x, y, z, w):
    """Convert quaternion (x, y, z, w) to roll, pitch, yaw in radians."""
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(2 * (w * y - z * x))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll, pitch, yaw


def process_rosbag(bag_path, frames, csv_filename, root_folder):
    """Process a single rosbag and write pose records directly to CSV file."""
    try:
        # Check if we should skip processing based on hash
        if should_skip_processing(bag_path, prefix=SCRIPT_NAME):
            return (-1, {})  # Return -1 to indicate skipped

        if frames is None:
            raise ValueError("frames parameter must be provided")

        # Initialize TF buffer for transform calculations
        tf_buffer = Buffer()

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

        record_counts = {frame: 0 for frame in frames}
        found_tfs = set()

        fieldnames = ["frame", "timestamp", "position.x", "position.y", "position.z",
                      "orientation.roll", "orientation.pitch", "orientation.yaw"]

        # Open CSV file for writing
        csv_file_path = os.path.join(os.path.dirname(bag_path), csv_filename)
        csvfile = open(csv_file_path, 'w', newline='')
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        try:
            while reader.has_next():
                topic, data, timestamp = reader.read_next()
                msg_type = get_message(typename(topic))
                msg = deserialize_message(data, msg_type)

                # Add transform messages to TF buffer for later lookup
                if topic == "/tf" or topic == "/tf_static" and hasattr(msg, 'transforms'):
                    for transform in msg.transforms:
                        tf_buffer.set_transform(transform, "default_authority")
                        found_tfs.add(f"{transform.header.frame_id} -> {transform.child_frame_id}")

                        # Check if this transform's child frame is one we're interested in
                        for frame in frames:
                            if transform.child_frame_id == frame:
                                try:
                                    # Look up transform from map to the target frame
                                    map_to_frame = tf_buffer.lookup_transform(
                                        "map", frame,
                                        transform.header.stamp
                                    )

                                    # Extract pose data
                                    translation = map_to_frame.transform.translation
                                    rotation = map_to_frame.transform.rotation

                                    roll, pitch, yaw = quat_to_rpy(rotation.x, rotation.y, rotation.z, rotation.w)

                                    # Write row directly to CSV
                                    writer.writerow({
                                        "frame": frame,
                                        "timestamp": timestamp / 1000000000.,
                                        "position.x": translation.x,
                                        "position.y": translation.y,
                                        "position.z": translation.z,
                                        "orientation.roll": roll,
                                        "orientation.pitch": pitch,
                                        "orientation.yaw": yaw,
                                    })
                                    record_counts[frame] += 1

                                except (LookupException, ConnectivityException, ExtrapolationException):
                                    pass
        finally:
            csvfile.close()

        create_rosbag_prov(bag_path, csv_file_path, root_folder)

        # Write hash file after successful processing
        write_hash_file(bag_path, prefix=SCRIPT_NAME)

        total_records = sum(record_counts.values())

        # Report results
        if total_records > 0:
            frame_summary = ", ".join([f"{frame}: {count}" for frame, count in record_counts.items() if count > 0])
            print(f"✓ {csv_file_path}: {total_records} messages ({frame_summary})")
            return (total_records, record_counts)
        else:
            print(f"✗ {bag_path}: No records found")
            if len(frames) == 1:  # Only show TF frames if processing a single frame
                print(f"  Found TF frames: \n{"\n - ".join(found_tfs)}")
            return (0, record_counts)

    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        write_hash_file(bag_path, prefix=SCRIPT_NAME)
        return (-2, {})


def main():

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})"
    )
    parser.add_argument(
        "--frame",
        type=str,
        action="append",
        default=[],
        help="Target frame name(s). Can be specified multiple times, e.g. --frame bla --frame blbu (default: base_link)"
    )
    parser.add_argument(
        "--csv-filename",
        type=str,
        default="poses.csv",
        help="Output CSV file name (default: <test-dir>/poses.csv)"
    )
    parser.add_argument(
        "input",
        help="input directory path to search for rosbags"
    )

    args = parser.parse_args()

    # Find all rosbags in subdirectories
    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    if args.frame == []:
        args.frame = ["base_link"]

    print(f"Found {len(rosbag_paths)} rosbags to process. Using {args.workers} parallel workers...")
    print(f"Target frame(s): {', '.join(args.frame)}")
    print(f"Output CSV filename: {args.csv_filename}")

    start = time.time()
    total_records = 0
    processed_bags = 0

    # Prepare arguments for parallel processing
    process_args = []
    for bag_path in rosbag_paths:
        process_args.append((bag_path, args.frame, args.csv_filename, args.input))

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
    # Aggregate per-frame counts across all bags
    total_frame_counts = {frame: 0 for frame in args.frame}

    for record_count, frame_counts in results:
        if record_count == -1:
            skipped_bags += 1
        elif record_count == -2:
            error_bags += 1
        elif record_count > 0:
            total_records += record_count
            processed_bags += 1
            # Aggregate frame counts
            for frame, count in frame_counts.items():
                total_frame_counts[frame] += count
        elif record_count == 0:
            failed_bags += 1

    elapsed = time.time() - start
    print(f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, {
          error_bags} errors, {failed_bags} failed, {skipped_bags} skipped), time {elapsed:.2f}s")

    # Check if any requested frame has no records
    empty_frames = [frame for frame, count in total_frame_counts.items() if count == 0]
    if empty_frames:
        print(f"✗ Warning: {args.input} No records found for requested frame(s): {', '.join(empty_frames)}")
    return 0


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
