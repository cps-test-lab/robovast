# Alternative Clearpath Integration Instructions

If the automated launch file integration doesn't work due to package naming issues, here's a manual approach:

## Step-by-Step Manual Integration

### 1. First, verify Clearpath packages are installed:
```bash
# In the container, check if packages exist:
ros2 pkg list | grep clearpath
```

### 2. If packages are missing, install them manually:
```bash
apt update
apt install ros-jazzy-clearpath-* -y
```

### 3. Test Clearpath simulation manually:
```bash
# Start simulation
ros2 launch clearpath_gz simulation.launch.py setup_path:=/etc/clearpath

# In another terminal, start nav2
ros2 launch clearpath_nav2_demos nav2.launch.py setup_path:=/etc/clearpath use_sim_time:=true

# In another terminal, start SLAM
ros2 launch clearpath_nav2_demos slam.launch.py setup_path:=/etc/clearpath use_sim_time:=true
```

### 4. Alternative launch file approach:

If the package names are different, update `clearpath_w200_launch.py`:

```python
# Replace the problematic includes with direct package paths:
# Instead of get_package_share_directory('clearpath_gz')
# Try get_package_share_directory('clearpath_simulator_gz')
# Or check what packages are actually available
```

### 5. Fallback: Use individual ROS2 launch commands

You can modify the scenario.osc to launch components separately:

```osc
# Instead of one combined launch file, use multiple ros_launch calls:
ros_launch('clearpath_gz', 'simulation.launch.py', [...])
ros_launch('clearpath_nav2_demos', 'nav2.launch.py', [...])  
ros_launch('clearpath_nav2_demos', 'slam.launch.py', [...])
```

### 6. Topic remapping for compatibility

If topics don't match exactly, add topic remapping in the launch file:

```python
# Add remapping nodes to bridge topics
Node(
    package='topic_tools',
    executable='relay',
    name='map_relay',
    arguments=['/w200_0000/map', '/map']
)
```

This ensures backward compatibility with RoboVAST's expected topic names.