.. _evaluation:

Analysis Notebooks
==================

An analysis notebook is a plain Jupyter notebook a campaign declares under
``visualization.results.explorer.notebooks``. The :doc:`web UI <web_ui>` Results
**Explorer** executes one per selected tree node — campaign, batch, configuration or run
— server-side, and renders it as HTML.

This page is about writing those notebooks and about ``robovast-data``, the package they read
the campaign's data with. Where they *appear* is the Explorer; see :doc:`web_ui`.

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

There are four reserved scopes, and a notebook declared under any other key is refused --
the renderer keeps only the scopes it can address, so a misspelled one would otherwise leave
the notebook staged and never rendered, with no tab and nothing said:

- **run** -- executed once per individual run directory
  (``<campaign-name>-<timestamp>/<config>/<run-number>/``).
- **config** -- executed once per configuration directory
  (``<campaign-name>-<timestamp>/<config>/``).
- **batch** -- executed once per proposing round of a *search* campaign. A batch has no
  directory of its own, so the notebook receives the campaign root as ``DATA_DIR`` plus an
  injected ``BATCH`` index; the tab appears only for a search campaign.
- **campaign** -- executed once per campaign directory
  (``<campaign-name>-<timestamp>/``).

Every declared path is relative to the ``.vast`` and must exist in the project that is
pushed. ``vast config validate`` reports one that does not: staging skips it with a
warning, and the campaign then runs to completion with a tab that cannot render.

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

   # Set DATA_DIR to a real path for interactive development.
   # The RoboVAST GUI replaces this line automatically.
   DATA_DIR = '/path/to/results/<campaign-name>-<timestamp>/<config-name>/'

   from robovast_data import open_data
   data = open_data(DATA_DIR)
   poses = data.table("poses")

.. _evaluation-reading-results:

Reading results
---------------

A notebook reads a campaign through the ``robovast-data`` package: ``pip install
robovast-data`` from PyPI alone, on any machine with Python -- it needs no ROS installation,
no service and no container image, because the decoder it builds tables with reads a
recording's message definitions from the recording itself. A table is built in a process
pool, so a plain script (not a notebook) does its work under ``if __name__ == "__main__":``.
``open_data(DATA_DIR)`` walks up from the path to the campaign (the directory holding ``campaign.db``) and **scopes
everything to the node the path names** — a run directory gives that run's rows, a
configuration directory that configuration's, the campaign root everything. The same cell
therefore serves all three notebook scopes, and no notebook names a file.

.. code-block:: python

   from robovast_data import open_data

   data = open_data(DATA_DIR)
   data.runs                                   # one row per run: outcome + each param_* column
   data.table("behaviors")                     # the scenario's behaviour tree
   data.table("poses", columns=["timestamp", "frame", "position.x", "position.y"])
   data.table("poses", config="cfg-3", run=0)  # narrower than the node, never wider
   data.table("nav_metrics", with_params=True) # each run's param_* columns beside its rows
   data.tables                                 # what can be read here, and what is built
   data.sql("SELECT config_name, avg(duration_s) AS s FROM runs GROUP BY 1")

``data.runs`` has one row per run with its ``status``, ``passed``, ``duration_s``,
``objective``, the host it ran on (``instance_type``, ``node_label``, ``cpu_name``, ...),
``probed``, ``live`` -- true while the run is still being written, so a number read from its
tables is provisional until it is false -- and one typed ``param_<name>`` column per varied
factor, whichever channel it was written on (:ref:`channel-param-columns`). A unit that produced no run at all — a draw that
could not be composed, a configuration whose results never arrived — is a row with an empty
``run_id``, so a count over ``runs`` includes the coverage that was not obtained.

Every table is keyed ``(config_name, run_id)`` and carries ``campaign_id``; ``runs`` carries
the same key, so joining it to a table relates what varied to what happened.
``with_params=True`` does that join for you and keeps the ``param_`` prefix, so a factor never
shadows a column of the table.

**A table is built the first time it is read.** It is decoded from each run's own recordings
into the campaign's ``.cache/`` directory, and the next read — from this notebook or any other —
reads the cache. A run whose table could not be built is left out of the answer with a warning
naming the table, the run and the reason. A table that no run in scope recorded raises, naming
the tables that do exist. Above five million rows, ``table()`` warns before it reads them all
into memory; narrow it with ``config=`` and ``run=``, or aggregate with ``sql()``.

**Which tables exist depends on the campaign.** ``runs``, ``behaviors`` (scenario_execution's
``behaviors.jsonl``), ``run_log``, ``scenario_timestamps``, ``resource_usage``,
``system_usage`` and ``run_clock`` are there whatever the simulator and whether or not the run
used ROS. ``poses`` (from ``/tf``, following the :ref:`pose contract <pose-contract>`),
``costmaps``, ``nav2_behavior_tree``, ``action_<name>_feedback`` / ``_status`` and
``rosbag2_<topic>`` come from a rosbag, so a ``mode: base`` campaign has none of them;
``sim_poses`` exists where the simulator writes it. Every CSV a run writes is a table named
after its file — ``out.csv`` is ``out``, a postprocessing plugin's ``nav_metrics.csv`` is
``nav_metrics`` — with a leading ``#`` preamble skipped. Ask ``data.tables`` rather than
assuming.

