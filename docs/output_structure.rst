.. _output-structure:

Output Structure
================

Every RoboVAST execution produces a results directory with a well-defined
layout.  This page documents the complete structure.

The results directory path is configured during ``vast init`` and stored in
the ``.robovast_project`` file.

Top-Level Layout
----------------

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
--------------------------

``_config/`` — Configuration Snapshot
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A copy of all input files used during execution. This folder can also be used, to trigger another
execution with the same configuration by running 

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
-----------------------

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
-------------

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Per-container CSV files with columns: ``timestamp``, ``pid``, ``name``,
``cpu_usage``, ``mem_usage``.  One file per container (e.g.
``resource_usage_nav.csv``, ``resource_usage_simulation.csv``,
``resource_usage_robovast.csv``).  Only present when secondary containers
are configured.

``rosbag2/`` — ROS Bag Data
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Standard ROS 2 bag format (MCAP storage).  The ``metadata.yaml`` file
lists all recorded topics with message types and counts.  Only present
in ROS-based campaigns.

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
   the ``collect_config_metadata`` and ``collect_run_metadata`` classmethods
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
configured in the ``.vast`` file under ``analysis.metadata_processing``:

.. code-block:: yaml

   analysis:
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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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

       @classmethod
       def collect_run_metadata(cls, config_entry, run_dir: Path,
                                 campaign_dir: Path) -> dict:
           """Return extra fields to merge into a run's test_results entry."""
           return {}

``collect_config_metadata`` is called once per configuration that used the
variation.  ``collect_run_metadata`` is called once per run directory.
Both return a dictionary that is merged into the respective metadata entry.  

For example, the ``FloorplanGeneration`` variation uses
``collect_config_metadata`` to load map and mesh metadata from the generated
