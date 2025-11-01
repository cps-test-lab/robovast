import pandas as pd
from pathlib import Path
from rosbag2_py import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
from tf2_ros.buffer import Buffer
from tf2_ros.transform_exceptions import LookupException, ConnectivityException, ExtrapolationException

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
        
def process_rosbag(bag_path, skipped_topics, output_dir):
    """Process a single rosbag and save to CSV in the output directory."""
    records = []
    append = records.append  # Local variable for faster access

    # Initialize TF buffer for transform calculations
    tf_buffer = Buffer()

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

        while reader.has_next():
            topic, data, timestamp = reader.read_next()
            msg_type = get_message(typename(topic))
            msg = deserialize_message(data, msg_type)

            # Add transform messages to TF buffer for later lookup
            if topic == "/tf" or topic == "/tf_static":
                if hasattr(msg, 'transforms'):
                    for transform in msg.transforms:
                        tf_buffer.set_transform(transform, "default_authority")

            if topic not in skipped_topics:
                fields = dict(gen_msg_values(msg))
                record = {
                    "timestamp": timestamp,
                    "topic": topic,
                    "type": type(msg).__name__,
                    **fields
                }
                append(record)
                
                gt_base_link_frame = "nav2_turtlebot4_base_link_gt"
                # If this is a TF message containing base_link transform, calculate map->base_link
                if (topic == "/tf" or topic == "/tf_static"):
                    for transform in msg.transforms:
                        if transform.child_frame_id == gt_base_link_frame:
                            try:                                
                                # Look up transform from map to base_link
                                map_to_base_link = tf_buffer.lookup_transform(
                                    "map", gt_base_link_frame, 
                                    transform.header.stamp
                                )
                                
                                # Create a record for the calculated groundtruth pose as a Pose message
                                translation = map_to_base_link.transform.translation
                                rotation = map_to_base_link.transform.rotation
                                pose_fields = {
                                    "position.x": translation.x,
                                    "position.y": translation.y,
                                    "position.z": translation.z,
                                    "orientation.x": rotation.x,
                                    "orientation.y": rotation.y,
                                    "orientation.z": rotation.z,
                                    "orientation.w": rotation.w,
                                }
                                transform_record = {
                                    "timestamp": timestamp,
                                    "topic": "/groundtruth_pose",
                                    "type": "Pose",
                                    **pose_fields
                                }
                                append(transform_record)
                                
                            except (LookupException, ConnectivityException, ExtrapolationException) as e:
                                # Transform lookup failed, skip this calculation
                                print(f"Error while calculation transform to {gt_base_link_frame}")
                                pass

        # if records:
        #     # Use only the immediate parent folder name for the CSV filename
        #     bag_path_obj = Path(bag_path)
        #     parent_folder = bag_path_obj.parent.name
        #     csv_filename = parent_folder + '.csv'
        #     output_file = Path(output_dir) / csv_filename

        #     # Ensure output directory exists
        #     output_file.parent.mkdir(parents=True, exist_ok=True)

        #     df = pd.DataFrame.from_records(records)
        #     df.to_csv(output_file, index=False)
        #     print(f"✓ {csv_filename}: {len(records)} messages")
        #     return len(records)
        # else:
        #     print(f"✗ {Path(bag_path).name}: No records found")
        #     return 0
    
    except Exception as e:
        print(f"✗ {Path(bag_path).name}: Error - {str(e)[:50]}...")
        return 0
