How to run
==========

The overall workflow in RoboVAST consists of three main steps: 

**Variation** → **Execution** → **Analysis**

For each step, RoboVAST provides dedicated tools to facilitate the process, which are described below. For detailed overall workflow instructions, please refer to the :doc:`example`.

The complete configuration is defined within a single file with ending ``.vast``.

Variation
---------

Generate Variants
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    usage: generate_variants [-h] --config CONFIG --output OUTPUT

    Generate test variants.

    options:
      -h, --help            show this help message and exit
      --config CONFIG       Path to .vast configuration file
      --output OUTPUT, -o OUTPUT
                            Output directory for generated scenarios variants and files


Execution
---------

Cluster Execution
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    usage: cluster_execution [-h] --config CONFIG [--variant VARIANT]

    Run all variants as jobs in Kubernetes.

    options:
       -h, --help         show this help message and exit
       --config CONFIG    Path to .vast configuration file
       --variant VARIANT  Run only a specific variant by name

Local Execution
^^^^^^^^^^^^^^^

.. code-block:: bash

    usage: execute_local [-h] --config CONFIG --output OUTPUT --variant VARIANT [--debug] [--shell]

    Execute scenario variant.

    options:
      -h, --help            show this help message and exit
      --config CONFIG       Path to .vast configuration file
      --output OUTPUT, -o OUTPUT
                            Output directory of the execution
      --variant VARIANT, -v VARIANT
                            Variant to execute
      --debug, -d           Enable debug output
      --shell, -s           Instead of running the scenario, login with shell


Analysis
--------

Result Analyzer
^^^^^^^^^^^^^^^

.. code-block:: bash

   usage: result_analyzer [-h] --results-dir RESULTS_DIR --config CONFIG

   Test Results Analyzer GUI

   options:
      -h, --help            show this help message and exit
      --results-dir RESULTS_DIR
                            Directory containing test results
      --config CONFIG       Path to .vast configuration file

