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

"""Script that extracts /rosout log messages from ROS2 bags and writes them to CSV.

Each row in the output CSV corresponds to one rcl_interfaces/msg/Log message
from the /rosout topic with the following columns:

    timestamp   – bag receive time in seconds (float)
    stamp       – message header stamp in seconds (float, sec + nanosec/1e9)
    level       – numeric log level (10=DEBUG, 20=INFO, 30=WARN, 40=ERROR, 50=FATAL)
    level_name  – human-readable level string (DEBUG / INFO / WARN / ERROR / FATAL)
    name        – logger / node name
    msg         – log message text
    file        – source file path
    function    – source function name
    line        – source line number

Usage example::

    python rosbags_rosout_to_csv.py /path/to/results \\
        --min-level WARN \\
        --csv-filename rosout.csv \\
        --workers 4
"""
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

ROSOUT_TOPIC = "/rosout"

# rcl_interfaces/msg/Log level constants
_LEVEL_NAMES = {
    10: "DEBUG",
    20: "INFO",
    30: "WARN",
    40: "ERROR",
    50: "FATAL",
}

_LEVEL_BY_NAME = {name: level for level, name in _LEVEL_NAMES.items()}

FIELDNAMES = [
    "timestamp",
    "stamp",
    "level",
    "level_name",
    "name",
    "msg",
    "file",
    "function",
    "line",
]


def _level_name(level: int) -> str:
    """Return a human-readable string for a numeric rcl log level."""
    return _LEVEL_NAMES.get(level, str(level))


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, min_level, csv_filename, output_file = args
    return process_rosbag(bag_path, min_level, csv_filename, output_file)


def process_rosbag(bag_path: str, min_level: int, csv_filename: str, output_file: str = None):
    """Process a single rosbag and write /rosout records to a CSV file.

    Args:
        bag_path: Path to the rosbag directory (mcap format).
        min_level: Minimum numeric log level to include (e.g. 20 for INFO+).
        csv_filename: Name of the output CSV file written next to the bag.

    Returns:
        Number of records written (>= 0), or -2 on error.
    """
    try:
        reader = rosbag2_py.SequentialReader()
        reader.open(
            rosbag2_py.StorageOptions(uri=bag_path, storage_id="mcap"),
            rosbag2_py.ConverterOptions(
                input_serialization_format="cdr",
                output_serialization_format="cdr",
            ),
        )

        topic_types = reader.get_all_topics_and_types()
        topic_type_map = {t.name: t.type for t in topic_types}

        if ROSOUT_TOPIC not in topic_type_map:
            print(f"✗ {bag_path}: topic {ROSOUT_TOPIC} not found in bag")
            return 0

        msg_type = get_message(topic_type_map[ROSOUT_TOPIC])

        csv_file_path = output_file if output_file else os.path.join(os.path.dirname(bag_path), csv_filename)
        record_count = 0

        with open(csv_file_path, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES)
            writer.writeheader()

            while reader.has_next():
                topic, data, timestamp = reader.read_next()
                if topic != ROSOUT_TOPIC:
                    continue

                msg = deserialize_message(data, msg_type)

                if msg.level < min_level:
                    continue

                stamp_sec = msg.stamp.sec + msg.stamp.nanosec / 1_000_000_000.0

                writer.writerow({
                    "timestamp": timestamp / 1_000_000_000.0,
                    "stamp": stamp_sec,
                    "level": msg.level,
                    "level_name": _level_name(msg.level),
                    "name": msg.name,
                    "msg": msg.msg,
                    "file": msg.file,
                    "function": msg.function,
                    "line": msg.line,
                })
                record_count += 1

        if record_count > 0:
            print(f"✓ {csv_file_path}: {record_count} messages")
            return record_count
        else:
            print(f"✗ {bag_path}: No rosout records found (min_level={min_level})")
            return 0

    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        return -2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        help="Input directory path to search for rosbags",
    )
    parser.add_argument(
        "--min-level",
        type=str,
        default="DEBUG",
        choices=list(_LEVEL_BY_NAME.keys()),
        help="Minimum log level to include (default: DEBUG, i.e. all messages)",
    )
    parser.add_argument(
        "--csv-filename",
        type=str,
        default="rosout.csv",
        help="Output CSV file name written next to each rosbag (default: rosout.csv)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=None,
        help="Write CSV to this exact file path. Requires exactly one rosbag to be found. "
             "Overrides --csv-filename.",
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (output/source paths relative to input dir)",
    )

    args = parser.parse_args()
    min_level = _LEVEL_BY_NAME[args.min_level]

    rosbag_paths = find_rosbags(args.input)
    print(f"Searching for rosbags in {args.input}...")
    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    if args.output_file and len(rosbag_paths) != 1:
        print(f"Error: --output-file requires exactly one rosbag, found {len(rosbag_paths)}")
        return 1

    print(f"Found {len(rosbag_paths)} rosbags to process. "
          f"Using {args.workers} parallel workers...")
    print(f"Min log level: {args.min_level} ({min_level})")
    if args.output_file:
        print(f"Output file: {args.output_file}")
    else:
        print(f"Output CSV filename: {args.csv_filename}")

    start = time.time()
    process_args = [
        (bag_path, min_level, args.csv_filename, args.output_file if args.output_file else None)
        for bag_path in rosbag_paths
    ]

    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    input_root = os.path.abspath(args.input)
    total_records = 0
    processed_bags = 0
    failed_bags = 0
    error_bags = 0

    for i, record_count in enumerate(results):
        if record_count == -2:
            error_bags += 1
        elif record_count > 0:
            total_records += record_count
            processed_bags += 1
            if args.provenance_file:
                bag_path = rosbag_paths[i]
                csv_file_path = os.path.join(os.path.dirname(bag_path), args.csv_filename)
                output_rel = os.path.relpath(csv_file_path, input_root)
                source_rel = os.path.relpath(bag_path, input_root)
                write_provenance_entry(
                    args.provenance_file,
                    output_rel,
                    [source_rel],
                    "rosbags_rosout_to_csv",
                    params={"min_level": args.min_level, "csv_filename": args.csv_filename},
                )
        else:
            failed_bags += 1

    elapsed = time.time() - start
    print(f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, "
          f"{error_bags} errors, {failed_bags} failed), "
          f"{total_records} total records, time {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
