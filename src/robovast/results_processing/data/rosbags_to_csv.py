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

"""script that reads ROS2 messages using the rosbag2_py API and writes separate CSV files per topic."""
import argparse
import csv
import os
import sys
import time
from multiprocessing import Pool, cpu_count

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosbags_common import find_rosbags, gen_msg_values, write_provenance_entry
from rosidl_runtime_py.utilities import get_message


def topic_to_filename(topic: str) -> str:
    """Convert a topic name like /foo/bar to foo_bar."""
    return topic.strip("/").replace("/", "_")


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, topics = args
    return process_rosbag(bag_path, topics)


def process_rosbag(bag_path, topics):
    """Process a single rosbag and write one CSV file per requested topic."""
    try:
        # records_by_topic: dict[topic -> list[record]]
        records_by_topic: dict = {t: [] for t in topics}

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
            if topic not in records_by_topic:
                continue
            msg_type = get_message(typename(topic))
            msg = deserialize_message(data, msg_type)
            fields = dict(gen_msg_values(msg))
            record = {
                "timestamp": timestamp,
                "type": type(msg).__name__,
                **fields,
            }
            records_by_topic[topic].append(record)

        parent_folder = os.path.abspath(os.path.dirname(bag_path))
        bag_name = os.path.basename(bag_path)
        base_fields = ["timestamp", "type"]

        output_files = []
        total_records = 0
        for topic, records in records_by_topic.items():
            if not records:
                print(f"  ✗ {bag_path} [{topic}]: no messages")
                continue

            fieldnames_set: set = set()
            for record in records:
                fieldnames_set.update(record.keys())
            other_fields = sorted(fieldnames_set - set(base_fields))
            fieldnames = base_fields + other_fields

            output_file = os.path.join(
                parent_folder,
                f"{bag_name}_{topic_to_filename(topic)}.csv",
            )
            with open(output_file, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for record in records:
                    writer.writerow(record)

            print(f"  ✓ {output_file}: {len(records)} messages")
            total_records += len(records)
            output_files.append(output_file)

        return total_records, output_files
    except Exception as e:
        print(f"✗ {bag_path}: Error - {str(e)}")
        return -2, []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        default=[],
        required=True,
        help="Topic to extract (can be specified multiple times)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=cpu_count(),
        help=f"Number of parallel workers (default: {cpu_count()})",
    )
    parser.add_argument(
        "input",
        help="input directory path to search for rosbags",
    )
    parser.add_argument(
        "--provenance-file",
        default=None,
        help="Write provenance JSON to this path (output/source paths relative to input dir)",
    )

    args = parser.parse_args()
    topics = list(dict.fromkeys(args.topics))  # deduplicate, preserve order

    # Find all rosbags in subdirectories
    rosbag_paths = find_rosbags(args.input)

    if not rosbag_paths:
        print(f"No rosbags found in {args.input}")
        return 0

    print(
        f"Found {len(rosbag_paths)} rosbags to process, "
        f"{len(topics)} topic(s): {topics}. "
        f"Using {args.workers} parallel workers..."
    )

    start = time.time()
    total_records = 0
    processed_bags = 0

    process_args = [(bag_path, topics) for bag_path in rosbag_paths]

    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except KeyboardInterrupt:
        print("Processing interrupted by user.")
        return 1

    input_root = os.path.abspath(args.input)
    failed_bags = 0
    error_bags = 0
    for i, (records_count, output_files) in enumerate(results):
        if records_count == -2:
            error_bags += 1
        elif records_count > 0:
            total_records += records_count
            processed_bags += 1
            if args.provenance_file:
                bag_path = rosbag_paths[i]
                source_rel = os.path.relpath(bag_path, input_root)
                for output_file in output_files:
                    output_rel = os.path.relpath(output_file, input_root)
                    write_provenance_entry(
                        args.provenance_file,
                        output_rel,
                        [source_rel],
                        "rosbags_to_csv",
                        params={"topics": topics},
                    )
        else:
            failed_bags += 1

    elapsed = time.time() - start
    print(
        f"Summary: {len(rosbag_paths)} rosbags ({processed_bags} success, "
        f"{error_bags} errors, {failed_bags} failed), "
        f"{total_records} total records, time {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
