.. _configuration:

Configuration
=============

This page documents all available parameters in the ``.vast`` configuration file format. The configuration file is written in YAML and defines all aspects of the RoboVAST workflow.

File Structure
--------------

A ``.vast`` configuration file has the following top-level structure:

.. code-block:: yaml

   version: 1
   metadata:
     title: "Project Title"
     description: "Project description"
     ...
   configuration:
     - name: scenario1
       ...
   execution:
     ...
   analysis:
     ...

Version
-------

**Type:** Integer

**Required:** Yes

Specifies the version of the configuration file format. Currently, only version ``1`` is supported.

.. code-block:: yaml

   version: 1


Metadata Section
----------------

**Type:** Dictionary

**Required:** No

The ``metadata`` section allows you to provide structured information about the run configuration. This section can contain arbitrary key-value pairs and nested structures. If present, the metadata will be included in the generated ``configurations.yaml`` file.

.. code-block:: yaml

   metadata:
     title: "Robot Navigation Results"
     description: "Autonomous navigation performance evaluation"
     creator: "Your Name"
     keywords: ["robotics", "navigation", "ROS2"]
     license: "CC-BY-4.0"
     custom_fields:
       nested_data: "value"

All fields within ``metadata`` are optional and can be customized according to your needs.


Configuration Section
---------------------

The ``configuration`` section defines which runs are to be executed. It is a list where each entry represents a scenario with its parameters and variations.

Scenario Definition
^^^^^^^^^^^^^^^^^^^

Each scenario in the configuration list has the following structure:

name
""""

**Type:** String

**Required:** Yes

A unique identifier for the scenario. This name will be used as the directory name for results.

.. code-block:: yaml

   configuration:
   - name: test-scenario-1

parameters
""""""""""

**Type:** List of dictionaries

**Required:** No

Fixed parameter values that apply to all runs of this scenario. Each list item should be a dictionary with a single parameter name-value pair.

This is useful when you want to define a single configuration with specific values without variations.

.. code-block:: yaml

   configuration:
   - name: test-fixed
     parameters:
     - growth_rate: 0.07
     - initial_population: 123
     - goal_pose:
         position:
           x: 10.0
           y: 5.0

