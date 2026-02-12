.. _configuration:

Configuration
=============

This page documents all available parameters in the ``.vast`` configuration file format. The configuration file is written in YAML and defines all aspects of the RoboVAST workflow.

File Structure
--------------

A ``.vast`` configuration file has the following top-level structure:

.. code-block:: yaml

   version: 1
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


Configuration Section
---------------------

The ``configuration`` section defines test scenarios to be executed. It is a list where each entry represents a scenario with its parameters and variations.

Scenario Definition
^^^^^^^^^^^^^^^^^^^

Each scenario in the configuration list has the following structure:

name
""""

**Type:** String

**Required:** Yes

A unique identifier for the scenario. This name will be used as the directory name for test results.

.. code-block:: yaml

   configuration:
   - name: test-scenario-1

parameters
""""""""""

**Type:** List of dictionaries

**Required:** No

Fixed parameter values that apply to all test runs of this scenario. Each list item should be a dictionary with a single parameter name-value pair.

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

Defines parameter variations to create multiple test configurations. Each variation uses a plugin-provided variation type. See :ref:`variation-points` for available variation types.

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

This example creates 3 × 2 = 6 test configurations.

.. note::

   You cannot specify both ``parameters`` and ``variations`` for the same scenario. Use ``parameters`` for fixed values or ``variations`` for parameter sweeps.


Execution Section
-----------------

The ``execution`` section specifies how and where tests are executed.

image
^^^^^

**Type:** String (Docker image reference)

**Required:** Yes

Docker container image to use for test execution. Can be a public image or a private registry image.

.. code-block:: yaml

   execution:
     image: ghcr.io/cps-test-lab/robovast:latest

runs
^^^^

**Type:** Integer

**Required:** Yes (unless specified in CLI)

Number of times to execute each test configuration. Multiple runs allow for statistical analysis of results.

.. code-block:: yaml

   execution:
     runs: 20

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

Path to an executable script that will be sourced before each test run. The file is executed using ``source <pre_command>``, allowing environment variables to be set and made available to the scenario execution.

**Important constraints:**

- Must be a path to an existing executable file
- No command line parameters are allowed
- The file is sourced (not executed in a sub-shell), so environment variable changes persist

.. code-block:: yaml

   execution:
     pre_command: /config/files/pre_command.sh
     test_files_filter:
     - "**/files/*.sh"

**Command execution context:**

- Runs before the scenario execution via ``source <pre_command>``
- Can modify the container environment
- Environment variables set by the script are available to the scenario
- If the script fails (exits with non-zero), the test fails

.. note::

   Custom scripts can be included in the container using ``test_files_filter`` (see below) to make them available at the specified path.

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
     test_files_filter:
     - "**/files/*.sh"

The post command script is executed by the scenario execution framework after the scenario finishes, allowing for cleanup or post-processing tasks.

.. note::

   Custom scripts can be included in the container using ``test_files_filter`` (see below) to make them available at the specified path.

test_files_filter
^^^^^^^^^^^^^^^^^

**Type:** List of strings (glob patterns)

**Required:** No

List of glob patterns specifying which files from the scenario directory should be copied into the test container. This is useful for including test-specific files like scripts, models, or configuration files.

.. code-block:: yaml

   execution:
     test_files_filter:
     - "**/files/*.py"
     - "**/models/*.sdf"
     - "**/maps/*"

env
^^^

**Type:** List of dictionaries

**Required:** No

Additional environment variables to set in the test container. Each list item should have ``name`` and ``value`` keys.

.. code-block:: yaml

   execution:
     env:
     - name: RMW_IMPLEMENTATION
       value: rmw_cyclonedds_cpp
     - name: CUSTOM_VAR
       value: custom_value

kubernetes
^^^^^^^^^^

**Type:** Dictionary

**Required:** Yes (for cluster execution)

Configuration specific to Kubernetes cluster execution.

kubernetes.resources
""""""""""""""""""""

**Type:** Dictionary

**Required:** Yes

Resource requests/limits for Kubernetes pods.

.. code-block:: yaml

   execution:
     kubernetes:
       resources:
         cpu: 6
         memory: 8Gi

**Available fields:**

- ``cpu`` (Required): Number of CPU cores (integer or string)
- ``memory`` (Optional): Memory limit (e.g., ``8Gi``, ``4096Mi``)


Analysis Section
----------------

The ``analysis`` section defines how test results should be analyzed.

postprocessing
^^^^^^^^^^^^^^

**Type:** List of strings (plugin commands)

**Required:** No

Commands to run for postprocessing test results. These are executed before the analysis GUI is launched and typically convert raw data files into more analysis-friendly formats.

**All postprocessing commands are plugins.** Each command is specified as a dictionary with a ``name`` field for the plugin name and additional fields for parameters.

.. code-block:: yaml

   analysis:
     postprocessing:
       - name: rosbags_tf_to_csv
         frames: [base_link, turtlebot4_base_link_gt]
       - name: rosbags_bt_to_csv
       - name: command
         script: ../../../tools/custom_script.sh
         args: [--arg, value]

To list all available plugins and their descriptions:

.. code-block:: bash

   vast analysis postprocess-commands

**Built-in Postprocessing Plugins:**

- ``rosbags_tf_to_csv``: Convert ROS TF transformations to CSV format. Optional ``frames`` parameter (list of frame names).
- ``rosbags_bt_to_csv``: Convert ROS behavior tree logs to CSV format (no parameters).
- ``command``: Execute arbitrary commands or scripts. Requires ``script`` parameter, optional ``args`` parameter (list).

See :ref:`extending-postprocessing` for how to add custom postprocessing plugins.

visualization
^^^^^^^^^^^^^

**Type:** List of dictionaries

**Required:** No

Defines analysis notebooks for visualization in the analysis GUI. Each entry creates a tab in the GUI.

Each dictionary can have a custom name and three reserved keys for different analysis scopes:

- ``single_test``: Path to Jupyter notebook for analyzing a single test run
- ``config``: Path to Jupyter notebook for analyzing all runs of a configuration
- ``run``: Path to Jupyter notebook for analyzing all tests in an execution run

.. code-block:: yaml

   analysis:
     visualization:
     - Analysis:
         single_test: analysis/analysis_single_test.ipynb
         config: analysis/analysis_config.ipynb
         run: analysis/analysis_run.ipynb
     - Performance:
         single_test: analysis/performance_single.ipynb
         config: analysis/performance_config.ipynb

**Notebook requirements:**

Each notebook must include the following placeholder line:

.. code-block:: python

   DATA_DIR = ''

The RoboVAST GUI automatically replaces this with the path to the selected test directory when executing the notebook.


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
     pre_command: /config/files/prepare_test.sh
     post_command: /config/files/post_command.sh
     run_as_user: 1000
     test_files_filter:
     - "**/files/*"
     - "**/models/*.sdf"
     env:
     - name: RMW_IMPLEMENTATION
       value: rmw_cyclonedds_cpp
     kubernetes:
       resources:
         cpu: 4
         memory: 8Gi
   analysis:
     postprocessing:
     - ../../../tools/docker_exec.sh rosbags_tf_to_csv.py --frame base_link
     - ../../../tools/docker_exec.sh rosbags_bt_to_csv.py
     visualization:
     - Analysis:
         single_test: analysis/analysis_single_test.ipynb
         config: analysis/analysis_config.ipynb
         run: analysis/analysis_run.ipynb
