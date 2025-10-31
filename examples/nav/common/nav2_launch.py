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

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node


def generate_launch_description():

    # For certain tests we need a modified nav2_bringup
    nav2_bringup_dir = get_package_share_directory('nav2_bringup_mod')

    return LaunchDescription([

        DeclareLaunchArgument(
            'laserscan_random_drop_percentage',
            default_value='0.0',
            description='Percentage of random drops in LaserScan'),

        DeclareLaunchArgument(
            'laserscan_gaussian_noise_std_deviation',
            default_value='0.0',
            description='Standard deviation of Gaussian noise in LaserScan'),

        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([PathJoinSubstitution([nav2_bringup_dir, 'launch', 'tb4_simulation_launch.py'])]),
            launch_arguments={
                'rviz_config_file': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'view.rviz'),
            }.items()
        ),

        Node(
            package="gazebo_tf_publisher",
            name="gazebo_tf_publisher",
            executable="gazebo_tf_publisher_node",
            parameters=[
                {"gz_pose_topic": "/world/default/dynamic_pose/info"},
                {"base_frame_id": "base_link"},
            ],
        ),

        Node(
            package='scenario_status',
            executable='scenario_status_node',
            name='scenario_status',
            output='screen'
        ),

        Node(
            package='gazebo_stats_publisher',
            executable='gazebo_stats_publisher',
            name='gazebo_stats_publisher',
            output='screen'
        ),

        Node(
            package='sys_stats_publisher',
            executable='sys_stats_publisher',
            name='sys_stats_publisher',
            output='screen'
        ),

        Node(
            package='message_modification',
            executable='laserscan_modification',
            name='laserscan_modification',
            output='screen',
            remappings=[
                ('/in', '/scan_sim'),
                ('/out', '/scan')
            ],
            parameters=[
                {'random_drop_percentage': LaunchConfiguration('laserscan_random_drop_percentage'),
                 'gaussian_noise_std_deviation': LaunchConfiguration('laserscan_gaussian_noise_std_deviation')}
            ]
        ),

        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([PathJoinSubstitution([os.path.dirname(os.path.abspath(__file__)), 'monitor_cam_launch.py'])]),
        )
    ])
