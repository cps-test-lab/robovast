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

    vast exec local prepare-run --config config1 ./test_run

Afterwards you can verify the scenario, the RoboVAST-configuration and the docker image.

.. code-block:: bash

    # execute a basic run
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

    vast exec local run --config config1 ./test_out

    # check that output is created in ./test_out/<campaign-name>-<timestamp>/<config-name>/<run_number>
    ls -l ./test_out/*-*/config1/0/

Once you are satisfied that the scenario and configuration work as expected, you can proceed to the next step.

4. Define Configurations
^^^^^^^^^^^^^^^^^^^^^^^^

Define configurations in your ``.vast`` file.
A good procedure is to add configurations one-by-one and analyze the result.

.. code-block:: bash

    # 1. add configuration in config file

    # 2. list created configurations
    vast config list

    # 3. try local execution with one of the created configurations
    vast exec local run --config <config-name> --runs 1 ./test_out

5. Execute in Cluster
^^^^^^^^^^^^^^^^^^^^^

Once you have defined your configurations and verified local execution, you can run the tests in a Kubernetes cluster.

A good practice is, to first run a single configuration to verify that everything works as expected. 


.. code-block:: bash

    # 1. run single configuration in cluster, once
    vast exec cluster run --config config1 --runs 1

    # 2. check results
    vast exec cluster download
    # Results are organized as: <results-dir>/<campaign-name>-<timestamp>/<config-name>/<run_number>/
    find ./results/

For long-running tests, you can use detached mode to run jobs in the background:

.. code-block:: bash

    # Run in detached mode (command exits after creating jobs)
    vast exec cluster run --detach
    
    # Monitor job status (shows progress per run when multiple runs are active)
    vast exec cluster monitor
    
    # Clean up after jobs complete (all campaigns, or use --campaign for a specific campaign)
    vast exec cluster run-cleanup

By default, a new run does not clean up previous runs, so you can run multiple
runs in parallel. Use ``--cleanup`` to remove previous runs before starting
(e.g. ``vast exec cluster run --cleanup``).

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

RoboVAST provides a GUI for analyzing run results, which is based on user-provided Jupyter notebooks.

To develop the notebooks, it is recommended to use e.g. VSCode. For the RoboVAST GUI to work, it is expected to contain a ``DATA_DIR`` definition. The RoboVAST GUI will replace this line with the actual path to the results directory. During development you can set this variable manually to point to your results directory.

.. code-block:: python

    # for single-run (specific run of a configuration)
    DATA_DIR = '<path-to-your-results-directory>/<campaign-name>-<timestamp>/<config-name>/<run_number>'
    # for configuration (all configurations)
    DATA_DIR = '<path-to-your-results-directory>/<campaign-name>-<timestamp>/<config-name>'
    # for complete run
    DATA_DIR = '<path-to-your-results-directory>/<campaign-name>-<timestamp>'

In case you are using ROS bags as output format, it is recommended to postprocess the results before analysis. This can be done with the postprocessing commands defined in the configuration file. RoboVAST provides several conversion scripts for common use-cases.

Postprocessing is cached based on the results directory hash. To bypass the cache and force postprocessing (e.g., after updating postprocessing scripts), use the ``--force`` or ``-f`` flag:

Afterwards you can start the GUI:

.. code-block:: bash

    vast results postprocess
    # or, to force postprocessing even if results are unchanged:
    vast results postprocess --force
    vast evaluation gui


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


.. _extending-metadata-processing:

Add Metadata Processing Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Metadata processing plugins run after the generic and variation-plugin metadata
phases and can modify the ``metadata.yaml`` produced for each campaign.  They are
configured in the ``.vast`` file under ``results_processing.metadata_processing``:

.. code-block:: yaml

   results_processing:
     metadata_processing:
       - my_metadata_plugin
       - my_metadata_plugin:
           param1: value1
           param2: value2

Each plugin must subclass ``robovast.common.metadata.MetadataProcessor`` and
implement the ``process_metadata`` method:

.. code-block:: python

   from pathlib import Path
   from robovast.common.metadata import MetadataProcessor

   class MyMetadataPlugin(MetadataProcessor):

       def process_metadata(self, metadata: dict, campaign_dir: Path) -> dict:
           # Modify metadata as needed
           metadata["custom_field"] = "custom_value"
           return metadata

Register the plugin in your package's ``pyproject.toml``:

.. code-block:: toml

   [tool.poetry.plugins."robovast.metadata_processing"]
   my_metadata_plugin = "my_package.metadata:MyMetadataPlugin"


.. _extending-variation-metadata:

Add Variation Plugin Metadata Hook
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The ``Variation`` base class defines an overridable classmethod that returns an
empty dict by default.  Subclasses implement it to attach domain-specific metadata
to each configuration entry in ``metadata.yaml``:

.. code-block:: python

   from pathlib import Path
   import yaml
   from robovast.common.variation import Variation

   class MyVariation(Variation):

       @classmethod
       def collect_config_metadata(cls, config_entry, config_dir: Path,
                                    campaign_dir: Path) -> dict:
           """Load extra metadata from a YAML sidecar in _config/."""
           data_file = config_dir / "_config" / "my_data.yaml"
           if data_file.exists():
               with open(data_file) as f:
                   return {"my_data": yaml.safe_load(f)}
           return {}

``collect_config_metadata`` is called once per configuration that used the
variation and returns a dictionary that is merged into the configuration's
metadata entry.


.. _extending-prov-metadata:

