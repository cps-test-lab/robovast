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

    vast init --config <config.vast> --results-dir <results-dir>

Initialize project with configuration and results directory. Creates a ``.vast_project`` file for subsequent commands.

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

    vast variation generate --output <output-dir>

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

    vast execution local --variant <variant-name> [--debug] [--shell]

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

Result Analyzer GUI
^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   vast analysis gui [--output <results-dir>]

Launch graphical analyzer. Uses project results directory by default.

