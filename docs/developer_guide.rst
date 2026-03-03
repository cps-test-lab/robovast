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

To enable GUI visualization (e.g. RViz) for local runs while keeping cluster runs headless, add ``execution.local.parameter_overrides`` in your ``.vast`` file (see :doc:`configuration`).

Next, it is important to verify that the output (e.g. ROS bag) is stored correctly. 

.. code-block:: bash

    vast execution local run --config config1 ./test_out

    # check that output is created in ./test_out/run-<timestamp>/<config-name>/<test_number>
    ls -l ./test_out/run-*/config1/0/

Once you are satisfied that the scenario and configuration work as expected, you can proceed to the next step.

Interactive Development with VSCode DevContainer
"""""""""""""""""""""""""""""""""""""""""""""""""

As an alternative to ``prepare-run`` / ``run.sh``, you can open the exact same container environment as a **VSCode Dev Container**.
This gives you an IDE with a terminal, Python/ROS2 IntelliSense, and the ability to edit test files live — all without any local ROS2 installation.

.. code-block:: bash

    vast execution local setup-devcontainer

The command reads your ``.vast`` configuration, generates all required scenario files, and writes a ``.devcontainer/`` directory next to your ``.vast`` file:

.. code-block:: text

    .devcontainer/
    ├── devcontainer.json      ← VSCode devcontainer configuration
    ├── docker-compose.yml     ← container definition (image, mounts, user, secondaries)
    └── config/                ← generated /config directory (mounted at /config in container)
        ├── entrypoint.sh
        ├── secondary_entrypoint.sh
        ├── scenario.osc
        ├── scenario.config
        ├── configurations.yaml
        └── restart_<name>.sh  ← one per secondary container (if any)

Files listed in ``test_files_filter`` (e.g. maps, launch files, your system-under-test) are **not** copied — they are bind-mounted directly from the host so any edits you make in VSCode take effect immediately inside the container.

**Secondary containers**

If your ``.vast`` configuration defines ``execution.secondary_containers``, they are added as additional services in ``docker-compose.yml`` and start automatically alongside the devcontainer.
Each secondary runs ``/config/secondary_entrypoint.sh`` (which launches ``scenario_execution_server_ros`` on a shared Unix socket), mirroring the production local-run setup.

To restart a secondary from within the devcontainer terminal:

.. code-block:: bash

    /config/restart_nav.sh        # replace 'nav' with the actual container name

The restart scripts call ``docker restart`` via the Docker socket, which is mounted read-only into the main container. The host user must be a member of the ``docker`` group for this to work.

**Workflow:**

1. Run ``vast execution local setup-devcontainer`` from your project directory.
2. Open the project folder in VSCode.
3. When prompted, click **Reopen in Container** (or use the command palette: *Dev Containers: Reopen in Container*).
4. Every new terminal automatically sources the ROS2 environment (``/opt/ros/$ROS_DISTRO/setup.bash`` and ``/ws/install/setup.bash``).
5. The workspace folder inside the container is ``/config``. From there you can, for example, run the scenario manually:

   .. code-block:: bash

       ros2 run scenario_execution_ros scenario_execution_ros \
           -o /out /config/scenario.osc \
           --scenario-parameter-file /config/scenario.config

**Options:**

- ``--config <name>`` — select a specific configuration (defaults to the first one).
- ``--no-gui`` — omit X11/display mounts (useful for headless environments).
- ``--force`` — overwrite an existing ``.devcontainer/`` directory.

.. note::

    Re-run ``vast execution local setup-devcontainer --force`` whenever you change the ``.vast`` configuration (e.g. add a new variation) to regenerate the ``config/`` files.

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
    
    # Monitor job status (shows progress per run when multiple runs are active)
    vast execution cluster monitor
    
    # Clean up after jobs complete (all runs, or use --run-id for a specific run)
    vast execution cluster run-cleanup

By default, a new run does not clean up previous runs, so you can run multiple
runs in parallel. Use ``--cleanup`` to remove previous runs before starting
(e.g. ``vast execution cluster run --cleanup``).

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

In case you are using ROS bags as output format, it is recommended to postprocess the results before analysis. This can be done with the postprocessing commands defined in the configuration file. RoboVAST provides several conversion scripts for common use-cases.

Postprocessing is cached based on the results directory hash. To bypass the cache and force postprocessing (e.g., after updating postprocessing scripts), use the ``--force`` or ``-f`` flag:

Afterwards you can start the GUI:

.. code-block:: bash

    vast analysis postprocess
    # or, to force postprocessing even if results are unchanged:  
    vast analysis postprocess --force
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


.. _extending-postprocessing:

Add Postprocessing Command Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Postprocessing plugins are Python functions that process test result directories (e.g., convert rosbag data to CSV). They are registered as entry points and executed before analysis.

**Return value:** A plugin must return ``(success: bool, message: str)``. It may optionally return a third value, a list of **provenance entries**, so that each produced file is recorded (e.g. which CSV was created from which rosbag). Each entry is a dict with keys: ``output`` (path relative to results_dir), ``sources`` (list of paths), ``plugin`` (plugin name), ``params`` (optional dict). If returned, these entries are merged and written into ``postprocessing.yaml`` in each test folder (``run-<id>/<config>/<test-number>/``).

**Provenance for container scripts:** Plugins that run scripts inside Docker (e.g. via ``docker_exec.sh``) cannot return data directly. The orchestrator passes a **provenance file** path to each plugin (optional kwarg ``provenance_file``). Container-invoking plugins must pass this to ``docker_exec.sh`` as ``--provenance-file HOST_PATH``; ``docker_exec.sh`` mounts the directory at ``/provenance`` in the container and the script receives ``--provenance-file /provenance/<basename>``. The script should write a JSON file at that path with format ``{"entries": [{"output": "...", "sources": [...], "plugin": "...", "params": {}}]}`` (paths relative to the results/input directory). Use the helper ``write_provenance_entry`` from ``rosbags_common`` (same directory as the scripts, so it works in the container) to append entries; the script gets the path from ``--provenance-file`` and uses its own plugin name when calling the helper.

**Creating a Postprocessing Plugin:**

.. code-block:: python

    from typing import Tuple, Optional, List
    
    def my_postprocessing_command(
        results_dir: str,
        config_dir: str,
        custom_param: Optional[str] = None,
        provenance_file: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Convert custom data to CSV.
        
        Args:
            results_dir: Path to the run-<id> directory to process
            config_dir: Config file directory (for resolving relative paths)
            custom_param: Optional custom parameter
            provenance_file: Optional path for provenance JSON (for container scripts)
        
        Returns:
            Tuple of (success, message) or (success, message, provenance_entries)
        """
        import subprocess
        import os
        
        script = os.path.join(config_dir, "tools/script.sh")
        cmd = [script, results_dir]
        if custom_param:
            cmd.extend(["--param", custom_param])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return False, f"Failed: {result.stderr}"
        return True, "Success"

**Register in pyproject.toml:**

.. code-block:: toml

    [tool.poetry.plugins."robovast.postprocessing_commands"]
    my_postprocessing_command = "your_package.postprocessing_plugins:my_postprocessing_command"

**Usage in .vast config:**

.. code-block:: yaml

    analysis:
      postprocessing:
        - my_postprocessing_command:
            custom_param: value

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