Add PROV-O Provenance Hook to a Variation Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Variation plugins can contribute domain-specific nodes to the campaign's
PROV-O provenance graph by overriding ``collect_prov_metadata`` on the
``Variation`` base class.  The default implementation returns ``None``
(no contribution).

This hook is the right place for provenance that is tightly coupled to a
specific variation — for example, a floorplan generation variation knows
which map and mesh files it produced and can declare their lineage in the
graph.

**Return type:** ``ProvContribution`` (or ``None`` to contribute nothing):

.. code-block:: python

   from robovast.common.variation import Variation, ProvContribution

   class MyVariation(Variation):

       @classmethod
       def collect_prov_metadata(
           cls,
           config_entry: dict,
           campaign_namespace,   # rdflib.Namespace for the campaign
           config_namespace,     # rdflib.Namespace for this config
           gen_activity_id: str, # IRI of the config-generation activity
       ):
           """Contribute domain-specific PROV-O nodes."""
           from rdflib import PROV, Namespace

           _ID, _TYPE = "@id", "@type"
           MY_NS = Namespace("https://example.org/metamodels/")

           config_cfg = config_entry.get("config", {})
           my_file = config_cfg.get("my_output_file", "")
           if not my_file:
               return None

           file_iri = config_namespace[my_file]

           return ProvContribution(
               # Extra graph nodes (entities, activities) appended to @graph
               graph_nodes=[{
                   _ID: file_iri,
                   _TYPE: PROV["Entity"],
                   "wasGeneratedBy": gen_activity_id,
                   MY_NS["someProperty"]: "value",
               }],
               # Properties merged onto the concrete scenario node
               scenario_properties={MY_NS["outputCount"]: 1},
               # IRIs that each run activity should declare as "used"
               run_used_iris=[file_iri],
           )

``ProvContribution`` fields:

``graph_nodes``
   List of JSON-LD node dictionaries appended to the PROV ``@graph``.  Use
   ``rdflib.PROV``, ``rdflib.DCTERMS``, or your own ``Namespace`` objects
   as keys/values.

``scenario_properties``
   Dict merged onto the *concrete scenario* entity node for this
   configuration.  Useful for adding counts or classification properties
   (e.g. number of goals, number of obstacles).

``run_used_iris``
   List of IRIs that every run activity in this configuration will
   declare as ``prov:used``.  Typically the IRIs of entities generated
   by this variation that are consumed at runtime (e.g. a map file, a
   mesh file).

.. note::

   ``collect_prov_metadata`` receives ``rdflib.Namespace`` objects
   (``campaign_namespace``, ``config_namespace``) so you can construct
   campaign-relative IRIs with ``campaign_namespace["some/path"]``.
   ``rdflib`` is a required dependency of the core ``robovast`` package.


.. _extending-postprocessing:

Add Postprocessing Command Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Postprocessing plugins are Python functions that process run result directories (e.g., convert rosbag data to CSV). They are registered as entry points and executed before analysis.

**Return value:** A plugin must return ``(success: bool, message: str)``. It may optionally return a third value, a list of **provenance entries**, so that each produced file is recorded (e.g. which CSV was created from which rosbag). Each entry is a dict with keys: ``output`` (path relative to results_dir), ``sources`` (list of paths), ``plugin`` (plugin name), ``params`` (optional dict). If returned, these entries are merged and written into ``postprocessing.yaml`` in each run folder (``<campaign-name>-<timestamp>/<config>/<run-number>/``).

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
            results_dir: Path to the <campaign-name>-<timestamp> run directory to process
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

.. _extending-publication:

Add Publication Plugin
^^^^^^^^^^^^^^^^^^^^^^

Publication plugins package or distribute the results directory after
postprocessing.  They are plain callables (functions or class instances) that
operate on the full results directory.

**Return value:** A plugin must return ``(success: bool, message: str)``.

**Creating a Publication Plugin:**

.. code-block:: python

    from typing import Optional, Tuple

    def my_publication_plugin(
        results_dir: str,
        config_dir: str,
        destination: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Upload results to a remote storage location.

        Args:
            results_dir: Path to the results directory (parent of campaign directories).
            config_dir: Directory containing the .vast config file; relative
                paths should be resolved from here.
            destination: Remote destination URL or path.

        Returns:
            Tuple of (success, message).
        """
        import subprocess
        dest = destination or "s3://my-bucket/results/"

        result = subprocess.run(
            ["aws", "s3", "sync", results_dir, dest],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"Upload failed: {result.stderr}"
        return True, f"Uploaded results to {dest}"

**Register in pyproject.toml:**

.. code-block:: toml

    [tool.poetry.plugins."robovast.publication_plugins"]
    my_publication_plugin = "your_package.publication_plugins:my_publication_plugin"

**Usage in .vast config:**

.. code-block:: yaml

    results_processing:
      publication:
        - my_publication_plugin:
            destination: s3://my-bucket/results/

Add Cluster Config Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^

To add a new cluster configuration option for RoboVAST, create a class that inherits from `robovast.execution.cluster_config.base.BaseConfig`.
Register your cluster config in your `pyproject.toml` under `[tool.poetry.plugins."robovast.cluster_configs"]`. The key is the name used to select the configuration, and the value is the import path to your configuration class.

.. code-block:: toml

    [tool.poetry.plugins."robovast.cluster_configs"]
    "YourClusterConfig" = "robovast_<yourplugin>.your_cluster_config:YourClusterConfig"

To test your cluster configuration, you can use:

.. code-block:: bash

    vast exec cluster prepare-setup --cluster-config YourClusterConfig ./setup_output

The output directory will contain all necessary files and instructions to manually execute the setup steps for your cluster configuration and execution.
