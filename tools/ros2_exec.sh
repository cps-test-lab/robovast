#!/bin/bash

# Detect ROS distro by checking /opt/ros/<distro> directories
ros_distros=$(ls /opt/ros 2>/dev/null)
if [ -z "$ros_distros" ]; then
    echo "Error: No ROS distributions found in /opt/ros."
    exit 1
fi

# Use the first found distro
ROS_DISTRO=$(echo "$ros_distros" | head -n 1)
echo "Detected ROS distribution: $ROS_DISTRO"

SCRIPT_DIR="$(dirname "$0")"
echo "Script directory: $SCRIPT_DIR"

source "/opt/ros/$ROS_DISTRO/setup.bash"
exec python3 "$SCRIPT_DIR/$@"