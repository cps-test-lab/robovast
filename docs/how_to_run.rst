How to run
==========

The overall workflow in RoboVAST consists of three main steps: 

**Variation** → **Execution** → **Analysis**

All commands are accessed via the unified ``vast`` CLI. For detailed workflow instructions, refer to :doc:`example`.

Configuration is defined in a single ``.vast`` file.

Getting Started
---------------

Initialize Project
^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast init <config.vast>

Initialize project with configuration and results directory. Creates a ``.vast_project`` file for subsequent commands. The optional ``--results-dir`` option defaults to ``results`` if not specified.


Shell Completion
^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast install-completion

Install shell completion for the ``vast`` command.

Variation
---------

List Variants
^^^^^^^^^^^^^

.. code-block:: bash

    vast variation list

List all variants from configuration without generating files.

Generate Variants
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast variation generate <output-dir>

Generate variant configurations and files to output directory.

List Variation Types
^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast variation types

List all available variation types from the config file. Shows the variation types defined under ``variation.variations`` that can be used to create variants.

List Variation Points
^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast variation points

List all possible variation points from the scenario file. Shows the parameters that can be varied according to the scenario configuration.

Execution
---------

Local Execution
^^^^^^^^^^^^^^^

.. code-block:: bash

    vast execution local <variant-name> [--debug] [--shell]

Execute a single variant locally using Docker. Options:

- ``--debug``: Enable debug output
- ``--shell``: Open shell instead of running scenario

Cluster Execution
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast execution cluster [--variant <variant-name>]

Execute all variants (or specific variant) as Kubernetes jobs.

Download Results
^^^^^^^^^^^^^^^^

.. code-block:: bash

    vast execution download [--output <output-dir>] [--force]

Download results from cluster transfer PVC. Options:

- ``--output``: Custom output directory (uses project results dir by default)
- ``--force``: Re-download existing files

Analysis
--------

Preprocess Results
^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   vast analysis preprocess [--results-dir <results-dir>] [--force]

Run preprocessing commands on test results before analysis. Preprocessing commands are defined in the configuration file's ``analysis.preprocessing`` section and are used to transform raw test data (e.g., converting ROS bags to CSV files).

The command:

- Executes preprocessing scripts/commands defined in the ``.vast`` configuration
- Passes the results directory as an argument to each command
- Tracks preprocessing state with a hash-based cache to avoid redundant processing
- Skips execution if preprocessing is already up to date (unless ``--force`` is used)

Options:

- ``--results-dir``: Custom results directory (uses project results dir by default)
- ``--force``: Force preprocessing by skipping cache check and re-running all commands

Example configuration in ``.vast`` file:

.. code-block:: yaml

   analysis:
     preprocessing:
       - tools/rosbags_to_csv.py
       - tools/process_data.sh

Each command receives the results directory path as its final argument.

Result Analyzer GUI
^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   vast analysis gui [--results-dir <results-dir>]

Launch graphical analyzer. Uses project results directory by default.

