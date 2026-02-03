# Clearpath Warthog Integration with RoboVAST

This document outlines the steps taken to integrate the Clearpath Warthog robot into the RoboVAST simulation framework.

## Changes Made

### 1. Docker Container Updates

**File: `container/Dockerfile`**
- Added Clearpath ROS2 packages:
  - `ros-jazzy-clearpath-simulator`
  - `ros-jazzy-clearpath-nav2-demos`
- Added robot configuration file copy:
  - `COPY clearpath_nav2_demos/config/w200/robot.yaml /etc/clearpath/robot.yaml`

### 2. New Launch File

**File: `configs/navigation/files/clearpath_w200_launch.py`**
- Integrated Clearpath simulation, nav2, and SLAM launch files
- Configured proper namespacing for Warthog robot (`w200_0000`)
- Maintained compatibility with RoboVAST's noise injection and monitoring systems
- Key features:
  - Uses Clearpath's simulation.launch.py for Gazebo simulation
  - Uses Clearpath's nav2.launch.py and slam.launch.py
  - Adapts TF publisher and laser scan topics for Clearpath namespace

### 3. Scenario Configuration Updates

**File: `configs/navigation/scenario.osc`**
- Updated launch file reference to use `clearpath_w200_launch.py`
- Added `setup_path` parameter for Clearpath configuration
- Updated bag recording topics for Clearpath namespace:
  - `/w200_0000/map`
  - `/w200_0000/local_costmap/costmap`
  - `/w200_0000/amcl_pose`
  - `/w200_0000/platform/odom`

## Robot Configuration

The Warthog robot is configured via `/etc/clearpath/robot.yaml` with:
- **Namespace**: `w200_0000` 
- **Platform**: W200 (Warthog)
- **Sensors**: Two Hokuyo UST lidars on vertical mounts
- **IP Configuration**: Robot IP 192.168.131.1

## How It Works

1. **Simulation**: Clearpath's simulation.launch.py starts Gazebo with the Warthog robot
2. **Navigation**: Clearpath's nav2.launch.py starts Nav2 stack with proper configuration
3. **Localization**: Clearpath's slam.launch.py provides SLAM/localization
4. **Robot Actions**: 
   - `robot.init_nav2(start_pose)` initializes navigation with the starting pose
   - `robot.nav_through_poses(goal_poses)` sends navigation goals to the robot

## Key Differences from TurtleBot

1. **Namespace**: All topics are under `/w200_0000/` namespace instead of root
2. **Odometry**: Uses `/w200_0000/platform/odom` instead of `/odom`
3. **Laser Scan**: Uses `/w200_0000/sensors/lidar2d_0/scan`
4. **Configuration**: Robot configuration comes from `/etc/clearpath/robot.yaml`

## Benefits of This Integration

- **Real Robot Compatibility**: The Warthog simulation closely matches the real robot
- **Professional Navigation Stack**: Uses Clearpath's tested Nav2 configurations
- **Proper Namespacing**: Supports multi-robot scenarios
- **Industry Standard**: Warthog is a widely used outdoor mobile robot platform
- **Sensor Fusion**: Supports multiple lidars and sensors out of the box

## Testing the Integration

To test the integration:

1. Build the updated Docker container with Clearpath packages
2. Run a RoboVAST navigation scenario
3. Verify the Warthog robot appears in simulation
4. Check that navigation goals are executed successfully
5. Ensure bag files are recorded with correct Clearpath topics

The robot should behave as a differential drive robot within RoboVAST's testing framework while using the more realistic Warthog dynamics and sensor configurations.