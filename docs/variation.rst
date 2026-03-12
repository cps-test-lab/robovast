.. _variation-points:

Variation Points
================

RoboVAST supports plugin-provided variation types. The following are available by default:

General
-------

ParameterVariationList
^^^^^^^^^^^^^^^^^^^^^^

Creates configurations from a predefined list of parameter values.

  Expected parameters:

  - ``name``: Name of the parameter to vary, or a list of parameter names for simultaneous multi-parameter variation
  - ``values``: List of values for the parameter. When ``name`` is a list, each entry must itself be a list of values — one per parameter name.

  Example (single parameter)::

    - ParameterVariationList:
        name: robot_radius
        values:
        - 0.175
        - 0.22

  Example (multiple parameters varied together)::

    - ParameterVariationList:
        name:
        - mesh_file
        - map_file
        values:
        - - environments/office/office.stl
          - environments/office/office.yaml
        - - environments/hospital/hospital.stl
          - environments/hospital/hospital.yaml


ParameterVariationDistributionUniform
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Creates configurations with random parameter values from a uniform distribution.

  Expected parameters:

  - ``name``: Name of the parameter to vary
  - ``num_variations``: Number of configurations to create
  - ``min``: Minimum value for the parameter
  - ``max``: Maximum value for the parameter
  - ``type``: Data type of the parameter (e.g., int, float, string)
  - ``seed``: Seed for random number generation to ensure reproducibility

ParameterVariationDistributionGaussian
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Creates configurations with random parameter values from a Gaussian (normal) distribution.

  Expected parameters:

  - ``name``: Name of the parameter to vary
  - ``num_variations``: Number of configurations to create
  - ``mean``: Mean value for the parameter
  - ``std``: Standard deviation for the parameter
  - ``min``: Minimum value for the parameter
  - ``max``: Maximum value for the parameter
  - ``type``: Data type of the parameter (e.g., int, float, string)
  - ``seed``: Seed for random number generation to ensure reproducibility

OneOfVariation
^^^^^^^^^^^^^^

Branches the configuration pipeline by running each child variation independently on a copy of the current configurations. All resulting branches are concatenated into a single flat list.

This enables "one of N alternatives" semantics: every alternative becomes a separate configuration in the downstream pipeline.

  Expected parameters:

  - ``variations``: List of child variation entries, using the same syntax as the top-level ``variations:`` list.

  Example::

    - OneOfVariation:
        variations:
        - ObstacleVariation:
            name: static_objects
            obstacle_configs:
            - amount: 3
              max_distance: 0.3
              model: file:///config/files/models/box.sdf.xacro
            seed: 42
            robot_diameter: 0.35
            count: 2
        - ObstacleVariationWithDistanceTrigger:
            name: dynamic_objects
            spawn_trigger_point: spawn_trigger_point
            spawn_trigger_threshold: spawn_trigger_threshold
            trigger_distance: [1.0, 2.0]
            obstacle_configs:
            - amount: 1
              max_distance: 0.3
              model: file:///config/files/models/box.sdf.xacro
            seed: 42
            robot_diameter: 0.35
            count: 2

Navigation
----------

FloorplanVariation
^^^^^^^^^^^^^^^^^^

