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
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, ExecuteProcess
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node


def generate_launch_description():

    return LaunchDescription([

        DeclareLaunchArgument(
            'laserscan_random_drop_percentage',
            default_value='0.0',
            description='Percentage of random drops in LaserScan'),

        DeclareLaunchArgument(
            'laserscan_gaussian_noise_std_deviation',
            default_value='0.0',
            description='Standard deviation of Gaussian noise in LaserScan'),

        DeclareLaunchArgument(
            'setup_path',
            default_value='/config/clearpath',  # Changed to mounted config directory
            description='Clearpath setup path'),
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='true',
            description='Use sim time'),

        DeclareLaunchArgument(
            'world',
            default_value='',
            description='World file path'),

        DeclareLaunchArgument(
            'x_pose',
            default_value='-8.00',
            description='Robot X position'),

        DeclareLaunchArgument(
            'y_pose', 
            default_value='0.00',
            description='Robot Y position'),

        DeclareLaunchArgument(
            'yaw',
            default_value='0.00',
            description='Robot yaw orientation'),

        DeclareLaunchArgument(
            'map',
            default_value='',
            description='Map file path'),

        # Launch RViz with custom config
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(['/opt/ros/jazzy/share/nav2_bringup/launch/rviz_launch.py']),
            launch_arguments={
                'namespace': 'w200_0000',
                'use_namespace': 'true',
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'rviz_config': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'view.rviz'),
            }.items()
        ),

        # Launch Gazebo simulation with world generation (headless mode)
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(['/config/files/nav2/gazebo_world_launch.py']),
            launch_arguments={
                'world': LaunchConfiguration('world'),
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'headless': 'True',
            }.items()
        ),

        # Spawn Clearpath robot using official Clearpath launch
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(['/opt/ros/jazzy/share/clearpath_gz/launch/robot_spawn.launch.py']),
            launch_arguments={
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'setup_path': LaunchConfiguration('setup_path'),
                'world': 'default',  # Must match the world in gazebo
                'rviz': 'false',  # We start rviz separately
                'x': LaunchConfiguration('x_pose'),
                'y': LaunchConfiguration('y_pose'),
                'yaw': LaunchConfiguration('yaw'),
                'generate': 'true',
            }.items()
        ),

        # Launch Nav2 using Clearpath's nav2.launch.py with proper w200 config
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([
                get_package_share_directory('clearpath_nav2_demos'), '/launch/nav2.launch.py']),
            launch_arguments={
                'setup_path': LaunchConfiguration('setup_path'),
                'use_sim_time': LaunchConfiguration('use_sim_time'),
            }.items()
        ),

        # Launch SLAM using Clearpath's slam.launch.py
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([
                get_package_share_directory('clearpath_nav2_demos'), '/launch/slam.launch.py']),
            launch_arguments={
                'setup_path': LaunchConfiguration('setup_path'),
                'use_sim_time': LaunchConfiguration('use_sim_time'),
            }.items()
        ),



        # LaserScan modification for noise/drops
        Node(
            package='message_modification',
            executable='laserscan_modification',
            name='laserscan_modification',
            output='screen',
            remappings=[
                ('/in', '/w200_0000/sensors/lidar2d_0/scan'),  # Clearpath lidar topic
                ('/out', '/w200_0000/scan')  # Output with namespace
            ],
            parameters=[
                {'random_drop_percentage': LaunchConfiguration('laserscan_random_drop_percentage'),
                 'gaussian_noise_std_deviation': LaunchConfiguration('laserscan_gaussian_noise_std_deviation')}
            ]
        ),

        # Monitor camera launch
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([PathJoinSubstitution([os.path.dirname(os.path.abspath(__file__)), 'monitor_cam_launch.py'])]),
        )
    ])