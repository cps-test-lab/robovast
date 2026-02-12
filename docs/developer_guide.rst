.. _devguide:

Developer Guide
===============

Test your Robotic Software with RoboVAST
----------------------------------------

RoboVAST is designed to facilitate testing and validation of robotic software systems by generating diverse scenarios and executing them in simulation environments. This guide provides an overview of how to utilize RoboVAST for testing your robotic applications.

1. Containerize your Software
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

As RoboVAST relies on containerization to ensure consistent and reproducible environments, the first step is to create a Docker container for your robotic software.

There are some requirements your container image must fulfill to be compatible with RoboVAST:
- the image must contain scenario-execution package installed in `/ws/install` (which currently is available for ROS2 jazzy)
- the image must be accessible by Kubernetes, e.g. by pushing it to a container registry.

2. Define a Test Scenario
^^^^^^^^^^^^^^^^^^^^^^^^^

Use the examples and the documentation of `scenario-execution <https://cps-test-lab.github.io/scenario-execution/>`_ to create a scenario that tests your robotic software.

Keep in mind, that variations are currently supported for all overwritable scenario parameters as described `here <https://cps-test-lab.github.io/scenario-execution/how_to_run.html#override-scenario-parameters>`_.

To test your scenario locally, you can run:

.. code-block:: bash

    ros2 run scenario_execution_ros scenario_execution <scenario-file> -t

3. Create Initial RoboVAST Configuration
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Create a RoboVAST configuration file, based on the existing examples in the `configs/` directory.
Do not set any configuration, as this will be done in the next step.


.. code-block:: bash

    vast execution local prepare-run --config config1 ./test_run

Afterwards you can verify the scenario, the RoboVAST-configuration and the docker image.

.. code-block:: bash

    # run basic test
    ./test_run/run.sh

    # use different container image
    ./test_run/run.sh --image <your-container-image>

    # analyze issues by using an interactive shell
    ./test_run/run.sh --shell

    # analyze network traffic, by using host network mode
    ./test_run/run.sh --network-host

    # check that a standalone non-GUI environment (like in Kubernetes) works
    ./test_run/run.sh --no-gui

Next, it is important to verify that the output (e.g. ROS bag) is stored correctly. 

.. code-block:: bash

    vast execution local run --config config1 ./test_out

    # check that output is created in ./test_out/run-<timestamp>/<config-name>/<test_number>
    ls -l ./test_out/run-*/config1/0/

Once you are satisfied that the scenario and configuration work as expected, you can proceed to the next step.

4. Define Configurations
^^^^^^^^^^^^^^^^^^^^^^^^

Define configurations in your ``.vast`` file.
A good procedure is to add configurations one-by-one and analyze the result.

.. code-block:: bash

    # 1. add configuration in config file

    # 2. list created configurations
    vast configuration list

    # 3. test local execution with one of the created configurations
    vast execution local run --config <config-name> --runs 1 ./test_out

5. Execute in Cluster
^^^^^^^^^^^^^^^^^^^^^

Once you have defined your configurations and verified local execution, you can run the tests in a Kubernetes cluster.

A good practice is, to first run a single configuration to verify that everything works as expected. 


.. code-block:: bash

    # 1. run single configuration in cluster, once
    vast execution cluster run --config config1 --runs 1

    # 2. check results
    vast execution cluster download
    # Results are organized as: <results-dir>/run-<timestamp>/<config-name>/<test_number>/
    find ./results/

For long-running tests, you can use detached mode to run jobs in the background:

.. code-block:: bash

    # Run in detached mode (command exits after creating jobs)
    vast execution cluster run --detach
    
    # Monitor job status
    kubectl get jobs
    
    # Clean up after jobs complete
    vast execution cluster run-cleanup

Running local container images in minikube
"""""""""""""""""""""""""""""""""""""""""""

To test local container images in a minikube cluster, you can load the image into minikube's Docker environment.

.. code-block:: bash

    # first terminal
    docker run --rm -it --network=host alpine ash -c "apk add socat && socat TCP-LISTEN:5000,reuseaddr,fork TCP:$(minikube ip):5000"

    # second terminal
    ./container/build.sh --push

    # specify the image in your RoboVAST configuration file


6. Analysis
^^^^^^^^^^^

RoboVAST provides a GUI for analyzing test results, which is based on user-provided Jupyter notebooks.

To develop the notebooks, it is recommended to use e.g. VSCode. For the RoboVAST GUI to work, it is expected to contain a ``DATA_DIR`` definition. The RoboVAST GUI will replace this line with the actual path to the results directory. During development you can set this variable manually to point to your results directory.

.. code-block:: python

    # for single-test (specific test of a configuration)
    DATA_DIR = '<path-to-your-results-directory>/run-<timestamp>/<config-name>/<test_number>'
    # for configuration (all configurations)
    DATA_DIR = '<path-to-your-results-directory>/run-<timestamp>/<config-name>'
    # for complete run
    DATA_DIR = '<path-to-your-results-directory>/run-<timestamp>'

In case you are using ROS bags as output format, it is recommended to preprocess the results before analysis. This can be done with the preprocessing commands defined in the configuration file. RoboVAST provides several conversion scripts for common use-cases, e.g., converting ROS bag messages to CSV files or tf-frames to poses.

Afterwards you can start the GUI:

.. code-block:: bash

    vast analysis preprocess
    vast analysis gui


Extending RoboVAST
------------------

Add Variation Plugin
^^^^^^^^^^^^^^^^^^^^

Provide your custom variation type by creating a class that inherits from `robovast.common.variation.Variation`.

To your `pyproject.toml`, add an entry under `[tool.poetry.plugins."robovast.variation_types"]` to register your variation type. The key is the name used in the RoboVAST configuration file, and the value is the import path to your variation class.

.. code-block:: toml

    [tool.poetry.plugins."robovast.variation_types"]
    "YourVariation" = "robovast_<yourplugin>.your_variation:YourVariation"


Add Command-line Plugin
^^^^^^^^^^^^^^^^^^^^^^^

To create a plugin for the `vast` CLI:

1. Create a Click group or command in your package
2. Register it in your `pyproject.toml` under `[tool.poetry.plugins."robovast.cli_plugins"]`
3. The plugin will be automatically discovered and added to the `vast` command

Example plugin registration:

.. code-block:: toml

    [tool.poetry.plugins."vast.plugins"]
    variation = "variation_utils.cli:variation"

Add Cluster Config Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^

To add a new cluster configuration option for RoboVAST, create a class that inherits from `robovast.execution.cluster_config.base.BaseConfig`.
Register your cluster config in your `pyproject.toml` under `[tool.poetry.plugins."robovast.cluster_configs"]`. The key is the name used to select the configuration, and the value is the import path to your configuration class.

.. code-block:: toml

    [tool.poetry.plugins."robovast.cluster_configs"]
    "YourClusterConfig" = "robovast_<yourplugin>.your_cluster_config:YourClusterConfig"

To test your cluster configuration, you can use:

.. code-block:: bash

    vast execution cluster prepare-setup --cluster-config YourClusterConfig ./setup_output

The output directory will contain all necessary files and instructions to manually execute the setup steps for your cluster configuration and execution.