Creates floorplan variations from variation files and generates corresponding map and mesh files.

  Expected parameters:

  - ``name``: List of two parameter names - first for map file, second for mesh file
  - ``variation_files``: List of variation files to use for floorplan generation (must contain at least one file)
  - ``num_variations``: Number of floorplan variations to generate (minimum 1)
  - ``seed``: Seed for random number generation to ensure reproducibility

  Generated outputs:

  - Map YAML file (``maps/*.yaml``)
  - Map PGM file (``maps/*.pgm``)
  - 3D mesh STL file (``3d-mesh/*.stl``)

FloorplanGeneration
^^^^^^^^^^^^^^^^^^^

Generates artifacts (maps and 3D meshes) from existing floorplan files without creating variations. Unlike FloorplanVariation which creates multiple variations from .variation files, this processes ``.fpm`` floorplan files directly and generates exactly one configuration per input floorplan.

  Expected parameters:

  - ``name``: List of two parameter names - first for map file, second for mesh file
  - ``floorplans``: List of paths to ``.fpm`` floorplan files to generate artifacts for (must contain at least one file)

  Generated outputs:

  - Map YAML file (``maps/*.yaml``)
  - Map PGM file (``maps/*.pgm``)
  - 3D mesh STL file (``3d-mesh/*.stl``)

  Example configuration:

  .. code-block:: yaml

     - FloorplanGeneration:
         name:
         - map_file
         - mesh_file
         floorplans:
         - floorplans/rooms/rooms.fpm
         - floorplans/hallways/hallways.fpm

PathVariationRandom
^^^^^^^^^^^^^^^^^^^

Generates random navigation paths with multiple waypoints between start and goal poses.

  Expected parameters:

  - ``start_pose``: Start position as parameter reference (``@start_pose``) or direct pose with ``x``, ``y``, ``yaw`` (in meters and radians)
  - ``goal_poses``: Goal positions as parameter reference (``@goal_poses`` or ``@goal_pose``) or list of poses
  - ``num_goal_poses``: Number of goal poses to generate per path (determines output parameter: single ``goal_pose`` or list ``goal_poses``)
  - ``path_length``: Target path length in meters
  - ``num_paths``: Number of different paths to generate
  - ``min_distance``: Minimum distance between consecutive waypoints in meters
  - ``map_file``: Optional map file path (uses scenario default if omitted)
  - ``path_length_tolerance``: Acceptable deviation from target path length in meters (default: 0.5)
  - ``seed``: Random seed for reproducible generation
  - ``robot_diameter``: Robot diameter for collision checking in meters

  Behavior:

  - Generates sequential waypoints with target distances between them
  - Validates path length within specified tolerance
  - Automatically detects output parameter name from reference: ``@goal_pose`` outputs single pose, ``@goal_poses`` outputs list
  - Uses path caching for performance optimization

  Generated outputs:

  - ``start_pose``: Generated or specified start pose
  - ``goal_pose`` or ``goal_poses``: Generated goal pose(s) depending on reference
  - Internal path data for validation and visualization

ObstacleVariation
^^^^^^^^^^^^^^^^^

Places random obstacles in the environment based on configured obstacle types.

  Expected parameters:

  - ``name``: Name of the parameter to store static objects
  - ``obstacle_configs``: List of obstacle configurations, each containing:

    - ``amount``: Number of obstacles to place. Mutually exclusive with ``amount_per_m``.
    - ``amount_per_m``: Obstacles per meter of path length (computed as ``floor(amount_per_m × path_length)``). Accepts a single float or a list of floats — each value produces a separate variation. Mutually exclusive with ``amount``.
    - ``max_distance``: Maximum distance from the path for obstacle placement. Accepts a single float or a list of floats — each value produces a separate variation.
    - ``model``: Model name/path for the obstacle
    - ``xacro_arguments``: Arguments to pass to xacro for model generation

  - ``seed``: Seed for random number generation to ensure reproducibility
  - ``robot_diameter``: Diameter of the robot for collision checking
  - ``map_file``: Optional map file path (can be omitted if provided by previous variation)
  - ``count``: Number of obstacle configurations to generate (default: 1)

  Generated outputs:

  - List of static objects with spawn poses and model information

ObstacleVariationWithDistanceTrigger
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Places exactly one obstacle at a position that is at least *trigger_distance* arc-length ahead of the robot's start along the planned path. Writes two scenario parameters for use in the scenario script.

  Expected parameters:

  - ``name``: Name of the parameter to store the placed obstacle
  - ``spawn_trigger_point``: Scenario parameter name to receive the obstacle's spawn pose position
  - ``spawn_trigger_threshold``: Scenario parameter name to receive the trigger distance value that was used
  - ``trigger_distance``: Arc-length in meter from the start to the obstacle. Accepts a single float or a list of floats — one output configuration is produced per value.
  - ``obstacle_configs``: List of obstacle configurations (same format as ``ObstacleVariation``). Total ``amount`` across all entries must equal exactly 1.
  - ``seed``: Seed for random number generation to ensure reproducibility
  - ``robot_diameter``: Diameter of the robot for collision checking in meter
  - ``map_file``: Optional map file path (uses scenario default if omitted)
  - ``count``: Number of obstacle configurations to generate (default: 1)
  - ``start_pose``: Optional explicit start pose (dict with ``x``, ``y``, ``yaw``)
  - ``goal_pose``: Optional explicit goal pose (dict with ``x``, ``y``, ``yaw``)

  Generated outputs:

  - ``<name>``: Placed obstacle with spawn pose and model information
  - ``<spawn_trigger_point>``: Position of the placed obstacle
  - ``<spawn_trigger_threshold>``: The trigger distance value that was applied

  Example::

    - ObstacleVariationWithDistanceTrigger:
        name: dynamic_objects
        spawn_trigger_point: spawn_trigger_point
        spawn_trigger_threshold: spawn_trigger_threshold
        trigger_distance: [1.0, 2.0]
        obstacle_configs:
        - amount: 1
          max_distance: [0.0, 0.3]
          model: file:///config/files/models/box.sdf.xacro
          xacro_arguments: width:=0.5, length:=0.5, height:=1.0
        seed: 42
        robot_diameter: 0.35
        count: 2

PathVariationRasterized
^^^^^^^^^^^^^^^^^^^^^^^

Creates route variations covering all areas of the map using a square grid rasterization. Generates paths between raster points that meet specified path length criteria.

  Expected parameters:

  - ``raster_size``: Grid spacing between raster points in meters
  - ``path_length``: Target path length in meters
  - ``robot_diameter``: Robot diameter for collision checking in meters
  - ``start_pose``: Optional start position as parameter reference (``@start_pose``) or direct pose with ``x``, ``y``, ``yaw``. If omitted, all valid raster points are used as potential start poses.
  - ``num_goal_poses``: Number of goal poses per path (default: 1). Single goal mode uses grid-to-grid paths; multi-goal mode uses search radius algorithm.
  - ``map_file``: Optional map file path (uses scenario default if omitted)
  - ``raster_offset_x``: X-axis offset for grid alignment in meters (default: 0.0)
  - ``raster_offset_y``: Y-axis offset for grid alignment in meters (default: 0.0)
  - ``path_length_tolerance``: Acceptable deviation from target path length in meters (default: 0.5)

  Behavior:

  - Creates square grid covering entire map area, filtering points in obstacles or too close to walls
  - **Single goal mode** (``num_goal_poses: 1``): Generates paths from each valid raster point to every other valid raster point for comprehensive coverage testing
  - **Multi-goal mode** (``num_goal_poses > 1``): Calculates search radius as ``(path_length / raster_size) / (num_goal_poses + 1)`` and generates multiple waypoints per path
  - Respects robot diameter for collision checking and grid alignment controlled by offsets

  Generated outputs:

  - ``start_pose``: Start pose (either specified or from raster grid)
  - ``goal_pose`` or ``goal_poses``: Goal pose(s) depending on ``num_goal_poses``
  - Internal path data, raster points, and path length for validation and visualization
