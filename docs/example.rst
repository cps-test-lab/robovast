.. _example:

Example
=======

Code is available in :repo_link:`examples/growth_sim`.

TL;DR
-----

To run the example, execute the following commands in the base folder of the RoboVAST repository:

.. code-block:: bash

   # show the variants, that will later be executed according to configuration
   poetry run list_variants --config examples/growth_sim/growth_sim.vast
    
   # execute the tests in the cluster (kubernetes required)
   poetry run cluster_execution --config examples/growth_sim/growth_sim.vast
    
   # download results from the cluster
   poetry run download_results --output ./growth_sim_results

   # analyze the results
   poetry run result_analyzer --config examples/growth_sim/growth_sim.vast \
       --results-dir ./growth_sim_results

Introduction
------------

The overall workflow in RoboVAST consists of three main steps: 

**Variation** → **Execution** → **Analysis**

For each step, RoboVAST provides dedicated tools to facilitate the process. For details on specific tools, please refer to :doc:`how_to_run`.

Test Definition
---------------

In this example, we test a simple logistic growth simulator defined in :repo_link:`examples/growth_sim/files/growth_sim.py`.
We will do parameter sweeps for ``initial_population`` and ``growth_rate``.
The simulator writes its output to a csv file.

This test uses a simple scenario: a single action invokes the growth simulator.
Three scenario parameters are defined, and two of them will be varied later using parameter overriding during scenario execution.
RoboVAST allows you to vary any scenario parameter as needed.

.. literalinclude:: ../examples/growth_sim/scenario.osc
   :language: python
   :caption: Scenario

RoboVAST Configuration
----------------------

The central part of RoboVAST is the configuration file, which defines all aspects of a workflow. It has the ending ``.vast`` and is written in YAML format.

In this example we use configuration file :repo_link:`examples/growth_sim/growth_sim.vast`.

The ``settings`` are split into three main sections: ``variation``, ``execution``, and ``analysis``.

Variation
^^^^^^^^^

The section ``variation`` is defined as a list of variations.
By using python entry-points as plugin mechanism it is possible to create custom variations.
Available variation types are described in :ref:`variation-points`.

In this example, we vary the parameters ``initial_population`` and ``growth_rate`` using a fixed list of values and the variation plugin ``ParameterVariationList``.

.. literalinclude:: ../examples/growth_sim/growth_sim.vast
   :language: yaml
   :lines: 13-26
   :caption: Variation section of RoboVAST Configuration File


RoboVAST creates a test for each combination, in this example 4 * 3 = 12 tests.


Execution
^^^^^^^^^

.. note::

     For the execution, it is expected that the connection to the Kubernetes cluster is set up properly.

The ``execution`` section of the ``.vast`` configuration specifies all necessary parameters for running the tests:

.. literalinclude:: ../examples/growth_sim/growth_sim.vast
   :language: yaml
   :lines: 7-12
   :caption: Execution section of RoboVAST Configuration File

In this example, we configure 100 runs for each variant to ensure statistically meaningful results.
In this basic example we hand in the system-under-test ``growth_sim.py`` directly by specifying the pattern ``**/files/*.py``. In larger setups, it might be required to use a custom container image.

Check Generated Variants
""""""""""""""""""""""""

Before starting the execution in the cluster, it is recommended to first check the variants.

.. code-block:: bash

   poetry run list_variants \
        --config examples/growth_sim/growth_sim.vast


Check Result of a Single Execution
""""""""""""""""""""""""""""""""""

To check, that the container image and the test are correctly set up, it is recommended to test the execution locally.

The command runs the container using the ``docker`` command and the same parameters and test-files as the kubernetes execution. Afterwards the output can be analyzed manually.

.. code-block:: bash

   poetry run execute_local \
        --config examples/growth_sim/growth_sim.vast \
        --variant variant1 \
        --output output_variant1


Cluster Execution
"""""""""""""""""

To execute all tests in the cluster, run ``cluster_execution``.

.. code-block:: bash

   poetry run cluster_execution \
        --config examples/growth_sim/growth_sim.vast

Download Results
""""""""""""""""

The output of an execution is stored within the cluster-internal NFS-server and can be downloaded with

.. code-block:: bash

   poetry run download_results \
        --output ./growth_sim_results

The resulting folder structure looks like this:

.. code-block:: bash

    growth_sim_results/
    ├── run_<timestamp>/             <-- Each cluster execution creates a new folder 
    |   ├── variant<index>           <-- Each variant is stored within a separate folder (example: variant42)
    |   |   ├── <run_number>         <-- Each run of a variant is stored in a separate folder. It contains all input- and output-files of a single test run
    |   |   |   ├── logs             <-- Logs folder (e.g. for ROS_LOG_DIR)
    |   |   |   |   ├── system.log   <-- The complete system log
    |   |   |   ├── scenario.osc     <-- The scenario used within this test
    |   |   |   ├── scenario.variant <-- The parameter set used within this run
    |   |   |   ├── run.yaml         <-- Details about the run (e.g. RUN_ID)
    |   |   |   ├── test.xml         <-- Scenario result, in junitxml format
    |   |   |   ├── <test-specifics> <-- Any test-specific files, stored during the test run within /out (e.g. rosbag)


Analysis
^^^^^^^^
As result analysis is tailored to each test, users are expected to implement their own analysis routines.

To simplify this process, RoboVAST provides the ``result_analyzer`` tool, which enables users to execute Jupyter notebooks directly from a graphical interface.

Analysis configuration is specified in the ``analysis`` section of the ``.vast`` configuration file.

.. literalinclude:: ../examples/growth_sim/growth_sim.vast
   :language: yaml
   :lines: 2-6
   :caption: Analysis section of RoboVAST Configuration File

Although this example includes only one entry in the analysis list, you can add more. Each additional entry will appear as a separate tab in the ``result_analyzer`` interface.

There are three reserved keys for analysis: ``single_test``, ``variant``, and ``run``. These allow you to specify Jupyter notebooks for different scopes:

- **single_test**: analyzes an individual test run.
- **variant**: analyzes all test runs for a specific variant or parameter set.
- **run**: analyzes all tests within a run, covering all variants and parameter sets.

You are free to implement the notebooks as needed. The only requirement is that each notebook includes the following line:

.. code-block:: python

   DATA_DIR = ''

During execution within the ``result_analyzer`` the content of ``DATA_DIR`` is replaced by the currently selected test-directory.

To improve usability the output of the jupyter-notebook-execution is cached and once it was generated it will be displayed instantly.
