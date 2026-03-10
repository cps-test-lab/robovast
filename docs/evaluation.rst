.. _evaluation:

Evaluation
==========

RoboVAST provides an interactive GUI (``vast eval``) based on Jupyter notebooks
for exploration and visualization of scenario execution results.


.. _evaluation-gui:

GUI
---

.. code-block:: bash

   vast eval gui [OPTIONS]

Opens a GUI application for interactive exploration and visualization of run results.
Automatically runs postprocessing before launching, unless ``--skip-postprocessing``
is specified.

**Options**

.. option:: -r, --results-dir PATH

   Directory containing the run results.  When omitted the value configured
   with ``vast init`` is used.

.. option:: -f, --force

   Force postprocessing even if the results directory is unchanged.

.. option:: --skip-postprocessing

   Launch the GUI without running postprocessing first.

.. option:: -o, --override VAST_FILE

   Use the given ``.vast`` file for both postprocessing and notebook
   discovery instead of the campaign copy.  See :ref:`evaluation-override`.


.. _evaluation-override:

Using ``--override`` to Supply a Local ``.vast`` File
------------------------------------------------------

By default ``vast eval gui`` reads the ``.vast`` configuration from the
**campaign snapshot** stored in
``<results-dir>/<campaign-name>-<timestamp>/_config/<name>.vast``.  This snapshot is copied
at execution time and may be out of date.

``--override`` (short form ``-o``) lets you point to any ``.vast`` file on disk,
for example your current working copy:

.. code-block:: bash

   # Use a local/updated .vast file
   vast eval gui --override my_project.vast

**When to use ``--override``**

- You have updated the ``evaluation.visualization`` section (e.g. new notebooks,
  changed paths) and want the GUI to pick up the changes immediately without
  re-running the scenario.
- The results were produced in a different directory and the campaign snapshot
  points to stale paths.
- During notebook development: point to your working ``.vast`` so the GUI
  always uses the latest notebook paths.

.. note::

   When ``--override`` is supplied, the same ``.vast`` file is used for
   **every** campaign folder (``<campaign-name>-<timestamp>``) found under the results directory.  The
   config directory of the override file (its parent folder) is used to
   resolve relative notebook paths defined under ``evaluation.visualization``.


.. _evaluation-notebooks:

Writing Evaluation Notebooks
-----------------------------

Notebooks are plain Jupyter ``.ipynb`` files referenced from the
``evaluation.visualization`` section of the ``.vast`` file:

.. code-block:: yaml

   evaluation:
     visualization:
       - MyAnalysis:
           run: analysis/analysis_run.ipynb
           config: analysis/analysis_config.ipynb
           campaign: analysis/analysis_campaign.ipynb

There are three reserved scopes:

- **run** -- executed once per individual run directory
  (``<campaign-name>-<timestamp>/<config>/<run-number>/``).
- **config** -- executed once per configuration directory
  (``<campaign-name>-<timestamp>/<config>/``).
- **campaign** -- executed once per campaign directory
  (``<campaign-name>-<timestamp>/``).

The **only hard requirement** is that every notebook contains the line::

   DATA_DIR = ''

When the GUI executes a notebook it replaces this line with the actual path
for the currently selected item.  The output is cached so subsequent views
are instant.


.. _evaluation-self-contained:

Self-Contained Evaluation Notebooks
-------------------------------------

The *self-contained* pattern extends the basic requirement above: the
notebook is written so it can be opened and executed **directly in VS Code
or JupyterLab** (i.e. without the GUI) by setting ``DATA_DIR`` to a real
path, while still remaining fully compatible with the GUI.

The approach
^^^^^^^^^^^^

Set ``DATA_DIR`` to a real results directory in the very first code cell:

.. code-block:: python

   # Self-contained: set DATA_DIR to a real path during development.
   # The RoboVAST GUI replaces this line at runtime.
   DATA_DIR = '/path/to/results/dynamic_obstacle-2026-03-04-132444/my-config-1/'

When the GUI runs the notebook it replaces the entire ``DATA_DIR = ...``
line, so the hardcoded path is never used in production.

Recommended first-cell pattern
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   import pandas as pd
   import numpy as np
   import matplotlib.pyplot as plt
   import os

   # Set DATA_DIR to a real path for interactive development.
   # The RoboVAST GUI replaces this line automatically.
   DATA_DIR = '/path/to/results/<campaign-name>-<timestamp>/<config-name>/'

   try:
       from robovast.common.analysis import read_output_files, read_output_csv
       df = read_output_files(DATA_DIR, lambda d: read_output_csv(d, "poses.csv"))
   except Exception as e:
       print(f"Error reading data: {e}")
       raise SystemExit("No data found -- check DATA_DIR.")

Handling missing columns defensively
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

When developing against a specific dataset, guard against unexpected
DataFrame schemas so the notebook fails clearly rather than with a cryptic
``KeyError``:

.. code-block:: python

   required_cols = {'run', 'config', 'timestamp', 'frame'}
   missing = required_cols - set(df.columns)
   if missing:
       raise ValueError(f"DataFrame is missing expected columns: {missing}. "
                        f"Available: {list(df.columns)}")

Scoping ``DATA_DIR`` per notebook type
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Use paths appropriate to the *scope* of the notebook:

.. list-table::
   :header-rows: 1
   :widths: 15 55 30

   * - Scope
     - Example ``DATA_DIR``
     - Available columns
   * - ``run``
     - ``/<campaign-name>-<timestamp>/<config>/<run-number>/``
     - ``frame``, ``timestamp``, ...
   * - ``config``
     - ``/<campaign-name>-<timestamp>/<config>/``
     - ``run``, ``frame``, ``timestamp``, ...
   * - ``campaign``
     - ``/<campaign-name>-<timestamp>/``
     - ``run``, ``config``, ``test``, ``frame``, ...

.. note::

   The ``test`` and ``config`` columns are only present when ``DATA_DIR``
   points to a *campaign* or *config* directory that contains **multiple**
   runs.  When ``DATA_DIR`` points to a single run directory those columns
   are absent.  Grouping by ``['test', 'config']`` on a run-level notebook
   will raise a ``KeyError``; always match the notebook scope to its
   ``DATA_DIR`` level.

Benefits of the self-contained pattern
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- **Interactive development**: run all cells with ``Run All`` in VS Code
  without launching the GUI.
- **No context switching**: tweak a visualization, re-run, inspect -- all in
  one editor window.
- **GUI-compatible**: the notebook works unchanged in the GUI; the hardcoded
  path is simply overwritten at runtime.
- **Reproducible**: the path embedded in ``DATA_DIR`` documents which dataset
  the notebook was last developed against.

Typical development workflow
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

1. Run an execution campaign to produce results.
2. Open the relevant ``.ipynb`` file in VS Code.
3. Set ``DATA_DIR`` to the actual campaign/config/run directory.
4. Develop and iterate with **Run All** (or cell-by-cell).
5. Once satisfied, commit the notebook.  The GUI will use it via the
   ``evaluation.visualization`` section of the ``.vast`` file; ``DATA_DIR``
   will be replaced automatically.
6. To share the notebook with colleagues working on the same dataset, leave
   the real ``DATA_DIR`` value in place -- they only need to update the path.
