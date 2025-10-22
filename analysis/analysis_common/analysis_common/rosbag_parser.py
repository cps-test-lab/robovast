import os

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
from tf2_ros import (Buffer, ConnectivityException, ExtrapolationException,
                     LookupException)
from transforms3d.euler import quat2euler


def get_tf_poses(rosbag_path: str, frame_id: str) -> dict:
    """
    Extract TF poses from a ROS bag

    Args:
        rosbag_path: Path to the ROS bag
        frame_id: The TF frame ID to extract poses for (relative to /map)
    """
    poses = []

    # Check if the directory exists

    if not os.path.exists(rosbag_path):
        print(f"The specified ROS bag path does not exist: {rosbag_path}")
        return []

    # Initialize TF buffer for transform calculations
    tf_buffer = Buffer()

    try:
        # Create reader
        reader = rosbag2_py.SequentialReader()

        storage_options = rosbag2_py.StorageOptions(uri=rosbag_path, storage_id='mcap')
        converter_options = rosbag2_py.ConverterOptions('', '')
        reader.open(storage_options, converter_options)

        # Get topic metadata
        topic_types = reader.get_all_topics_and_types()
        topic_type_map = {topic.name: topic.type for topic in topic_types}

        # Process messages
        while reader.has_next():

            (topic, data, timestamp) = reader.read_next()

            gt_base_link_frame = "nav2_turtlebot4_base_link_gt"

            if topic == "/tf" or topic == "/tf_static":
                try:
                    # Get message type and deserialize
                    msg_type = get_message(topic_type_map[topic])
                    msg = deserialize_message(data, msg_type)

                    timestamp_sec = timestamp * 1e-9  # Convert to seconds

                    for transform in msg.transforms:
                        tf_buffer.set_transform(transform, "default_authority")
                        if transform.child_frame_id == gt_base_link_frame:
                            try:
                                # Look up transform from map to base_link
                                map_to_base_link = tf_buffer.lookup_transform(
                                    "map", gt_base_link_frame,
                                    transform.header.stamp
                                )
                                # Create a record for the calculated groundtruth pose as a Pose message
                                pos = map_to_base_link.transform.translation
                                orient = map_to_base_link.transform.rotation

                                _, _, yaw = quat2euler([orient.w, orient.x, orient.y, orient.z])

                                pose_data = {'timestamp': timestamp_sec, 'x': pos.x, 'y': pos.y, 'yaw': yaw}
                                poses.append(pose_data)
                            except (LookupException, ConnectivityException, ExtrapolationException) as e:
                                # Transform lookup failed, skip this calculation
                                print(f"Error while calculation transform to {gt_base_link_frame}")
                            except Exception as e:
                                print(f"Unexpected error while processing transform: {e}")

                except Exception as e:
                    print(f"Error processing message for topic {topic}: {e}")
                    poses = []

    except Exception as e:
        print(f"Error reading rosbag: {str(e)}")
        poses = []

    return poses
