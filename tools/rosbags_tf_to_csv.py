#!/usr/bin/env python3

"""script that get positions in map frame from ROS2 bags tf-messages."""
import argparse
import os
import sys
from pathlib import Path
from multiprocessing import Pool, cpu_count
import math
import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
from tf2_ros import Buffer
from tf2_py import LookupException, ConnectivityException, ExtrapolationException
import csv

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
    for root, dirs, files in os.walk(directory):
        # Check if this directory contains .mcap files or metadata.yaml (rosbag indicators)
        has_mcap = any(f.endswith('.mcap') for f in files)
        has_metadata = 'metadata.yaml' in files
        
        if has_mcap or has_metadata:
            rosbag_dirs.append(root)
    
    return rosbag_dirs


def process_rosbag_wrapper(args):
    """Wrapper function for multiprocessing that unpacks arguments."""
    bag_path, frame = args
    return process_rosbag(bag_path, frame)


def quat_to_rpy(x, y, z, w):
    """Convert quaternion (x, y, z, w) to roll, pitch, yaw in radians."""
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(2 * (w * y - z * x))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll, pitch, yaw

def process_rosbag(bag_path, frame="base_link"):
    """Process a single rosbag and save to CSV in the output directory."""
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
    
    parent_folder = os.path.abspath(os.path.dirname(bag_path))
    output_file = os.path.join(parent_folder, frame + '.csv')
    record_count = 0
    found_tfs = set()
    
    # Open CSV file once and write records directly
    with open(output_file, 'w', newline='') as csvfile:
        fieldnames = ["frame", "timestamp", "position.x", "position.y", "position.z", 
                      "orientation.roll", "orientation.pitch", "orientation.yaw"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        while reader.has_next():
            topic, data, timestamp = reader.read_next()
            msg_type = get_message(typename(topic))
            msg = deserialize_message(data, msg_type)

            # Add transform messages to TF buffer for later lookup
            if topic == "/tf" or topic == "/tf_static" and hasattr(msg, 'transforms'):
                for transform in msg.transforms:
                    tf_buffer.set_transform(transform, "default_authority")
                    found_tfs.add(f"{transform.header.frame_id} -> {transform.child_frame_id}")
                    if transform.child_frame_id == frame:
                        try:                                
                            # Look up transform from map to base_link
                            map_to_base_link = tf_buffer.lookup_transform(
                                "map", frame, 
                                transform.header.stamp
                            )
                            
                            # Extract pose data
                            translation = map_to_base_link.transform.translation
                            rotation = map_to_base_link.transform.rotation

                            roll, pitch, yaw = quat_to_rpy(rotation.x, rotation.y, rotation.z, rotation.w)

                            # Write directly to CSV
                            writer.writerow({
                                "frame": frame,
                                "timestamp": timestamp,
                                "position.x": translation.x,
                                "position.y": translation.y,
                                "position.z": translation.z,
                                "orientation.roll": roll,
                                "orientation.pitch": pitch,
                                "orientation.yaw": yaw,
                            })
                            record_count += 1
                            
                        except (LookupException, ConnectivityException, ExtrapolationException) as e:
                            pass

    if record_count > 0:
        print(f"✓ {output_file}: {record_count} messages")
        return record_count
    else:
        print(f"✗ {Path(bag_path).name}: No records found")
        print(f"  Found TF frames: \n{"\n - ".join(found_tfs)}")
        os.remove(output_file)  # Clean up empty file
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
        "--frame",
        type=str,
        default="base_link",
        help="Target frame name (default: base_link)"
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
        return
    
    print(f"Found {len(rosbag_paths)} rosbags to process:")
    for path in rosbag_paths:
        print(f"  - {path}")
    print(f"Using {args.workers} parallel workers")
    print(f"Target frame: {args.frame}")
    print()
    
    start = time.time()
    total_records = 0
    processed_bags = 0
    
    # Prepare arguments for parallel processing
    process_args = []
    for bag_path in rosbag_paths:
        process_args.append((bag_path, args.frame))

    # Process rosbags in parallel
    print("Processing rosbags...")
    try:
        with Pool(processes=args.workers) as pool:
            results = pool.map(process_rosbag_wrapper, process_args)
    except Exception as e:
        print(f"✗ Error during rosbag processing: {str(e)}")
        sys.exit(1)

    print()  # Add a blank line after all processing output
    
    success = True

    # Calculate summary statistics
    for records_count in results:
        total_records += records_count
        if records_count > 0:
            processed_bags += 1
        else:
            success = False

    elapsed = time.time() - start
    print(f"\nSummary:")
    print(f"Processed {processed_bags}/{len(rosbag_paths)} rosbags successfully")
    print(f"Total records: {total_records}")
    print(f"Total time: {elapsed:.2f} seconds")
    if elapsed > 0:
        print(f"Average processing rate: {total_records/elapsed:.0f} records/second")

    if not success:
        print("Some rosbags failed to process correctly.")
        sys.exit(1)

if __name__ == "__main__":
    # Required for multiprocessing on Windows and some Unix systems
    sys.exit(main())
