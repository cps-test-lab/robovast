.. _example:

Example
=======

Code is available in :repo_link:`configs/examples/growth_sim`.

TL;DR
-----

To run the example, execute the following commands in the base folder of the RoboVAST repository:

.. code-block:: bash

   # initialize project
   vast init configs/examples/growth_sim/growth_sim.vast

   # show the configurations that will be executed
   vast configuration list

   # setup pods in cluster (kubernetes required)
   vast execution cluster setup minikube
    
   # execute the tests in the cluster
   vast execution cluster run
   
   # OR: execute in detached mode (exit immediately, cleanup manually)
   # vast execution cluster run --detach
   # vast execution cluster run-cleanup  # run this after jobs complete
    
   # download results from the cluster
   vast execution cluster download

   # cleanup pods in cluster
   vast execution cluster cleanup

   # analyze the results
   vast analysis gui

Introduction
------------

The overall workflow in RoboVAST consists of three main steps: 

**Variation** → **Execution** → **Analysis**

For each step, RoboVAST provides dedicated tools to facilitate the process. For details on specific tools, please refer to :doc:`how_to_run`.

Before running any tests, you must initialize the RoboVAST project configuration:

.. code-block:: bash

   vast init <config>

This command sets up the required configuration files and prepares your project for further steps.

Test Description
----------------

In this example, we test a simple logistic growth simulator defined in :repo_link:`configs/examples/growth_sim/files/growth_sim.py`.
We will do parameter sweeps for ``initial_population`` and ``growth_rate``.
The simulator writes its output to a csv file.

This test uses a simple scenario: a single action invokes the growth simulator.
Three scenario parameters are defined, and two of them will be varied later using parameter overriding during scenario execution.
RoboVAST allows you to vary any scenario parameter as needed.

.. literalinclude:: ../configs/examples/growth_sim/scenario.osc
   :language: python
   :caption: Scenario

RoboVAST Configuration
----------------------

The central part of RoboVAST is the configuration file, which defines all aspects of a workflow. It has the ending ``.vast`` and is written in YAML format.

In this example we use configuration file :repo_link:`configs/examples/growth_sim/growth_sim.vast`.

The ``settings`` are split into three main sections: ``configuration``, ``execution``, and ``analysis``.

Configuration
^^^^^^^^^^^^^

The ``configuration`` section defines the test scenarios to be executed. Each scenario specifies:

- ``name``: A unique identifier for the scenario
- ``parameters``: (Optional) Fixed parameters that apply to all configurations of this scenario
- ``variations``: (Optional) Advanced variation types for complex test generation

In this example, we define two scenarios:

1. **test**: Uses ``variations`` to create multiple configurations by varying ``initial_population`` and ``growth_rate`` using the ``ParameterVariationList`` plugin. This creates 4 × 3 = 12 configurations.
2. **test-fixed-values**: Uses ``parameters`` to define a single configuration with fixed values (``growth_rate: 0.07`` and ``initial_population: 123``).

.. literalinclude:: ../configs/examples/growth_sim/growth_sim.vast
   :language: yaml
   :lines: 2-21
   :caption: Configuration section of RoboVAST Configuration File


Execution
^^^^^^^^^

.. note::

     For the execution, it is expected that the connection to the Kubernetes cluster is set up properly.

The ``execution`` section of the ``.vast`` configuration specifies all necessary parameters for running the tests, including the scenario file to execute. For multi-container setups and CPU/memory allocation, see ``resources`` and ``secondary_containers`` in :doc:`configuration`.

.. literalinclude:: ../configs/examples/growth_sim/growth_sim.vast
   :language: yaml
   :lines: 22-29
   :caption: Execution section of RoboVAST Configuration File

