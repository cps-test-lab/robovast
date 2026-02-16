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

Generates artifacts (maps and 3D meshes) from existing floorplan files without creating variations. Unlike FloorplanVariation which creates multiple variations from .variation files, this processes .fpm floorplan files directly and generates exactly one configuration per input floorplan.

  Expected parameters:

  - ``name``: List of two parameter names - first for map file, second for mesh file
  - ``floorplans``: List of paths to .fpm floorplan files to generate artifacts for (must contain at least one file)

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

Creates random route variations with start and goal poses, generating navigable paths of specified length.

  Expected parameters:

  - ``start_pose``: Start pose as either a reference (string starting with ``@``) or a direct pose specification with ``x``, ``y``, and ``yaw``
  - ``goal_pose``: Goal pose as either a reference (string starting with ``@``) or a dict with ``x``, ``y``, and ``yaw``
  - ``map_file``: Optional map file path (can be omitted if provided by previous variation)
  - ``path_length``: Desired path length in meters
  - ``num_paths``: Number of paths to generate
  - ``path_length_tolerance``: Tolerance for path length (default: 0.5)
  - ``min_distance``: Minimum distance between waypoints
  - ``seed``: Seed for random number generation to ensure reproducibility
  - ``robot_diameter``: Diameter of the robot for collision checking

  Generated outputs:

  - ``start_pose``: Generated or specified start pose
  - ``goal_pose``: Generated goal pose
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

  - ``start_pose``: Optional start pose as either a reference (string starting with ``@``) or a direct pose specification with ``x``, ``y``, and ``yaw``. If omitted, all raster points are used as start poses.
  - ``map_file``: Optional map file path (can be omitted if provided by previous variation)
  - ``raster_size``: Grid spacing for square rasterization in meters
  - ``raster_offset_x``: Offset for raster grid in x direction in meters (default: 0.0)
  - ``raster_offset_y``: Offset for raster grid in y direction in meters (default: 0.0)
  - ``path_length``: Desired path length in meters
  - ``path_length_tolerance``: Tolerance for path length (default: 0.5)
  - ``robot_diameter``: Diameter of the robot for collision checking

  Generated outputs:

  - ``start_pose``: Start pose (either specified or from raster grid)
  - ``goal_pose``: Goal pose from raster grid
  - Internal path data, raster points, and path length for validation and visualization
