.. _configuration-reference:

Configuration Reference
========================

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

scenario_file
"""""""""""""

**Type:** String (file path)

**Required:** Yes

Path to the OpenSCENARIO 2 scenario file (``.osc``), relative to the ``.vast`` configuration file.

.. code-block:: yaml

   configuration:
   - name: test
     scenario_file: scenario.osc

parameters
""""""""""

**Type:** List of dictionaries

**Required:** No

Fixed parameter values that apply to all test runs of this scenario. Each list item should be a dictionary with a single parameter name-value pair.

This is useful when you want to define a single configuration with specific values without variations.

.. code-block:: yaml

   configuration:
   - name: test-fixed
     scenario_file: scenario.osc
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
     scenario_file: scenario.osc
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

prepare_script
^^^^^^^^^^^^^^

**Type:** String (file path)

**Required:** No

Path to a bash script that should be executed before each test run. The script is sourced (not executed in a subshell) so it can set environment variables.

The script path is relative to the ``.vast`` configuration file. The script is copied to each test's ``/config/prepare_test.sh`` and executed by the entrypoint before running the scenario.

.. code-block:: yaml

   execution:
     prepare_script: prepare_test.sh

Example ``prepare_test.sh`` script:

.. code-block:: bash

   #!/bin/bash -e
   # Copy custom files to simulation environment
   cp -r /config/files/EmptyWarehouse /root/Projects/SimulationProject/Levels

   # Set environment variables
   export CUSTOM_VAR="value"

   # Run preparation commands
   echo "Test environment prepared"

**Script execution context:**

- Runs before the scenario execution
- Has access to all files in ``/config/``
- Can modify the container environment
- Environment variables set by the script are available to the scenario
- If the script fails (exits with non-zero), the test fails

local
^^^^^

**Type:** Dictionary

**Required:** No

Configuration specific to local execution (when using ``vast run`` or ``vast prepare-run``).

local.additional_docker_run_parameters
"""""""""""""""""""""""""""""""""""""""

**Type:** String (multiline)

**Required:** No

Additional parameters to pass to the ``docker run`` command during local execution. This is useful for adding GPU support, network configuration, display settings, or any other Docker-specific options.

.. code-block:: yaml

   execution:
     local:
       additional_docker_run_parameters: |
         --runtime=nvidia \
         --gpus all \
         --network host \
         -e DISPLAY=${DISPLAY} \
         -e QT_X11_NO_MITSHM=1 \
         -e NVIDIA_VISIBLE_DEVICES=all \
         -e NVIDIA_DRIVER_CAPABILITIES=all

**Notes:**

- Parameters are added to the generated ``run.sh`` script
- Multiline strings are supported using YAML's ``|`` syntax
- Line continuation with backslashes (``\``) is supported
- Environment variable references like ``${DISPLAY}`` are preserved
- These parameters only affect local execution, not Kubernetes cluster execution

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

preprocessing
^^^^^^^^^^^^^

**Type:** List of strings (shell commands)

**Required:** No

Commands to run for preprocessing test results. These are executed before the analysis GUI is launched and typically convert raw data files into more analysis-friendly formats.

.. code-block:: yaml

   analysis:
     preprocessing:
     - ../../../tools/docker_exec.sh rosbags_tf_to_csv.py --frame base_link
     - ../../../tools/docker_exec.sh rosbags_bt_to_csv.py

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
     prepare_script: prepare_test.sh
     local:
       additional_docker_run_parameters: |
         --runtime=nvidia \
         --gpus all
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
     preprocessing:
     - ../../../tools/docker_exec.sh rosbags_tf_to_csv.py --frame base_link
     - ../../../tools/docker_exec.sh rosbags_bt_to_csv.py
     visualization:
     - Analysis:
         single_test: analysis/analysis_single_test.ipynb
         config: analysis/analysis_config.ipynb
         run: analysis/analysis_run.ipynb
