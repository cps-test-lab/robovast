.. _analysis:

Analysis
========

RoboVAST provides two tightly integrated analysis workflows: **CLI-driven postprocessing** and an
**interactive GUI** based on Jupyter notebooks.  This page covers the full analysis pipeline,
the ``--override`` option for using a local ``.vast`` file, and the *self-contained* notebook
pattern that lets you develop and run notebooks directly in VS Code or JupyterLab without the GUI.

Overview
--------

The typical analysis workflow is:

.. code-block:: bash

   # 1. (optional) Convert raw output (e.g. ROS bags) to CSV
   vast analysis postprocess

   # 2. Open the interactive GUI to run Jupyter notebooks on the results
   vast analysis gui

Both commands read the ``.vast`` configuration from the most recent
``campaign-<id>/_config/`` directory inside your results folder.  Use
``--override`` to supply a different ``.vast`` file (see below).


.. _analysis-postprocessing:

Postprocessing
--------------

Postprocessing transforms raw run output (e.g. ROS bags, custom binary files) into
analysis-friendly formats (e.g. CSV).  Commands are defined in the
``analysis.postprocessing`` section of the ``.vast`` file and executed by plugins
(see :ref:`extending-postprocessing` for how to write your own).

.. code-block:: bash

   vast analysis postprocess [OPTIONS]

**Options**

.. option:: -r, --results-dir PATH

   Directory containing the run results (parent of ``campaign-*`` folders).
   When omitted the value configured with ``vast init`` is used.

.. option:: -f, --force

   Bypass the postprocessing cache and re-run all commands even if the
   results directory has not changed since the last postprocessing run.

.. option:: -o, --override VAST_FILE

   Use the given ``.vast`` file instead of the one stored in
   ``campaign-<id>/_config/``.  See :ref:`analysis-override` for details.

Postprocessing is **cached** by a hash of the results directory.  When the
directory is unchanged the step is skipped automatically.  Use ``--force`` (or
``-f``) to bypass the cache, for example after updating a postprocessing script:

.. code-block:: bash

   vast analysis postprocess --force


.. _analysis-gui:

GUI
---

.. code-block:: bash

   vast analysis gui [OPTIONS]

The GUI automatically runs postprocessing before launching, unless
``--skip-postprocessing`` is specified.

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
   discovery instead of the campaign copy.  See :ref:`analysis-override`.


.. _analysis-override:

Using ``--override`` to Supply a Local ``.vast`` File
------------------------------------------------------

By default ``vast analysis postprocess`` and ``vast analysis gui`` read the
``.vast`` configuration from the **campaign snapshot** stored in
``<results-dir>/campaign-<id>/_config/<name>.vast``.  This snapshot is copied
at execution time and may be out of date.

``--override`` (short form ``-o``) lets you point to any ``.vast`` file on disk,
for example your current working copy:

.. code-block:: bash

   # Postprocessing – use a local/updated .vast file
   vast analysis postprocess --override my_project.vast

   # GUI – use a local/updated .vast file (also passed to postprocessing)
   vast analysis gui --override my_project.vast

**When to use ``--override``**

- You have updated the ``analysis.visualization`` section (e.g. new notebooks,
  changed paths) and want the GUI to pick up the changes immediately without
  re-running the scenario.
- The results were produced in a different directory and the campaign snapshot
  points to stale paths.
- You want to apply updated postprocessing scripts to existing results without
  triggering a new execution campaign.
- During notebook development: point to your working ``.vast`` so the GUI
  always uses the latest notebook paths.

.. note::

   When ``--override`` is supplied, the same ``.vast`` file is used for
   **every** ``campaign-*`` folder found under the results directory.  The
   config directory of the override file (its parent folder) is used to
   resolve relative notebook paths defined under ``analysis.visualization``.


.. _analysis-notebooks:

Writing Analysis Notebooks
--------------------------

Notebooks are plain Jupyter ``.ipynb`` files referenced from the
``analysis.visualization`` section of the ``.vast`` file:

.. code-block:: yaml

   analysis:
     visualization:
       - MyAnalysis:
           run: analysis/analysis_run.ipynb
           config: analysis/analysis_config.ipynb
           campaign: analysis/analysis_campaign.ipynb

There are three reserved scopes:

- **run** – executed once per individual run directory
  (``campaign-<id>/<config>/<run-number>/``).
- **config** – executed once per configuration directory
  (``campaign-<id>/<config>/``).
- **campaign** – executed once per campaign directory
  (``campaign-<id>/``).

The **only hard requirement** is that every notebook contains the line::

   DATA_DIR = ''

When the GUI executes a notebook it replaces this line with the actual path
for the currently selected item.  The output is cached so subsequent views
are instant.


.. _analysis-self-contained:

Self-Contained Analysis Notebooks
----------------------------------

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
   DATA_DIR = '/path/to/results/campaign-2026-03-04-132444/my-config-1/'

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
   DATA_DIR = '/path/to/results/campaign-<id>/<config-name>/'

   try:
       from robovast.common.analysis import read_output_files, read_output_csv
       df = read_output_files(DATA_DIR, lambda d: read_output_csv(d, "poses.csv"))
   except Exception as e:
       print(f"Error reading data: {e}")
       raise SystemExit("No data found – check DATA_DIR.")

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
     - ``…/campaign-<id>/<config>/<run-number>/``
     - ``frame``, ``timestamp``, …
   * - ``config``
     - ``…/campaign-<id>/<config>/``
     - ``run``, ``frame``, ``timestamp``, …
   * - ``campaign``
     - ``…/campaign-<id>/``
     - ``run``, ``config``, ``test``, ``frame``, …

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
- **No context switching**: tweak a visualization, re-run, inspect – all in
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
   ``analysis.visualization`` section of the ``.vast`` file; ``DATA_DIR``
   will be replaced automatically.
6. To share the notebook with colleagues working on the same dataset, leave
   the real ``DATA_DIR`` value in place – they only need to update the path.
