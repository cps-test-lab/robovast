# Testing Clearpath Integration

## Build and Test Instructions

### 1. Build the Updated Docker Image

```bash
cd /home/sam/RoboVAST/robovast
./container/build.sh --project localhost:5000
```

### 2. Test Navigation Scenario

```bash
# Activate virtual environment
source venv/bin/activate

# Run a navigation test
cd configs/navigation
vast run navigation_variation.vast
```

### 3. Verify Integration

Watch for these indicators of successful integration:

#### In the logs, you should see:
- Clearpath simulation starting: "Starting Clearpath Gazebo simulation..."
- Robot namespace: Messages referencing `/w200_0000/`
- Navigation stack: "Nav2 lifecycle nodes starting..."
- SLAM: "SLAM toolbox initialized..."

#### In the simulation:
- Warthog robot model appears (not TurtleBot)
- Robot moves according to navigation commands
- Map building occurs during SLAM

#### In recorded bag files:
- Topics include `/w200_0000/sensors/lidar2d_0/scan`
- Odometry from `/w200_0000/platform/odom`
- Navigation goals on `/w200_0000/navigate_through_poses/goal`

### 4. Common Issues and Solutions

#### Issue: "Package clearpath_gz not found"
**Solution**: The package might have a different name. Check available packages:
```bash
ros2 pkg list | grep clearpath
```

#### Issue: Robot doesn't appear in simulation
**Solution**: Check robot.yaml file is correctly copied:
```bash
docker run --rm -it localhost:5000/robovast-jazzy:latest cat /etc/clearpath/robot.yaml
```

#### Issue: Navigation commands not working
**Solution**: Verify namespace in robot actions. The `differential_drive_robot` should automatically handle the namespace.

#### Issue: Topics not recorded in bag files
**Solution**: Check topic names match the updated scenario.osc configuration.

### 5. Manual Testing Steps

If automated testing fails, you can test components individually:

```bash
# 1. Start container
docker run -it localhost:5000/robovast-jazzy:latest /bin/bash

# 2. Test Clearpath packages
ros2 launch clearpath_nav2_demos nav2.launch.py setup_path:=/etc/clearpath --help

# 3. Check robot configuration
cat /etc/clearpath/robot.yaml

# 4. Verify namespace
ros2 topic list | grep w200_0000
```

### 6. Performance Verification

Compare performance with TurtleBot:
- **Navigation accuracy**: Should be similar or better due to better sensor setup
- **Path planning**: May be different due to different robot dimensions
- **Computation load**: Might be higher due to more complex robot model

### 7. Expected Behavior Changes

When switching from TurtleBot to Warthog:
- **Size**: Warthog is larger, may require wider paths
- **Speed**: Different velocity limits
- **Sensors**: Multiple lidars provide better coverage
- **Topics**: All under `/w200_0000/` namespace

### 8. Success Criteria

The integration is successful when:
1. ✅ Docker image builds without errors
2. ✅ RoboVAST scenarios run with Warthog robot
3. ✅ Navigation goals are executed successfully  
4. ✅ Bag files contain Clearpath-namespaced topics
5. ✅ SLAM/localization works properly
6. ✅ Robot respects path planning and obstacle avoidance

### 9. Rollback Plan

If integration fails, you can revert by:
1. Changing `clearpath_w200_launch.py` back to `nav2_launch.py` in scenario.osc
2. Rebuilding container without Clearpath packages
3. Using original TurtleBot configuration

This ensures minimal disruption to existing RoboVAST functionality.