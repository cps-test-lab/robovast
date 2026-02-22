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

  - ``name``: Name of the parameter to vary
  - ``values``: List of values for the parameter


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

    - ``amount``: Number of obstacles to place
    - ``max_distance``: Maximum distance from path (currently not used for random placement)
    - ``model``: Model name/path for the obstacle
    - ``xacro_arguments``: Arguments to pass to xacro for model generation

  - ``seed``: Seed for random number generation to ensure reproducibility
  - ``robot_diameter``: Diameter of the robot for collision checking
  - ``map_file``: Optional map file path (can be omitted if provided by previous variation)
  - ``count``: Number of obstacle configurations to generate (default: 1)

  Generated outputs:

  - List of static objects with spawn poses and model information

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
