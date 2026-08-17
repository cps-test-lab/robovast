.. _evaluation:

Evaluation
==========

RoboVAST provides an interactive GUI (``vast eval``) based on Jupyter notebooks
for exploration and visualization of scenario execution results.

.. note::

   The :doc:`web UI <web_ui>` Results **Explorer** renders these same
   ``visualization.results.explorer.notebooks`` notebooks per selected node, executed server-side and
   shown as HTML — so the notebooks written below work in both the desktop GUI and the
   browser.


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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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

- You have updated the ``visualization.results.explorer.notebooks`` section (e.g. new notebooks,
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
   resolve relative notebook paths defined under ``visualization.results.explorer.notebooks``.


.. _evaluation-notebooks:

Writing Evaluation Notebooks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Notebooks are plain Jupyter ``.ipynb`` files referenced from the
``visualization.results.explorer.notebooks`` section of the ``.vast`` file:

.. code-block:: yaml

   visualization:
     results:
       explorer:
         notebooks:
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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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

   from robovast.common.analysis import read_table, read_runs
   df = read_table(DATA_DIR, "poses")

.. _evaluation-reading-results:

Reading results
^^^^^^^^^^^^^^^

``read_table`` reads one of the campaign's ``data.db`` tables and **restricts it to what
``DATA_DIR`` selects** — a run directory gives that run's rows, a configuration directory that
configuration's, the campaign root everything. The same cell therefore serves all three
notebook scopes, and no notebook names a file.

.. code-block:: python

   read_table(DATA_DIR, "behaviors")            # the scenario's behaviour tree
   read_table(DATA_DIR, "poses", columns=["timestamp", "position.x", "position.y"])
   read_table(DATA_DIR, "poses", where="frame = ?", params=("base_link",))
   read_runs(DATA_DIR)                          # per-run outcome + each param_* column
   list_tables(DATA_DIR)                        # what this campaign actually has

Tables are keyed ``(config_name, run_id)``; ``runs`` carries the same key, so joining it to a
metric table relates what varied to what happened. ``read_sql(DATA_DIR, ...)`` is the escape
hatch for joins and for the ``run_view`` / ``config_view`` views — it is deliberately *not*
scoped.

What a metric varied *with* is usually the question, so ``with_params=True`` attaches each
scenario parameter of the owning run as a column, with the ``param_`` prefix dropped
(:func:`~robovast.common.analysis.db.attach_params` does the same to a frame you already
have). A parameter whose name collides with a column of the table raises rather than
shadowing it:

.. code-block:: python

   df = read_table(DATA_DIR, "poses", with_params=True)
   df["map_file"]        # the parameter, beside the measurements it belongs to

A parameter that names a *file* — ``map_file``, ``mesh_file`` — holds a path relative to the
campaign's ``_config/``. Resolve it with ``config_file`` rather than joining it onto
``DATA_DIR``, which is only the campaign root at campaign scope:

.. code-block:: python

   config_file(DATA_DIR, df["map_file"].iloc[0])            # raises if it is not there
   config_file(DATA_DIR, rel, config_name, must_exist=False)  # to test existence yourself

**Which tables exist depends on the campaign.** ``runs``, ``behaviors``, ``run_log``,
``resource_usage`` and ``scenario_timestamps`` are produced whatever the simulator and whether
or not the run used ROS. ``poses``, ``costmaps``, ``action_*`` and the ``nav2_*`` tables come
from a rosbag, so a ``mode: base`` campaign has none of them. Ask ``list_tables`` or
``table_info`` rather than assuming, and name the columns an analysis cannot do without:

.. code-block:: python

   # Raises, naming the missing column and what the table does have, rather than
   # returning a frame that is quietly missing it.
   read_table(DATA_DIR, "behaviors", require=["status_name", "tip_id"])

Reading requires postprocessing to have run — ``data.db`` does not exist before it, and a
campaign whose postprocessing failed still reports ``finished``. That case raises with the
remedy in the message rather than falling back to the per-run files, which would answer a
different question with less data. :func:`~robovast.common.analysis.files.read_run_statuses`
reads ``test.xml`` directly and so still works when there is no database at all.

The per-run file readers (:mod:`robovast.common.analysis.files`) remain available for what is
genuinely not in the database.

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
     - What ``read_table`` returns
   * - ``run``
     - ``/<campaign-name>-<timestamp>/<config>/<run-number>/``
     - that run's rows
   * - ``config``
     - ``/<campaign-name>-<timestamp>/<config>/``
     - every run of that configuration
   * - ``campaign``
     - ``/<campaign-name>-<timestamp>/``
     - every run of every configuration

.. note::

   The scope changes which **rows** come back, not which columns: every metric table
   carries ``config_name`` and ``run_id`` at all three levels, so a cell written for one
   scope runs unchanged at another. At run scope both columns hold a single value —
   grouping by them is redundant there but not an error, which is what lets the same
   cell serve every scope.

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
   ``visualization.results.explorer.notebooks`` section of the ``.vast`` file; ``DATA_DIR``
   will be replaced automatically.
6. To share the notebook with colleagues working on the same dataset, leave
   the real ``DATA_DIR`` value in place -- they only need to update the path.

