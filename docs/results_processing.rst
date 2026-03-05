.. _results-processing:

Results Processing
==================

Every RoboVAST execution produces a results directory with a well-defined layout.
This page documents the output structure and how to postprocess and merge results
using the ``vast results`` command group.


.. _results-output-structure:

Output Structure
----------------

The results directory path is configured during ``vast init`` and stored in
the ``.robovast_project`` file.

Top-Level Layout
^^^^^^^^^^^^^^^^

.. code-block:: text

   <results-dir>/
   └── campaign-<timestamp>/                 # One per execution (e.g. campaign-2026-03-04-152130)
       ├── metadata.yaml                     # Campaign metadata (auto-generated)
       ├── _config/                          # Campaign-level configuration snapshot
       ├── _execution/                       # Execution metadata
       ├── _transient/                       # Intermediate/preprocessed data
       └── <config-name-1>/                  # One directory per configuration variant
       └── <config-name-2>/

Campaign-Level Directories
^^^^^^^^^^^^^^^^^^^^^^^^^^^

``_config/`` — Configuration Snapshot
""""""""""""""""""""""""""""""""""""""

A copy of all input files used during execution. This folder can also be used to trigger another
execution with the same configuration by running:

.. code-block:: text

   vast init <campaign-dir>/_config/<config-name>.vast
   vast execution cluster run

The structure inside is domain-specific, but typically includes:

.. code-block:: text

   _config/
   ├── <name>.vast                            # The .vast configuration used
   ├── scenario.osc                           # OpenSCENARIO scenario file
   ├── analysis/                              # Jupyter notebooks for analysis
   │   ├── analysis_run.ipynb
   │   ├── analysis_config.ipynb
   │   └── analysis_campaign.ipynb
   └── <run-files defined within vast-config> # e.g. launch files, models, scripts, parameters

``_execution/`` — Execution Metadata
"""""""""""""""""""""""""""""""""""""

.. code-block:: text

   _execution/
   └── execution.yaml

Contains:

- ``execution_time``: ISO timestamp of when the execution started
- ``robovast_version``: Git commit hash of the robovast version used
- ``runs``: Number of runs per configuration
- ``execution_type``: ``cluster`` or ``local``
- ``image``: Docker image with SHA digest
- ``cluster_info``: Node count, labels, CPU manager policies (cluster only)

``_transient/`` — Intermediate Data
"""""""""""""""""""""""""""""""""""""

.. code-block:: text

   _transient/
   ├── configurations.yaml                   # Fully resolved configuration parameters
   ├── entrypoint.sh                         # Generated container entrypoint script
   ├── secondary_entrypoint.sh               # Generated secondary container entrypoint script
   └── collect_sysinfo.py                    # System info collection script

``configurations.yaml`` contains the fully resolved parameter values for every
configuration variant, including internal computed fields like navigation path waypoints
(``_path``), raster points (``_raster_points``), resolved file paths, and
``_variations`` (list of applied variation plugins with name, start time, duration,
and any plugin-specific fields).

Configuration Directory
^^^^^^^^^^^^^^^^^^^^^^^

Each configuration variant gets its own directory:

.. code-block:: text

   <config-name>/
   ├── _config/
   │   ├── config.yaml                       # Configuration identifier hashes
   │   ├── scenario.config                   # Resolved parameter values (YAML)
   │   ├── maps/                             # [navigation only]
   │   │   ├── <name>.pgm                    # 2D occupancy grid image
   │   │   └── <name>.yaml                   # Map metadata (resolution, origin, thresholds)
   │   └── 3d-mesh/                          # [navigation only]
   │       ├── <name>.stl                    # 3D environment mesh
   │       └── <name>.stl.yaml               # Mesh metadata
   ├── _transient/                           # Per-config intermediate files
   └── <run-number>/                         # 0, 1, 2, ... (one per run)

``scenario.config`` contains the actual scenario parameter values used for this
configuration, wrapped in a single key matching the scenario name:

.. code-block:: yaml

   test_scenario:
     growth_rate: 0.5
     initial_population: 50

Run Directory
^^^^^^^^^^^^^

Each run directory contains all output from a single execution:

