# Path Variation Configuration Guide

This document describes the configuration options for RoboVAST path variation components, which generate navigation scenarios with different start and goal poses.

## Overview

RoboVAST provides two path variation types:

- **PathVariationRandom**: Generates random paths with configurable waypoints
- **PathVariationRasterized**: Generates systematic path coverage using a grid-based approach

## Common Configuration Elements

### PoseConfig

Represents a 2D pose with position and orientation:

```yaml
x: 1.5        # X coordinate in meters
y: 2.0        # Y coordinate in meters  
yaw: 0.785    # Orientation in radians
```

### Parameter References

Both variation types support parameter references using `@` syntax to reference scenario parameters:

```yaml
start_pose: "@start_pose"     # References a scenario parameter named "start_pose"
goal_poses: "@goal_poses"     # References a scenario parameter named "goal_poses"
```

## PathVariationRandom

Generates random navigation paths with multiple waypoints between start and goal poses.

### Random Configuration Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_pose` | `str \| PoseConfig` | ✅ | - | Start position (parameter reference or direct pose) |
| `goal_poses` | `str \| list[dict] \| list[PoseConfig]` | ✅ | - | Goal positions (parameter reference or pose list) |
| `num_goal_poses` | `int` | ❌ | `None` | Number of goal poses to generate (auto-detected if not specified) |
| `map_file` | `str` | ❌ | `None` | Path to map file (uses scenario default if not specified) |
| `path_length` | `float` | ✅ | - | Target path length in meters |
| `num_paths` | `int` | ✅ | - | Number of different paths to generate |
| `path_length_tolerance` | `float` | ❌ | `0.5` | Acceptable deviation from target path length (meters) |
| `min_distance` | `float` | ✅ | - | Minimum distance between consecutive waypoints (meters) |
| `seed` | `int` | ✅ | - | Random seed for reproducible generation |
| `robot_diameter` | `float` | ✅ | - | Robot diameter for collision checking (meters) |

### Example Configuration

```yaml
type: PathVariationRandom
parameters:
  start_pose: "@start_pose"           # Reference to scenario parameter
  goal_poses: "@goal_poses"           # Will output to goal_poses if multiple, goal_pose if single
  num_goal_poses: 3                   # Generate 3 goal poses per path
  path_length: 15.0                   # Target 15 meter paths
  path_length_tolerance: 1.0          # Allow ±1m deviation
  num_paths: 10                       # Generate 10 different path variants
  min_distance: 2.0                   # Minimum 2m between waypoints
  seed: 12345                         # Reproducible random generation
  robot_diameter: 0.6                 # 60cm robot diameter
```

### Behavior Notes

- Automatically detects output parameter name based on `goal_poses` reference:
  - If referencing `@goal_pose` → outputs single pose to `goal_pose` parameter  
  - If referencing `@goal_poses` → outputs pose list to `goal_poses` parameter
- Generates sequential waypoints with target distances between them
- Validates path length within specified tolerance
- Uses path caching for performance optimization

## PathVariationRasterized

Generates systematic path coverage using a regular grid of waypoints across the map.

### Rasterized Configuration Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_pose` | `str \| PoseConfig` | ❌ | `None` | Start position (parameter reference, direct pose, or use grid points) |
| `num_goal_poses` | `int` | ❌ | `1` | Number of goal poses per path |
| `map_file` | `str` | ❌ | `None` | Path to map file (uses scenario default if not specified) |
| `raster_size` | `float` | ✅ | - | Grid spacing between raster points (meters) |
| `raster_offset_x` | `float` | ❌ | `0.0` | X-axis offset for grid alignment (meters) |
| `raster_offset_y` | `float` | ❌ | `0.0` | Y-axis offset for grid alignment (meters) |
| `path_length` | `float` | ✅ | - | Target path length in meters |
| `path_length_tolerance` | `float` | ❌ | `0.5` | Acceptable deviation from target path length (meters) |
| `robot_diameter` | `float` | ✅ | - | Robot diameter for collision checking (meters) |

### Example Configurations

#### Single Goal Pose (Original Grid-to-Grid Behavior)

```yaml
type: PathVariationRasterized
parameters:
  start_pose: "@start_pose"           # Use specific start pose
  num_goal_poses: 1                   # Single goal per path
  raster_size: 2.0                    # 2m grid spacing
  raster_offset_x: 1.0                # Offset grid by 1m in X
  raster_offset_y: 0.5                # Offset grid by 0.5m in Y
  path_length: 10.0                   # Target 10m paths
  path_length_tolerance: 0.8          # Allow ±0.8m deviation
  robot_diameter: 0.6                 # 60cm robot
```

#### Multiple Goal Poses (Search Radius Algorithm)

```yaml
type: PathVariationRasterized
parameters:
  num_goal_poses: 4                   # Generate 4 goal poses per path
  raster_size: 1.5                    # 1.5m grid spacing
  path_length: 20.0                   # Target 20m paths
  robot_diameter: 0.8                 # 80cm robot
```

### Behavior Modes

#### Single Goal Mode (`num_goal_poses: 1`)

- **Grid-to-Grid**: Generates paths from each valid raster point to every other valid raster point
- **Output**: Single `goal_pose` parameter per generated configuration
- **Use Case**: Comprehensive coverage testing, systematic exploration

#### Multi-Goal Mode (`num_goal_poses > 1`)

- **Search Radius Algorithm**: Calculates optimal search radius based on path length and raster spacing
- **Search Radius**: `(path_length / raster_size) / (num_goal_poses + 1)`
- **Bonus Distance**: Applied to final goal pose for path length optimization
- **Output**: List of poses in `goal_poses` parameter
- **Use Case**: Complex multi-waypoint navigation scenarios

### Grid Generation

- Creates square grid covering entire map area
- Filters out points in obstacles or too close to walls
- Respects robot diameter for collision checking
- Grid alignment controlled by `raster_offset_x` and `raster_offset_y`

## Output Parameters

Both variation types automatically determine the correct output parameter name:

| Scenario | Output Parameter | Value Type | Description |
|----------|------------------|------------|-------------|
| Single goal pose | `goal_pose` | `Pose` | Single pose object |
| Multiple goal poses | `goal_poses` | `list[Pose]` | List of pose objects |

## Best Practices

### Random Path Generation

- Use `num_goal_poses: 1` for simple start-to-goal scenarios
- Use `num_goal_poses > 1` for complex multi-waypoint navigation
- Set `min_distance` based on robot turning radius and obstacle density
- Choose `path_length_tolerance` based on acceptable scenario variation

### Rasterized Path Generation

- Use `raster_size` smaller than typical room dimensions for indoor maps
- Use `num_goal_poses: 1` for systematic coverage analysis
- Use `num_goal_poses > 1` for multi-objective navigation testing
- Adjust `raster_offset_x/y` to align grid with map features when needed

### Performance Considerations

- Smaller `raster_size` increases computation time exponentially
- Large `num_paths` in random variation increases generation time
- Path caching improves performance for repeated generations
- Consider map complexity when setting `path_length_tolerance`

## Error Handling

Common configuration errors and solutions:

| Error | Cause | Solution |
|-------|--------|----------|
| `'NoneType' object has no attribute 'position'` | Missing start_pose parameter reference | Ensure referenced parameter exists in scenario |
| `Start pose not valid on map` | Pose in obstacle or outside map bounds | Check pose coordinates against map |
| `No path found` | Impossible path due to obstacles | Increase `path_length_tolerance` or adjust poses |
| `All points are occupied` | No valid raster points found | Decrease `robot_diameter` or increase `raster_size` |