The ``scenario_file`` parameter specifies which OpenSCENARIO 2 file to execute (``scenario.osc``).
In this example, we configure 20 runs for each config to ensure statistically meaningful results.
In this basic example we hand in the system-under-test ``growth_sim.py`` directly by specifying the pattern ``**/files/*.py`` in the ``test_files_filter``. In larger setups, it might be required to use a custom container image.

Check Generated Configurations
""""""""""""""""""""""""""""""

Before starting the execution in the cluster, it is recommended to first check the configurations.

.. code-block:: bash

   vast configuration list


Check Result of a Single Execution
""""""""""""""""""""""""""""""""""

To check that the container image and test are correctly set up, it is recommended to test the execution locally.

The command runs the container using the ``docker`` command and the same parameters and test-files as the kubernetes execution. Afterwards the output can be analyzed manually.

.. code-block:: bash

   vast execution local run --config config1 output_config1


Cluster Execution
"""""""""""""""""

To execute all tests in the cluster, run:

.. code-block:: bash

   vast execution cluster run

By default, this command waits for all jobs to complete and displays statistics.

**Detached Execution**

For long-running tests, you can use the ``--detach`` (or ``-d``) flag to exit immediately after creating the jobs:

.. code-block:: bash

   vast execution cluster run --detach

When running in detached mode:

- The command exits right after creating all Kubernetes jobs
- Jobs continue running in the background in the cluster
- You can monitor job status using ``kubectl get jobs``
- You need to manually clean up jobs after they complete

To clean up after a detached run:

.. code-block:: bash

   vast execution cluster run-cleanup

This removes all scenario execution jobs and their associated pods from the cluster.

Download Results
""""""""""""""""

The output of an execution is stored within the cluster-internal server and can be downloaded with:

.. code-block:: bash

   vast execution cluster download

The resulting folder structure looks like this:

.. code-block:: bash

    growth_sim_results/
    ├── run-<timestamp>/             <-- Each cluster execution creates a new folder 
    |   ├── _config/                 <-- Test files are stored here (as defined by test_files_filter in the .vast configuration)
    |   ├── scenario.osc             <-- The scenario used during this run
    |   ├── <config-name>            <-- Each configuration is stored within a separate folder (example: config42)
    |   |   ├── scenario.config      <-- The parameter set used within this configuration (e.g. growth_rate: 0.07, initial_population: 123)
    |   |   ├── _config/             <-- Generated config-specific files are stored here (e.g. generated maps)
    |   |   ├── <test_number>         <-- Each run of a configuration is stored in a separate folder. It contains all input- and output-files of a single test run
    |   |   |   ├── logs             <-- Logs folder (e.g. for ROS_LOG_DIR)
    |   |   |   |   ├── system.log   <-- The complete system log
    |   |   |   ├── test.xml         <-- Scenario result, in junitxml format
    |   |   |   ├── <test-specifics> <-- Any test-specific files, stored during the test run within /out (e.g. rosbag)


Analysis
^^^^^^^^
As result analysis is tailored to each test, users are expected to implement their own analysis routines.

There are two steps invoked to analyze results.
First, the results can optionally be postprocessed to simplify later analysis. The user might specify postprocessing commands in ``analysis.postprocessing`` section of the ``.vast`` configuration. Common scripts including converting ROS bags to CSV files or extracting poses from tf-data are available to improve usability.

.. code-block:: bash

   vast analysis postprocess

Postprocessing is cached based on the results directory hash. If the results directory is unchanged since the last postprocessing, the postprocessing is skipped automatically. To force postprocessing even if the results are unchanged (e.g., after updating postprocessing scripts), use the ``--force`` or ``-f`` flag:

.. code-block:: bash

   vast analysis postprocess --force

After postprocessing, the actual analysis can be performed.
To simplify this process, RoboVAST provides a GUI tool, which enables users to execute Jupyter notebooks directly from a graphical interface.

.. code-block:: bash

   vast analysis gui

The visualization can be customized by adapting the ``analysis.visualization`` section of the ``.vast`` configuration file.

.. literalinclude:: ../configs/examples/growth_sim/growth_sim.vast
   :language: yaml
   :lines: 32-36
   :caption: Analysis section of RoboVAST Configuration File

Although this example includes only one entry in the analysis list, you can add more. Each additional entry will appear as a separate tab in the GUI.

There are three reserved keys for analysis: ``single_test``, ``config``, and ``run``. These allow you to specify Jupyter notebooks for different scopes:

- **single_test**: analyzes an individual test run.
- **config**: analyzes all test runs for a specific configuration/parameter set.
- **run**: analyzes all tests within a run, covering all configurations and parameter sets.

You are free to implement the notebooks as needed. The only requirement is that each notebook includes the following line:

.. code-block:: python

   DATA_DIR = ''

During execution within the GUI the content of ``DATA_DIR`` is replaced by the currently selected test-directory.

To improve usability the output of the jupyter-notebook-execution is cached and once it was generated it will be displayed instantly.