.. code-block:: text

   <run-number>/
   ├── test.xml                              # JUnit test result (pass/fail, duration)
   ├── sysinfo.yaml                          # Hardware info (platform, CPU, memory)
   ├── logs/                                 # Log files
   │   ├── system.log                        # Main system log
   │   ├── system_<secondary>.log            # Secondary container log [if multi-container]
   │   └── <ros log files>.log               # ROS log files [if ROS-based]
   ├── resource_usage_*.csv                  # Per-container CPU/memory [if multi-container]
   ├── rosbag2/                              # ROS bag data [if ROS-based]
   │   ├── metadata.yaml                     # Topics, message counts, duration
   │   └── *.mcap                            # Binary bag files (MCAP format)
   └── <test-specific files>                 # Domain-specific output (e.g. out.csv)

``test.xml`` — JUnit Test Result
"""""""""""""""""""""""""""""""""

Standard JUnit XML format with scenario execution results:

.. code-block:: xml

   <testsuite errors="0" failures="0" name="scenario_execution" tests="1" time="49.03">
     <testcase classname="tests.scenario" name="test_scenario" time="49.03">
       <properties>
         <property name="start_time" value="1772634122.583653"/>
       </properties>
     </testcase>
   </testsuite>

``resource_usage_*.csv`` — Resource Usage
""""""""""""""""""""""""""""""""""""""""""

Per-container CSV files with columns: ``timestamp``, ``pid``, ``name``,
``cpu_usage``, ``mem_usage``.  One file per container (e.g.
``resource_usage_nav.csv``, ``resource_usage_simulation.csv``,
``resource_usage_robovast.csv``).  Only present when secondary containers
are configured.

``rosbag2/`` — ROS Bag Data
"""""""""""""""""""""""""""""

Standard ROS 2 bag format (MCAP storage).  The ``metadata.yaml`` file
lists all recorded topics with message types and counts.  Only present
in ROS-based campaigns.


.. _results-metadata:

``metadata.yaml`` — Campaign Metadata
--------------------------------------

Every campaign directory contains a ``metadata.yaml`` file that is
automatically generated after postprocessing completes.  It aggregates
structural and domain-specific metadata about the entire campaign into a
single file.

The file is produced by a three-phase pipeline:

1. **Generic metadata** — collected by ``MetadataGenerator``
   (``robovast.common.metadata``).  This includes configurations, test
   results (pass/fail, timing, output files, sysinfo), execution metadata,
   run files, and the scenario file reference.

2. **Variation-plugin metadata** — each variation plugin used during
   configuration generation can contribute additional metadata by overriding
   the ``collect_config_metadata`` classmethod
   defined on the ``Variation`` base class.  For example,
   ``FloorplanGeneration`` overrides ``collect_config_metadata`` to load map
   and mesh YAML metadata from ``_config/``.  The ``variations`` field in
   each configuration entry lists all variation plugins that were applied,
   together with their execution timing (``name``, ``started_at`` as ISO
   timestamp, ``duration`` in seconds).

3. **User-defined metadata processors** — custom plugins registered under
   the ``robovast.metadata_processing`` entry-point group and configured
   in the ``.vast`` file (see below).

Example structure of ``metadata.yaml``:

.. code-block:: yaml

   configurations:
     - name: config-1
       config:
         growth_rate: 0.5
         initial_population: 100
       config_files: []
       created_at: '2026-03-04T16:15:03.212496'
       variations:
         - name: FloorplanGeneration
           started_at: '2026-03-04T16:14:55.123456+00:00'
           duration: 3.217
         - name: PathVariationRandom
           started_at: '2026-03-04T16:14:58.340789+00:00'
           duration: 1.842
       test_results:
         - dir: config-1/0
           success: 'true'
           start_time: '2026-03-04T16:16:00+00:00'
           end_time: '2026-03-04T16:16:49'
           output_files:
             - config-1/0/sysinfo.yaml
             - config-1/0/logs/system.log
           sysinfo: { ... }
           postprocessing: {}
   metadata: {}
   run_files:
     - _config/files/growth_sim.py
   scenario_file: scenario.osc
   execution:
     execution_time: '2026-03-04T16:15:02'
     robovast_version: abc123
     runs: 2
     execution_type: cluster
     image: ghcr.io/example:latest

