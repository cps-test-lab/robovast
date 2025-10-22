#!/usr/bin/env python3
"""
Rosbag Parser - A module for parsing rosbag2 files and extracting relevant data.
"""

import os
import pickle
import threading
from pathlib import Path

import pandas as pd
from tf2_py import (ConnectivityException, ExtrapolationException,
                    LookupException)
from tf2_ros import Buffer

from .worker_thread import CancellableWorkload

try:
    QT_SUPPORT = True
except ImportError:
    print("Error: PySide2 is required for rosbag_parser.py")
    QT_SUPPORT = False

import threading

import rosbag2_py
from rclpy.serialization import deserialize_message
from robovast_common import FileCache
from rosidl_runtime_py.utilities import get_message


class RosbagParser:
    """Main class for parsing rosbag2 files"""

    def __init__(self, rosbag_path=None):
        self.rosbag_path = rosbag_path
        self.cancel_event = threading.Event()

    def _find_rosbag_path(self, directory_path):
        """Find rosbag2 database file in the directory"""
        possible_paths = [
            directory_path / "rosbag2",
            directory_path / "bag",
            directory_path
        ]

        for path in possible_paths:
            if path.exists():
                # Look for sqlite3 database files
                for file in path.rglob("*.db3"):
                    return file.parent
                # Look for metadata.yaml
                for file in path.rglob("metadata.yaml"):
                    return file.parent

        return None

    def parse_topics(self, topics):
        """Parse the actual rosbag2 file"""
        if not Path(self.rosbag_path).exists():
            raise FileNotFoundError(f"Rosbag path does not exist: {self.rosbag_path}")

        # Initialize TF buffer for transform calculations
        tf_buffer = Buffer()

        result = {topic: {'timestamps': [], 'values': []} for topic in topics}
        result["/groundtruth_pose"] = {'timestamps': [], 'values': []}  # For ground truth pose
        try:
            # Create reader
            reader = rosbag2_py.SequentialReader()

            storage_options = rosbag2_py.StorageOptions(uri=str(self.rosbag_path), storage_id='mcap')
            converter_options = rosbag2_py.ConverterOptions('', '')
            reader.open(storage_options, converter_options)

            # Get topic metadata
            topic_types = reader.get_all_topics_and_types()
            topic_type_map = {topic.name: topic.type for topic in topic_types}

            # Process messages
            while reader.has_next():
                if self.cancel_event.is_set():
                    return None
                (topic, data, timestamp) = reader.read_next()

                gt_base_link_frame = "nav2_turtlebot4_base_link_gt"

                if topic in topics:
                    try:
                        # Get message type and deserialize
                        msg_type = get_message(topic_type_map[topic])
                        msg = deserialize_message(data, msg_type)

                        timestamp_sec = timestamp * 1e-9  # Convert to seconds

                        # Extract value based on topic
                        if topic == "/gazebo/real_time_factor":
                            if hasattr(msg, 'data'):
                                value = msg.data
                            else:
                                value = float(msg)
                            result[topic]['timestamps'].append(timestamp_sec)
                            result[topic]['values'].append(value)

                        elif (topic == "/tf" or topic == "/tf_static"):
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

                                        pose_data = {
                                            'position': {
                                                'x': pos.x,
                                                'y': pos.y,
                                                'z': pos.z
                                            },
                                            'orientation': {
                                                'x': orient.x,
                                                'y': orient.y,
                                                'z': orient.z,
                                                'w': orient.w
                                            }
                                        }
                                        result["/groundtruth_pose"]['timestamps'].append(timestamp_sec)
                                        result["/groundtruth_pose"]['values'].append(pose_data)
                                    except (LookupException, ConnectivityException, ExtrapolationException) as e:
                                        # Transform lookup failed, skip this calculation
                                        print(f"Error while calculation transform to {gt_base_link_frame}")
                                    except Exception as e:
                                        print(f"Unexpected error while processing transform: {e}")
                        else:
                            # Generic value extraction
                            if hasattr(msg, 'data'):
                                value = msg.data
                            elif hasattr(msg, 'value'):
                                value = msg.value
                            else:
                                value = 0.0
                            result[topic]['timestamps'].append(timestamp_sec)
                            result[topic]['values'].append(value)

                    except Exception as e:
                        print(f"Error processing message for topic {topic}: {e}")

        except Exception as e:
            raise RuntimeError(f"Error reading rosbag: {str(e)}")

        self.save_csv(result)

        return result

    def save_csv(self, result):
        # Create CSV file in test_directory with result array content
        test_directory = Path(self.rosbag_path).parent

        # Prepare data for CSV with timestamp and flattened value columns
        csv_data = []

        def flatten_dict(d, parent_key='', sep='_'):
            """Flatten a nested dictionary"""
            items = []
            for k, v in d.items():
                new_key = f"{parent_key}{sep}{k}" if parent_key else k
                if isinstance(v, dict):
                    items.extend(flatten_dict(v, new_key, sep=sep).items())
                else:
                    items.append((new_key, v))
            return dict(items)

        for topic, data in result.items():
            if self.cancel_event.is_set():
                return None
            timestamps = data['timestamps']
            values = data['values']
            for i, timestamp in enumerate(timestamps):
                value = values[i] if i < len(values) else None

                # Start with basic row data
                row_data = {
                    'timestamp': timestamp,
                    'topic': topic
                }

                # Handle different value types
                if isinstance(value, dict):
                    # Flatten dictionary values into separate columns
                    flattened = flatten_dict(value)
                    row_data.update(flattened)
                elif value is not None:
                    # Simple values go in a 'value' column
                    row_data['value'] = value

                csv_data.append(row_data)

        if csv_data:
            # Create DataFrame and save to CSV
            df = pd.DataFrame(csv_data)
            csv_filename = test_directory / f"rosbag2.csv"
            df.to_csv(csv_filename, index=False)
            print(f"CSV file created: {csv_filename}")

    def calculate_motion_derivatives(self, pose_data):
        """Calculate speed and yaw rate from pose data"""
        if not pose_data or len(pose_data['timestamps']) < 2:
            return {'speed': {'timestamps': [], 'values': []},
                    'yaw_rate': {'timestamps': [], 'values': []}}

        timestamps = pose_data['timestamps']
        poses = pose_data['values']

        speeds = {'timestamps': [], 'values': []}
        yaw_rates = {'timestamps': [], 'values': []}

        def quat_to_yaw(qx, qy, qz, qw):
            import math
            return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))

        for i in range(1, len(timestamps)):
            dt = timestamps[i] - timestamps[i-1]
            if dt <= 0:
                continue

            # Calculate speed (magnitude of velocity in x/y plane)
            dx = poses[i]['position']['x'] - poses[i-1]['position']['x']
            dy = poses[i]['position']['y'] - poses[i-1]['position']['y']
            speed = (dx**2 + dy**2)**0.5 / dt

            # Calculate yaw rate
            current_yaw = quat_to_yaw(poses[i]['orientation']['x'], poses[i]['orientation']['y'],
                                      poses[i]['orientation']['z'], poses[i]['orientation']['w'])
            prev_yaw = quat_to_yaw(poses[i-1]['orientation']['x'], poses[i-1]['orientation']['y'],
                                   poses[i-1]['orientation']['z'], poses[i-1]['orientation']['w'])

            # Handle angle wrap-around
            dyaw = current_yaw - prev_yaw
            if dyaw > 3.14159:
                dyaw -= 2 * 3.14159
            elif dyaw < -3.14159:
                dyaw += 2 * 3.14159
            yaw_rate = dyaw / dt

            # Store calculated values
            speeds['timestamps'].append(timestamps[i])
            speeds['values'].append(speed)
            yaw_rates['timestamps'].append(timestamps[i])
            yaw_rates['values'].append(yaw_rate)

        return {'speed': speeds, 'yaw_rate': yaw_rates}