``data.sql()`` takes one DuckDB ``SELECT`` and is scoped exactly as ``table()`` is. Besides
the tables it sees the views ``run_view``, ``config_view``, ``pose_track_view`` (every
recorded pose of every pose table), ``run_validity_view`` and ``container_failure_view``.
``PERCENTILE(value, p)`` takes ``p`` in 0..100 and ``REGEXP(pattern, value)`` is a search.

A parameter that names a *file* — ``map_file``, ``mesh_file`` — holds a path relative to the
configuration's resolved ``_config/``. Read it through the configuration rather than joining
it onto ``DATA_DIR``, which is only the campaign root at campaign scope:

.. code-block:: python

   cfg = data.config("cfg-3")
   cfg.files()                      # every resolved file the configuration ran with
   cfg.yaml("scenario.config")      # parsed; cfg.text(...) for the raw text
   cfg.path                         # the directory, to join a relative path onto

``Campaign(path)`` is the whole campaign whatever node the path names, ``Corpus(glob)`` several
campaigns as one (every row keeps its ``campaign_id``), and a downloaded ``.tar.gz`` opens as
its directory does, extracted beside it on first use. ``read_table(path, name)`` and
``read_runs(path)`` are the one-line forms.

An **export** (:ref:`results-export`, ``vast campaign export <id>``) needs none of this for
its tables: they are already files, one per table, and a parquet export opens with pandas or
DuckDB directly --

.. code-block:: python

   import duckdb
   import pandas as pd

   poses = pd.read_parquet("<campaign>-export-<id>/tables/poses.parquet")
   duckdb.sql("SELECT config_name, count(*) FROM '<campaign>-export-<id>/tables/runs.parquet' GROUP BY 1")

-- while the records it ships beside them (``<campaign_id>/``) are a campaign directory
``Campaign`` opens as it opens the archive.

A campaign on a service opens by its URL, without downloading it:

.. code-block:: python

   c = Campaign("https://<service>/campaigns/<campaign_id>", token="<token>")
   c.runs
   c.table("poses", config="cfg-3", run=0)
   c.sql("SELECT config_name, count(*) FROM poses GROUP BY 1")

The service builds what each call names from the campaign's records, as it does for the web UI,
and sends the answer as CSV. ``vast service token`` prints the token it accepts. Three things
differ from a campaign on disk: the columns are typed by pandas from the CSV, a query takes no
parameters, and ``config()`` is refused, because the service does not serve a configuration's
files through these routes.

Reading a campaign's own record
-------------------------------

The tables hold *measurements*, per run. The controller also keeps a **record** of the
campaign, ``campaign.db``, written as the campaign runs: the campaign row with its resolved
configuration and, for a search, why it stopped; the batches a search proposed; one unit per
configuration with its objectives, its quality-diversity measures and the parameters it was
drawn with; the runs, jobs, nodes and container failures. ``sql()`` reads it as the
``campaign`` schema, and it is the whole campaign's at every scope:

.. code-block:: python

   data.sql("SELECT idx, id FROM campaign.batch ORDER BY idx")
   data.sql("SELECT u.config_name, u.objective, u.measures_json, b.idx AS batch "
            "FROM campaign.unit u JOIN campaign.batch b ON b.id = u.batch_id")
   data.sql("SELECT stop_kind, stop_reason FROM campaign.campaign")

The record's tables are ``campaign.campaign``, ``campaign.batch``, ``campaign.unit``,
``campaign.run``, ``campaign.job``, ``campaign.node`` and ``campaign.container_failure``,
each with the campaign's id as ``campaign_id``. ``objectives_json`` and ``measures_json``
exist **only** on ``campaign.unit`` — ``runs.objective`` lifts just the single scalar — so a
multi-objective or quality-diversity campaign is read there. Reading the record decodes
nothing, so a batch or archive view works on a search still in progress.

Three unit statuses are not results and must not be averaged over: ``composition_failed`` is
a draw that could not be built into a configuration at all and never ran, ``missing`` is a
configuration the campaign was composed with whose results never reached the tree, and
``no_sample`` is one that ran and lost every run to infrastructure. All three are coverage
that was not obtained; count them rather than dropping them, or the campaign reads as having
explored more than it did -- and the first two carry no runs at all, so a query that joins
through ``campaign.run`` drops them without saying so.

The per-run file readers (:mod:`robovast.common.analysis.files`) read a run's files as they
are; :func:`~robovast.common.analysis.files.read_run_statuses`, for one, takes each run's
outcome straight from its ``test.xml``.

Handling missing columns defensively
--------------------------------------

When developing against a specific dataset, guard against unexpected
DataFrame schemas so the notebook fails clearly rather than with a cryptic
``KeyError``:

.. code-block:: python

   required_cols = {'config_name', 'run_id', 'timestamp', 'frame'}
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
     - What ``table()`` returns
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

   The scope changes which **rows** come back, not which columns: every table carries
   ``config_name`` and ``run_id`` at all three levels, so a cell written for one
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