Metadata Processing Plugins
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

User-defined metadata processing plugins can modify the metadata
dictionary after the generic and variation-plugin phases.  They are
configured in the ``.vast`` file under ``results_processing.metadata_processing``:

.. code-block:: yaml

   results_processing:
     metadata_processing:
       - my_metadata_plugin
       - my_metadata_plugin:
           param1: value1
           param2: value2

Each plugin must subclass ``robovast.common.metadata.MetadataProcessor``
and implement the ``process_metadata`` method:

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

Variation Plugin Metadata Hooks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The ``Variation`` base class defines two overridable classmethods that
return an empty dict by default.  Subclasses implement them to attach
domain-specific metadata:

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
variation and returns a dictionary that is merged into the configuration's metadata entry.


.. _results-postprocessing:

Postprocessing
--------------

Postprocessing transforms raw run output (e.g. ROS bags, custom binary files) into
analysis-friendly formats (e.g. CSV).  Commands are defined in the
``results_processing.postprocessing`` section of the ``.vast`` file and executed by plugins
(see :ref:`extending-postprocessing` for how to write your own).

.. code-block:: bash

   vast results postprocess [OPTIONS]

**Options**

.. option:: -r, --results-dir PATH

   Directory containing the run results (parent of ``campaign-*`` folders).
   When omitted the value configured with ``vast init`` is used.

.. option:: -f, --force

   Bypass the postprocessing cache and re-run all commands even if the
   results directory has not changed since the last postprocessing run.

.. option:: -o, --override VAST_FILE

   Use the given ``.vast`` file instead of the one stored in
   ``campaign-<id>/_config/``.  See :ref:`results-override` for details.

Postprocessing is **cached** by a hash of the results directory.  When the
directory is unchanged the step is skipped automatically.  Use ``--force`` (or
``-f``) to bypass the cache, for example after updating a postprocessing script:

.. code-block:: bash

   vast results postprocess --force


.. _results-merge:

Merging Results
---------------

.. code-block:: bash

   vast results merge-results MERGED_CAMPAIGN_DIR [OPTIONS]

Merges campaign-dirs with identical configs into one ``merged_campaign_dir``.
Groups ``campaign-dir/config-dir`` by ``config_identifier`` from ``config.yaml``.
Run folders (0, 1, 2, …) from all campaigns are renumbered and copied.
Original campaign-dirs are not modified.

**Arguments**

``MERGED_CAMPAIGN_DIR``
   Target directory where the merged campaign will be written.

**Options**

.. option:: -r, --results-dir PATH

   Source directory containing ``run-*`` dirs.  When omitted the value
   configured with ``vast init`` is used.


.. _results-postprocess-commands:

Listing Postprocessing Plugins
-------------------------------

.. code-block:: bash

   vast results postprocess-commands

Lists all available postprocessing command plugins, their descriptions, and
parameters.  Useful for discovering which commands can be used in the
``results_processing.postprocessing`` section of the ``.vast`` file.


.. _results-override:

Using ``--override`` to Supply a Local ``.vast`` File
------------------------------------------------------

By default ``vast results postprocess`` reads the ``.vast`` configuration from the
**campaign snapshot** stored in
``<results-dir>/campaign-<id>/_config/<name>.vast``.  This snapshot is copied
at execution time and may be out of date.

``--override`` (short form ``-o``) lets you point to any ``.vast`` file on disk,
for example your current working copy:

.. code-block:: bash

   # Use a local/updated .vast file
   vast results postprocess --override my_project.vast

**When to use ``--override``**

- You want to apply updated postprocessing scripts to existing results without
  triggering a new execution campaign.
- The results were produced in a different directory and the campaign snapshot
  points to stale paths.
- You want to bypass the snapshot and always use the latest ``.vast`` during
  iterative postprocessing development.

.. note::

   When ``--override`` is supplied, the same ``.vast`` file is used for
   **every** ``campaign-*`` folder found under the results directory.  The
   config directory of the override file (its parent folder) is used to
   resolve relative paths.
