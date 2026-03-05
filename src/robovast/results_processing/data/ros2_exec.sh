#!/usr/bin/env bash

# This script sets up the ROS2 environment and executes a Python script
# It is meant to be called from within a Docker container by docker_exec.sh

# Check if arguments are provided
if [ $# -eq 0 ]; then
    echo "Error: No script specified"
    exit 1
fi

# Detect ROS distro by checking /opt/ros/<distro> directories
ros_distros=$(ls /opt/ros 2>/dev/null)
if [ -z "$ros_distros" ]; then
    echo 'Error: No ROS distributions found in /opt/ros.'
    exit 1
fi

# Use the first found distro
ROS_DISTRO=$(echo "$ros_distros" | head -n 1)
echo "Detected ROS distribution: $ROS_DISTRO"

# Source the ROS setup
source "/opt/ros/$ROS_DISTRO/setup.bash"

# Execute the Python script with all arguments
exec python3 "$@"