class RosbagAnalyzerWorker(CancellableWorkload):

    def __init__(self, topics):
        super().__init__('RosbagAnalyzer')
        self.file_cache = FileCache()
        self.parser = None
        self.topics = topics

    def cancel(self):
        """Cancel the calculation"""
        if self.parser:
            self.parser.cancel_event.set()
        super().cancel()

    def run(self, rosbag_path, run_type):
        rosbag_path = os.path.join(rosbag_path, "rosbag2")
        print(f"RosbagAnalyzerWorker started on: {rosbag_path}")

        self.file_cache.set_current_data_directory(Path(rosbag_path).parent)

        # Parse topics
        self.progress_callback(10, f"Processing rosbag data ...")
        if self.is_cancelled():
            return False, None

        overview_data = {}  # Store accumulated data
        # try to get cached file
        files_for_hash = [str(file_name.relative_to(rosbag_path)) for file_name in Path(rosbag_path).glob("*") if file_name.is_file()]
        cached_file = self.file_cache.get_cached_file(files_for_hash, "rosbag_data.pkl", binary=True, strings_for_hash=self.topics)
        if cached_file:
            try:
                # Unpickle from bytes returned by get_cached_file
                overview_data = pickle.loads(cached_file)
                print(f"Loaded rosbag from cache.")
                self.progress_callback(100, f"Processing rosbag data completed.")
                return True, overview_data
            except Exception as e:
                print(f"Error loading cached file: {e}")

        if self.is_cancelled():
            return False, None

        self.parser = RosbagParser(rosbag_path)
        print(f"Starting analysis for rosbag: {rosbag_path}")

        topic_data = self.parser.parse_topics(self.topics)

        self.progress_callback(20, f"Processing rosbag data ...")
        if self.is_cancelled():
            return False, None

        # Process each topic
        for topic, data in topic_data.items():
            if self.is_cancelled():
                return False, None

            if topic == "/gazebo/real_time_factor":
                # Direct emission for simple topics
                overview_data['/gazebo/real_time_factor'] = {
                    'timestamps': data['timestamps'],
                    'values': data['values']
                }

            elif topic == "/groundtruth_pose":
                overview_data['/groundtruth_pose'] = {
                    'timestamps': data['timestamps'],
                    'values': data['values']
                }
                # Calculate derivatives
                derivatives = self.parser.calculate_motion_derivatives(data)

                # Emit speed and yaw rate
                if derivatives['speed']['timestamps']:
                    overview_data['/groundtruth_pose_speed'] = {
                        'timestamps': derivatives['speed']['timestamps'],
                        'values': derivatives['speed']['values']
                    }
                if derivatives['yaw_rate']['timestamps']:
                    overview_data['/groundtruth_pose_yaw_rate'] = {
                        'timestamps': derivatives['yaw_rate']['timestamps'],
                        'values': derivatives['yaw_rate']['values']
                    }

            else:
                # Direct emission for other topics
                overview_data[topic] = {
                    'timestamps': data['timestamps'],
                    'values': data['values']
                }

        self.progress_callback(90, f"Processing rosbag data ...")

        if self.is_cancelled():
            return False, None

        # Save the data to a pickle file
        print("Saving parsed data to cache...")
        self.file_cache.save_file_to_cache(files_for_hash, "rosbag_data.pkl", pickle.dumps(
            overview_data), binary=True, strings_for_hash=self.topics)

        self.progress_callback(100, f"Processing rosbag data completed.")
        print("Rosbag analysis completed successfully.")
        return True, overview_data
