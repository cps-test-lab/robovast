.. _example:

Example
=======

Code is available in :repo_link:`examples/httpd_test`.

TL;DR
-----

To run the example, execute the following command in the root of the RoboVAST repository:

.. code-block:: bash

   # show the variants, that will later be executed according to configuration
   ros2 run variation_utils list_variants \
        --config examples/httpd_test/httpd_test.vast
    
   # execute the tests in the cluster (kubernetes required)
   ros2 run cluster_execution cluster_execution \
        --config examples/httpd_test/httpd_test.vast
    
   # download results from the cluster
   ros2 run cluster_execution download_results \
        --output ./httpd_test_results

   # analyze the results
   ros2 run result_analyzer result_analyzer \
        --config examples/httpd_test/httpd_test.vast \
        --results-dir ./httpd_test_results

Introduction
------------

The overall workflow in RoboVAST consists of three main steps: 

**Variation** → **Execution** → **Analysis**

For each step, RoboVAST provides dedicated tools to facilitate the process. For details on specific tools, please refer to :doc:`how_to_run`.

Test Definition
---------------

In this example, we test an HTTP server defined in :repo_link:`examples/httpd_test/common/server.py`. The server is accessed by a client :repo_link:`examples/httpd_test/common/client.py` that sends HTTP requests and measures response times.

RoboVAST Configuration
----------------------

The central part of RoboVAST is the configuration file, which defines all aspects of a workflow. It has the ending ``.vast`` and is written in YAML format.

In this example we use configuration file :repo_link:`examples/httpd_test/httpd_test.vast`.

The ``settings`` are split into three main sections: ``variation``, ``execution``, and ``analysis``.


``variation``
^^^^^^^^^^^^^

In this example, we vary the parameters of the client. This is defined in the ``variation`` section of the configuration file:

.. code-block:: yaml

   variation:
   - ParameterVariationRandom:
       name: req_timeout
       num_variations: 3
       min: 500
       max: 1000
       type: string
       seed: 1

This configuration has one variation point, which creates 3 variants with random values for the parameter ``req_timeout`` between 500 and 1000 milliseconds. The seed will ensure that the same random values are generated each time the configuration is used.

If multiple variation points are defined, RoboVAST will create variants for all combinations of the defined variation points, using the order of the ``variation`` list. Available variation types are described in :doc:`variation`.

``execution``
^^^^^^^^^^^^^

.. note::

     For the execution, it is expected that the connection to the Kubernetes cluster is set up properly. Follow TODO

Check Generated Variants
""""""""""""""""""""""""

Before starting the execution in the cluster, it is recommended to first check the variants.

.. code-block:: bash

   ros2 run variation_utils list_variants \
        --config examples/httpd_test/httpd_test.vast


Check Result of a Single Execution
""""""""""""""""""""""""""""""""""

.. code-block:: bash

   ros2 run execution execution_utils execute_variant \
        --config examples/httpd_test/httpd_test.vast \
        --variant <variant-name>


Cluster Execution
"""""""""""""""""

.. code-block:: bash

   ros2 run cluster_execution cluster_execution \
        --config examples/httpd_test/httpd_test.vast

Download Results
""""""""""""""""

The output of an execution is stored within the cluster-internal nfs-server and can be downloaded with

.. code-block:: bash

   ros2 run cluster_execution download_results \
        --output ./httpd_test_results

The resulting folder structure looks like this:

.. code-block:: bash

    httpd_test_results/
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


``analysis``
^^^^^^^^^^^^

As the result analysis is very test-specific the analysis itself is left to the user.

For convience there is the ``result_analyzer``, which allows the user to run jupyter-notebooks from within a GUI.

The configuration is done within the ``.vast`` config-file in the section ``settings/analysis``.

.. code-block:: yaml

  analysis:
  - Analysis:
      single_test: analysis/analysis_single_test.ipynb
      variant: analysis/analysis_variant.ipynb
      run: analysis/analysis_run.ipynb

While there is only one entry in the analysis list, it is possible to add more which end up as additional tabs within the ``result_analyzer``.

The three entries with the fixed keys ``single_test``, ``variant``, and ``run`` allows to specify jupyter-notebooks for different levels.

- **single_test**: analyze a single test-run.
- **variant**: analyze all test-runs for a single variant/parameter-set
- **run**: analyze all tests of a run, with all variants/parameter-sets

The implementation of the notebooks is completely up to the user. The only requirement is that the notebook contains the line:

.. code-block:: python

   DATA_DIR=''

During execution within the ``result_analyzer`` the content of ``DATA_DIR`` is replaced by the currently selected test-directory.

To improve usability the output of the jupyter-notebook-execution is cached and once it was generated it will be displayed instantly.
