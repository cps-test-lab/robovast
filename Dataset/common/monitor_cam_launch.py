import os

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, IncludeLaunchDescription,
                            OpaqueFunction)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution


def calculate_map_center_and_dimensions(map_file_path):
    """
    Calculate the center coordinates and dimensions of the map based on the map YAML file.

    Args:
        map_file_path: Path to the map YAML file

    Returns:
        tuple: (center_x, center_y, map_width_meters, map_height_meters)
    """
    try:
        with open(map_file_path, 'r') as f:
            map_data = yaml.safe_load(f)

        # Get map metadata
        resolution = map_data.get('resolution', 0.05)  # Default resolution
        origin = map_data.get('origin', [0.0, 0.0, 0.0])  # Default origin
        image_file = map_data.get('image', 'map.pgm')

        # Calculate the full path to the image file
        map_dir = os.path.dirname(map_file_path)
        image_path = os.path.join(map_dir, image_file)

        # Read the PGM file to get image dimensions
        if os.path.exists(image_path):
            with open(image_path, 'rb') as img_file:
                # Read PGM header
                magic = img_file.readline().decode().strip()

                # Skip comments
                line = img_file.readline().decode().strip()
                while line.startswith('#'):
                    line = img_file.readline().decode().strip()

                # Parse width and height
                if magic == 'P5':  # Binary PGM
                    width, height = map(int, line.split())

                    # Calculate map center and dimensions
                    map_width_meters = width * resolution
                    map_height_meters = height * resolution

                    center_x = origin[0] + map_width_meters / 2.0
                    center_y = origin[1] + map_height_meters / 2.0

                    return (center_x, center_y, map_width_meters, map_height_meters)
                else:
                    print(f"Unsupported PGM format: {magic}")
                    return (0.0, 0.0, 10.0, 10.0)
        else:
            print(f"Map image file not found: {image_path}")
            return (0.0, 0.0, 10.0, 10.0)

    except Exception as e:
        print(f"Error calculating map center: {e}")
        return (0.0, 0.0, 10.0, 10.0)


def launch_setup(context, *args, **kwargs):
    """Setup function to calculate map center and configure camera position"""

    gazebo_static_camera_dir = get_package_share_directory('gazebo_static_camera')

    # Get the map configuration
    map_config = LaunchConfiguration('map').perform(context)

    # Calculate map center and dimensions
    if map_config and os.path.exists(map_config):
        center_x, center_y, map_width, map_height = calculate_map_center_and_dimensions(map_config)

        # Calculate camera height to see the whole map
        # Using the actual camera horizontal FOV of 1.047 radians (~60°)
        # For a top-down view with pitch=1.57 (90°), we use the horizontal FOV
        # Height = (max_dimension / 2) / tan(FOV/2)
        # Adding margin for better visibility
        import math
        horizontal_fov = 1.047  # radians, from camera.sdf.xacro
        max_dimension = max(map_width, map_height)

        # Calculate minimum height needed to see the whole map
        min_height = (max_dimension / 2) / math.tan(horizontal_fov / 2)

        # Add 30% margin for better visibility and ensure minimum height
        camera_height = max(5.0, min_height * 1.3)

        print(f"Calculated map center: x={center_x:.2f}, y={center_y:.2f}")
        print(f"Map dimensions: {map_width:.2f}x{map_height:.2f}m, camera height: {camera_height:.2f}m")
    else:
        # Default center and height if map file not found
        center_x, center_y = 0.0, 0.0
        camera_height = 16.0
        print(f"Using default camera position: x={center_x}, y={center_y}, z={camera_height}")

    return [
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource([PathJoinSubstitution([gazebo_static_camera_dir, 'launch', 'spawn_static_camera_launch.py'])]),
            launch_arguments={
                'x': str(center_x),
                'y': str(center_y),
                'z': str(camera_height),
                'pitch': '1.57',
                'yaw': '1.57',
                'update_rate': '1.0',
                'image_width': '640',
                'image_height': '640'
            }.items()
        ),
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument(
            'map',
            default_value='',
            description='Full path to map yaml file to load (required for camera center calculation)'),

        OpaqueFunction(function=launch_setup)
    ])
