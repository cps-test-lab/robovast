.. _search:

Iterative Search
================

By default a RoboVAST campaign runs as a **batch**. Adding a ``search:`` block
turns it into an **iterative, closed-loop search**: a strategy proposes parameter
sets, an *extract* step scores them, and the strategy is told the results so it
can propose the next generation. The goal is surfacing failures and
near-failures across the parameter space.

The loop is **algorithm-agnostic** and the config is **uniform across
strategies** — `random`, quality-diversity (`qd`), `optuna`, and future
algorithms share one schema; only the per-strategy ``strategy_parameters`` differ.

.. note::

   Search is **experimental**. ``random`` ships in the base install; ``qd``
   (pyribs) and ``optuna`` need their extras: ``pip install 'robovast[qd]'`` /
   ``pip install 'robovast[optuna]'``.

The generic ``search`` block
----------------------------

A ``search:`` section is self-contained: its configurations are synthesized from
``search_space``, so it is **mutually exclusive** with a ``configuration:`` block
(supplying both is a config-validation error). Every searched parameter must be a
``search_space`` dimension; non-varied parameters fall back to scenario
(``.osc``) defaults.

.. code-block:: yaml

   execution:
     scenario_file: scenario.osc
     runs: 3
     configs_per_job: 1

   search:
     # ---- universal core (every strategy) ----
     strategy: qd               # plugin: entry-point name OR ./search/s.py:Cls
     search_space:              # typed dims: float / int / choice
       thrust_gain:   {type: float, low: 0.3, high: 3.0}
       mass:          {type: int,   low: 1,   high: 3}
       mode:          {type: choice, values: [a, b, c]}
     extract:                   # the one scorer -> {objectives, measures}
       plugin: ./search/extract.py:QuadExtract   # entry-point name OR file ref
       params: {trajectory: trajectory.csv}      # parameterize from the .vast
     objectives:                # what to optimize (>=1 entries)
     - {name: failure_rate, direction: maximize}
     per_step: 16               # parameter sets per generation
     budget: {generations: 20}
     seed: 0
     postprocessing: []         # optional, search-only (NOT results_processing)
     # ---- strategy-specific (one block; the strategy validates it) ----
     strategy_parameters:
       archive: {type: cvt, cells: 512,
                 measures: {max_tilt: {low: 0.0, high: 0.75},
                            drift_dist: {low: 0.0, high: 3.5}}}
       sigma: 0.15

Concepts
--------

**search_space** — a *typed* mapping of parameter path to domain
(``float{low,high,log}`` / ``int{low,high,log,step}`` / ``choice{values}``).
Malformed domains are rejected at config-validation time.

**extract** — the single, SUT-specific scoring step. It reads a parameter set's
per-config results and returns named **objectives** (optimized) and **measures**
(quality-diversity behavior axes). ``plugin`` is an entry-point name (built-in
``failure_rate``) or a **local file relative to the .vast** (``./search/x.py:Cls``);
``params`` is passed to it so thresholds/columns are swept from config without
editing code. The extractor aggregates over a config's runs; the framework records
how many samples backed each result. This is the one place SUT logic lives —
adding a new system needs only a ``.vast`` and (optionally) a local extract.

**objectives** — named optimized values with a ``direction`` (``maximize`` /
``minimize``). One entry today (multi-objective is forward-compatible since
objectives are already a named list).

**strategy_parameters** — algorithm-specific tuning, owned and validated by the
chosen strategy plugin (so a new algorithm adds nothing to the core schema):

* ``qd`` — an ``archive`` (``grid`` | ``cvt``, per-measure bounds/bins, or cvt
  ``cells``) plus emitter ``sigma`` / ``emitters``.
* ``optuna`` — ``sampler`` (``tpe`` | ``cmaes`` | ``random``), ``constant_liar``.
* ``random`` — none.

Strategies
----------

* **random** — uniform sampling each generation; memoryless baseline (coverage).
* **qd** (pyribs MAP-Elites) — fills an *archive* of behaviorally distinct
  high-objective configs: with ``failure_rate`` + behavior measures, a map of the
  *different kinds* of failures (diversity). ``report()`` returns the archive
  (coverage / QD-score / elites).
* **optuna** (TPE) — sample-efficiently drives toward the single most
  failure-prone config (exploitation).

Custom strategies/extractors are file-loadable too — the same
``./path.py:Class`` reference works for ``strategy``, ``extract`` and search
``postprocessing``.

Repetitions and noisy systems
-----------------------------

Robotic systems are non-deterministic, so an objective is a point estimate over
``execution.runs`` repetitions (the extractor aggregates them). Every
``Evaluation`` carries ``n_samples`` so a noise-aware strategy can weigh
confidence, and a strategy may set ``ParamSet.n_reps`` to request more
repetitions for a borderline set (the loop groups a generation by effective
repetition count and launches each group accordingly).

Extraction vs. analysis postprocessing
---------------------------------------

Search ``postprocessing`` (optional) prepares raw artifacts *for scoring* and is
**separate** from ``results_processing`` (analysis/visualization, never run by
the search loop). Both accept entry-point names and local file refs. The built-in
``extract_to_csv`` postprocessing plugin runs an extractor and writes its
objectives+measures to a per-config CSV, so the *same* extract module feeds both
search and the analysis notebooks.

Running a search
----------------

.. code-block:: bash

   vast execution local run

``vast execution local run`` is the single entry point: when the project ``.vast``
has a ``search:`` block it drives the search loop, otherwise it runs a batch.
Results, per-generation outputs and a live-queryable ``search.sqlite`` are written
under a timestamped directory in the project results dir (override with
``--output``). See ``configs/examples/quadrotor_landing/`` for runnable random,
QD and Optuna variants over one shared scenario, sim and extract.
