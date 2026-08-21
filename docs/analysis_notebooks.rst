.. _evaluation:

Analysis Notebooks
==================

An analysis notebook is a plain Jupyter notebook a campaign declares under
``visualization.results.explorer.notebooks``. The :doc:`web UI <web_ui>` Results
**Explorer** executes one per selected tree node — campaign, batch, configuration or run
— server-side, and renders it as HTML.

This page is about writing those notebooks and about the analysis library they read the
campaign's data with. Where they *appear* is the Explorer; see :doc:`web_ui`.

.. _evaluation-notebooks:

Writing Evaluation Notebooks
----------------------------

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

The workload name (``MyAnalysis`` above) becomes the label of its tab in the Explorer, and the
way a link addresses that tab (``?tab=MyAnalysis``). One name is taken: a workload may not be
called **log**, in any casing. The Explorer already appends a built-in **Log** tab to every run,
so a second tab of that name would read the same and say nothing about which is which; the
``.vast`` is rejected rather than the tab bar growing an ambiguity.

The **only hard requirement** is that every notebook contains the line::

   DATA_DIR = ''

When the GUI executes a notebook it replaces this line with the actual path
for the currently selected item.  The output is cached so subsequent views
are instant.


.. _evaluation-self-contained:

Self-Contained Evaluation Notebooks
-----------------------------------

The *self-contained* pattern extends the basic requirement above: the
notebook is written so it can be opened and executed **directly in VS Code
or JupyterLab** (i.e. without the GUI) by setting ``DATA_DIR`` to a real
path, while still remaining fully compatible with the GUI.

The approach
------------

Set ``DATA_DIR`` to a real results directory in the very first code cell:

.. code-block:: python

   # Self-contained: set DATA_DIR to a real path during development.
   # The RoboVAST GUI replaces this line at runtime.
   DATA_DIR = '/path/to/results/dynamic_obstacle-2026-03-04-132444/my-config-1/'

When the GUI runs the notebook it replaces the entire ``DATA_DIR = ...``
line, so the hardcoded path is never used in production.

Recommended first-cell pattern
--------------------------------

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
---------------

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
--------------------------------------

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
----------------------------------------

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
----------------------------------------

- **Interactive development**: run all cells with ``Run All`` in VS Code
  without launching the GUI.
- **No context switching**: tweak a visualization, re-run, inspect -- all in
  one editor window.
- **GUI-compatible**: the notebook works unchanged in the GUI; the hardcoded
  path is simply overwritten at runtime.
- **Reproducible**: the path embedded in ``DATA_DIR`` documents which dataset
  the notebook was last developed against.

Typical development workflow
-----------------------------

1. Run an execution campaign to produce results.
2. Open the relevant ``.ipynb`` file in VS Code.
3. Set ``DATA_DIR`` to the actual campaign/config/run directory.
4. Develop and iterate with **Run All** (or cell-by-cell).
5. Once satisfied, commit the notebook.  The GUI will use it via the
   ``visualization.results.explorer.notebooks`` section of the ``.vast`` file; ``DATA_DIR``
   will be replaced automatically.
6. To share the notebook with colleagues working on the same dataset, leave
   the real ``DATA_DIR`` value in place -- they only need to update the path.