variations
""""""""""

**Type:** List of variation definitions

**Required:** No

Defines parameter variations to create multiple run configurations. Each variation uses a plugin-provided variation type. See :ref:`variation-points` for available variation types.

Multiple variations are combined using Cartesian product to generate all possible parameter combinations.

.. code-block:: yaml

   configuration:
   - name: test-variations
     variations:
     - ParameterVariationList:
         name: speed
         values:
         - 1.0
         - 2.0
         - 3.0
     - ParameterVariationList:
         name: distance
         values:
         - 5.0
         - 10.0

This example creates 3 × 2 = 6 run configurations.

.. note::

   You cannot specify both ``parameters`` and ``variations`` for the same scenario. Use ``parameters`` for fixed values or ``variations`` for parameter sweeps.


Execution Section
-----------------

The ``execution`` section specifies how and where tests are executed.

image
^^^^^

**Type:** String (Docker image reference)

**Required:** Yes

Docker container image to use for execution. Can be a public image or a private registry image.

.. code-block:: yaml

   execution:
     image: ghcr.io/cps-test-lab/robovast:latest

runs
^^^^

**Type:** Integer

**Required:** Yes (unless specified in CLI)

Number of times to execute each run configuration. Multiple runs allow for statistical analysis of results.

.. code-block:: yaml

   execution:
     runs: 20

timeout
^^^^^^^

**Type:** Integer (seconds)

**Required:** No

**Applies to:** Cluster execution (Kubernetes). For local execution, this value is currently not enforced.

Maximum wall-clock time (in seconds) allowed for a single run.

- **Local (Docker Compose):** Currently not enforced; local runs will continue past this timeout and must be stopped manually.
- **Cluster (Kubernetes):** Sets ``activeDeadlineSeconds`` on the Job spec; Kubernetes terminates the pod when the deadline expires.

If omitted (or ``null``), there is no time limit.

.. code-block:: yaml

   execution:
     timeout: 3600   # 1 hour per run

scenario_file
^^^^^^^^^^^^^

**Type:** String (file path)

**Required:** Yes

Path to the OpenSCENARIO 2 scenario file (``.osc``), relative to the ``.vast`` configuration file. This defines the scenario to execute for all configurations.

.. code-block:: yaml

   execution:
     scenario_file: scenario.osc

run_as_user
^^^^^^^^^^^

**Type:** Integer

**Required:** No

The user ID (UID) to run the container as. Defaults to ``1000`` if not specified. If your container requires running as root, set this to ``0``.

.. code-block:: yaml

   execution:
     run_as_user: 1000

pre_command
^^^^^^^^^^^

**Type:** String (path to executable script)

**Required:** No

Path to an executable script that will be sourced before each run. The file is executed using ``source <pre_command>``, allowing environment variables to be set and made available to the scenario execution.

**Important constraints:**

- Must be a path to an existing executable file
- No command line parameters are allowed
- The file is sourced (not executed in a sub-shell), so environment variable changes persist

.. code-block:: yaml

   execution:
     pre_command: /config/files/pre_command.sh
     run_files:
     - "**/files/*.sh"

**Command execution context:**

- Runs before the scenario execution via ``source <pre_command>``
- Can modify the container environment
- Environment variables set by the script are available to the scenario
- If the script fails (exits with non-zero), the run fails

.. note::

   Custom scripts can be included in the container using ``run_files`` (see below) to make them available at the specified path.

post_command
^^^^^^^^^^^^

**Type:** String (path to executable)

**Required:** No

Path to an executable file that should be executed after the scenario completes. This is passed to the scenario execution as the ``--post-run`` parameter.

**Important constraints:**

- Must be a path to an existing executable file
- No command line parameters are allowed
- No shell commands or piping allowed
- The file must have executable permissions

.. code-block:: yaml

   execution:
     post_command: /config/files/post_command.sh
     run_files:
     - "**/files/*.sh"

The post command script is executed by the scenario execution framework after the scenario finishes, allowing for cleanup or post-processing tasks.

.. note::

   Custom scripts can be included in the container using ``run_files`` (see below) to make them available at the specified path.

run_files
^^^^^^^^^

**Type:** List of strings (glob patterns)

**Required:** No

List of glob patterns specifying which files from the scenario directory should be copied into the run container. This is useful for including run-specific files like scripts, models, or configuration files.

.. code-block:: yaml

   execution:
     run_files:
     - "**/files/*.py"
     - "**/models/*.sdf"
     - "**/maps/*"

env
^^^

**Type:** List of dictionaries

**Required:** No

Additional environment variables to set in the run container. Each list item should be a single key-value pair.

.. code-block:: yaml

   execution:
     env:
     - RMW_IMPLEMENTATION: rmw_cyclonedds_cpp
     - CUSTOM_VAR: custom_value
     - ENABLE_X11: "false"

resources
^^^^^^^^^

**Type:** Dictionary

**Required:** No

**Applies to:** Local and cluster execution

CPU and memory limits for the main (primary) container. Used by Docker Compose for local runs and by Kubernetes for cluster runs. These values are also exposed as ``AVAILABLE_CPUS`` and ``AVAILABLE_MEM`` environment variables inside the container.

.. code-block:: yaml

   execution:
     resources:
       cpu: 6
       memory: 8Gi

**Available fields:**

- ``cpu`` (Optional): Number of CPU cores (integer), or a per-cluster list
- ``memory`` (Optional): Memory limit (e.g., ``8Gi``, ``4096Mi``), or a per-cluster list

**Per-cluster resource values** are supported when multiple clusters need
different allocations.  See :ref:`cluster-execution` for the full syntax.

.. code-block:: yaml

   execution:
     resources:
       cpu:
         - gcp-c4: 4     # 4 CPUs on the gcp-c4 cluster
         - local:   8     # 8 CPUs when running locally

secondary_containers
^^^^^^^^^^^^^^^^^^^^

**Type:** List of container definitions

**Required:** No

**Applies to:** Local and cluster execution

Additional containers that run alongside the main ``robovast`` container in the same pod (Kubernetes) or Docker Compose stack (local). Use this to run separate processes such as the navigation stack or simulation in dedicated containers, each with its own CPU and memory allocation. All containers share the same network namespace and can communicate via localhost.

Each entry is either a container name (string) or a dictionary with the container name as key and optional ``resources`` as value. All secondary containers use the same Docker image as the main container.

.. code-block:: yaml

   execution:
     resources:
       cpu: 2
     secondary_containers:
     - nav:
         resources:
           cpu: 3
           memory: 4Gi
     - simulation:
         resources:
           cpu: 5
           memory: 8Gi
           gpu: 1

**Per-container resources:**

- ``cpu`` (Optional): Number of CPU cores for this container, or a per-cluster list
- ``memory`` (Optional): Memory limit (e.g., ``4Gi``, ``4096Mi``), or a per-cluster list
- ``gpu`` (Optional): Number of GPUs (enables NVIDIA runtime when set)

Per-cluster lists follow the same syntax as the main ``resources`` field.
See :ref:`cluster-execution` for details.

.. note::

   Secondary containers run the ``secondary_entrypoint.sh`` script and receive ``CONTAINER_NAME`` and ``ROS_LOG_DIR`` environment variables. Ensure your scenario or entrypoint logic handles multiple containers appropriately.

local
^^^^^

**Type:** Dictionary

**Required:** No

**Applies to:** Local execution only (ignored for cluster runs)

Configuration options that apply only when running tests locally (e.g. ``vast execution local run``).

local.parameter_overrides
""""""""""""""""""""""""""

**Type:** List of dictionaries (key-value pairs)

**Required:** No

Overrides for scenario parameters that are added to the generated ``scenario.config`` **only for local runs**. Each list item is a single key-value pair. Values override whatever was produced by configuration variations. Nested dictionaries are supported (values are replaced entirely).

Parameters are validated against the scenario file (``.osc``); only parameters defined in the scenario are allowed.

.. code-block:: yaml

   execution:
     scenario_file: scenario.osc
     local:
       parameter_overrides:
       - headless: "False"
       - use_rviz: "True"

.. note::

   Parameter values must match the types expected by the scenario. If the scenario defines a parameter as a string (e.g. ``headless: string = "False"``), use quoted values.


Results Processing Section
--------------------------

The ``results_processing`` section defines how run results should be processed after execution.

postprocessing
^^^^^^^^^^^^^^

**Type:** List of strings (plugin commands)

**Required:** No

Commands to run for postprocessing run results. These are executed before the evaluation GUI is launched and typically convert raw data files into more analysis-friendly formats.

**All postprocessing commands are plugins.** Each command is specified either as:
- A simple string (for commands without parameters)
- A dictionary with the plugin name as key and parameters as value

.. code-block:: yaml

   results_processing:
     postprocessing:
       - rosbags_tf_to_csv:
           frames: [base_link, turtlebot4_base_link_gt]
       - rosbags_bt_to_csv
       - rosbags_to_webm:
           topic: /camera/image_raw/compressed
           fps: 30
       - rosbags_action_to_csv:
           action: navigate_to_pose
       - command:
           script: ../../../tools/custom_script.sh
           args: [--arg, value]

To list all available plugins and their descriptions:

.. code-block:: bash

   vast results postprocess-commands

**Built-in Postprocessing Plugins:**

- ``rosbags_tf_to_csv``: Convert ROS TF transformations to CSV format. Optional ``frames`` parameter (list of frame names).
- ``rosbags_bt_to_csv``: Convert ROS behavior tree logs to CSV format (no parameters).
- ``rosbags_to_csv``: Extract a specific set of ROS topics from rosbags to separate CSV files. Required ``topics`` parameter (list of topic names to extract). For each topic one CSV file per bag is written next to the bag, named ``<bag>_<topic>.csv``.
- ``rosbags_to_webm``: Convert a ``sensor_msgs/msg/CompressedImage`` topic from ROS bags to WebM video files (VP9 codec). Optional ``topic`` parameter (compressed image topic name, default ``/camera/image_raw/compressed``) and ``fps`` parameter (fallback frame rate when timestamps are unavailable, default ``30``).
- ``rosbags_action_to_csv``: Extract ROS2 action feedback and status messages to two CSV files (``<filename_prefix>_feedback.csv`` and ``<filename_prefix>_status.csv``). Reads ``/<action>/_action/feedback`` and ``/<action>/_action/status`` topics. Nested data is flattened to columns. Required ``action`` parameter (action name, e.g. ``navigate_to_pose``). Optional ``filename_prefix`` parameter (default: ``action_<action>``).
- ``command``: Execute arbitrary commands or scripts. Requires ``script`` parameter, optional ``args`` parameter (list).
- ``compress``: Create a gzipped tarball (``campaign-<id>.tar.gz``) for each campaign directory; runs on the host (no Docker). Optional ``output_dir`` (default: results directory), ``exclude_dirs`` (directory names to exclude, default ``['.cache']``), ``overwrite`` (if ``false``, skip when a tarball already exists; default ``false``).

See :ref:`extending-postprocessing` for how to add custom postprocessing plugins.

publication
^^^^^^^^^^^

**Type:** List of strings or dictionaries (plugin commands)

**Required:** No

Defines publication plugins that package or distribute the results directory after
postprocessing.  Each entry is either a plugin name (string) or a dictionary with
the plugin name as key and plugin-specific parameters as value.

Publication plugins are executed by ``vast results publish`` and operate on the
full results directory (parent of ``campaign-*`` directories).

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           include_filter:
           - "*.csv"
           - "/_config/*"
           exclude_filter:
           - "*.pyc"
           destination: archives/

**Built-in Publication Plugins:**

- ``zip``: Create a zip archive for every ``campaign-*`` directory under the
  results directory.  Optional parameters:

  - ``filename``: Template for the zip filename.  Supports ``{key}``
    placeholders resolved from the ``.vast`` file's ``metadata:`` section and
    the built-in ``{timestamp}`` placeholder (extracted from the campaign
    directory name, e.g. ``campaign-2026-03-05-121530`` → ``2026-03-05-121530``).
    Example: ``my_dataset_{robot_id}_{timestamp}.zip``.
    If omitted, the default name ``<campaign-dir-name>.zip`` is used.
    A descriptive error listing all available placeholders is raised when an
    unknown placeholder is referenced.
  - ``include_filter``: List of glob patterns.  Only matching files are included.
    Patterns starting with ``/`` are anchored to the campaign root; patterns without
    ``/`` match on the basename only; other patterns are matched against the full
    relative path.  If omitted, all files are candidates.
  - ``exclude_filter``: List of glob patterns.  Matching files are excluded regardless
    of ``include_filter``.
  - ``destination``: Directory where zip files are written.  Relative paths are
    resolved from the results directory.  Defaults to the results directory itself.
  - ``overwrite``: Controls behavior when the output zip file already exists.
    ``true`` always overwrites silently; ``false`` always skips silently.
    Omit (or leave unset) to be prompted interactively — the default answer is
    *yes* (overwrite).  Passing ``--force`` / ``-f`` on the CLI is equivalent
    to setting ``overwrite: true`` for every plugin.

Multiple ``zip`` entries may be defined to produce different archives from the
same campaign:

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           filename: my_dataset_{robot_id}_{timestamp}.zip
           include_filter: ["*.csv"]
           destination: csv-archives/
           overwrite: false    # skip if archive already exists
       - zip:
           filename: my_dataset_{robot_id}_{timestamp}_videos.zip
           include_filter: ["*.webm"]
           destination: video-archives/

See :ref:`extending-publication` for how to add custom publication plugins.

metadata_processing
^^^^^^^^^^^^^^^^^^^^

**Type:** List of strings or dictionaries (plugin commands)

**Required:** No

Defines metadata processing plugins that run after generic metadata generation.

.. code-block:: yaml

   results_processing:
     metadata_processing:
       - my_plugin
       - my_plugin:
           param1: value1


Evaluation Section
------------------

The ``evaluation`` section defines how run results should be visualized and evaluated.

visualization
^^^^^^^^^^^^^

**Type:** List of dictionaries

**Required:** No

Defines evaluation notebooks for visualization in the evaluation GUI. Each entry creates a tab in the GUI.

Each dictionary can have a custom name and three reserved keys for different evaluation scopes:

- ``run``: Path to Jupyter notebook for analyzing a single run
- ``config``: Path to Jupyter notebook for analyzing all runs of a configuration
- ``campaign``: Path to Jupyter notebook for analyzing all runs in a campaign

.. code-block:: yaml

   evaluation:
     visualization:
     - Analysis:
         run: analysis/analysis_run.ipynb
         config: analysis/analysis_config.ipynb
         campaign: analysis/analysis_campaign.ipynb
     - Performance:
         run: analysis/performance_run.ipynb
         config: analysis/performance_config.ipynb

**Notebook requirements:**

Each notebook must include the following placeholder line:

.. code-block:: python

   DATA_DIR = ''

The RoboVAST GUI automatically replaces this with the path to the selected run directory when executing the notebook.


Complete Example
----------------

Here's a complete example showing all major configuration options:

.. code-block:: yaml

   version: 1
   configuration:
   - name: parameter-sweep
     scenario_file: scenario.osc
     variations:
     - ParameterVariationList:
         name: velocity
         values: [1.0, 2.0, 3.0]
     - ParameterVariationDistributionUniform:
         name: obstacle_count
         num_variations: 5
         min: 1
         max: 10
         type: int
         seed: 42
   - name: baseline
     scenario_file: scenario.osc
     parameters:
     - velocity: 2.0
     - obstacle_count: 5
   execution:
     image: ghcr.io/cps-test-lab/robovast:latest
     runs: 20
     resources:
       cpu: 4
       memory: 8Gi
     secondary_containers:
     - nav:
         resources:
           cpu: 3
           memory: 4Gi
     pre_command: /config/files/prepare_test.sh
     post_command: /config/files/post_command.sh
     run_as_user: 1000
     run_files:
     - "**/files/*"
     - "**/models/*.sdf"
     env:
     - RMW_IMPLEMENTATION: rmw_cyclonedds_cpp
   results_processing:
     postprocessing:
     - rosbags_tf_to_csv:
        frames: [base_link]
     - rosbags_bt_to_csv
     - rosbags_to_csv
     - rosbags_to_webm
   evaluation:
     visualization:
     - Analysis:
         run: analysis/analysis_run.ipynb
         config: analysis/analysis_config.ipynb
         campaign: analysis/analysis_campaign.ipynb
