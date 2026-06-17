.. _search:

Iterative Search
================

By default a RoboVAST campaign runs as a **batch**. Adding a ``search:`` block
turns it into an **iterative, closed-loop search**: a strategy proposes parameter
sets, an *extract* step scores them, and the strategy is told the results so it
can propose the next batch. The goal is surfacing failures and
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
     runs_per_job: 1

   search:
     # ---- universal core (every strategy) ----
     strategy: qd               # plugin: entry-point name OR ./search/s.py:Cls
     search_space:              # typed dims: float / int / choice
       thrust_gain:   {type: float, low: 0.3, high: 3.0}
       mass:          {type: int,   low: 1,   high: 3}
       mode:          {type: choice, values: [a, b, c]}
     postprocessing:            # write per-run metrics from raw results (same
     - ./search/metrics.py:QuadMetrics   #   format/loader as results_processing
     extract:                   # read those metrics -> {objectives, measures}
       plugin: ./search/extract.py:QuadExtract   # entry-point name OR file ref
       params: {metrics: metrics.csv}            # parameterize from the .vast
     objectives:                # what to optimize (>=1 entries)
     - {name: failure_rate, direction: maximize}
     per_batch: 16              # parameter sets proposed per batch
     budget: {batches: 20}
     seed: 0
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

**postprocessing** — a list of postprocessing plugins run over each batch's
results *before* scoring (e.g. to write per-run ``metrics.csv`` from raw
artifacts). Same format and loader as ``results_processing.postprocessing``: each
entry is an entry-point name, a ``./path.py:Class`` local file ref, or a
``{name: {params}}`` dict — so the **same SUT plugin feeds both** the search and
the batch analysis notebooks (one place computes metrics).

**extract** — the scoring step that turns a parameter set's postprocessed results
into named **objectives** (optimized) and **measures** (quality-diversity behavior
axes). ``plugin`` is an entry-point name (built-in ``failure_rate``) or a **local
file relative to the .vast** (``./search/x.py:Cls``); ``params`` is passed to it.
It reads what postprocessing produced (e.g. ``metrics.csv``) plus ``test.xml`` and
aggregates over a config's runs; the framework records how many samples backed
each result. Metric *computation* lives in a postprocessing plugin; the extractor
just reads, aggregates and names.

**objectives** — named optimized values with a ``direction`` (``maximize`` /
``minimize``). One entry today (multi-objective is forward-compatible since
objectives are already a named list).

**strategy_parameters** — algorithm-specific tuning, owned and validated by the
chosen strategy plugin (so a new algorithm adds nothing to the core schema). See
each strategy below for its parameters.

Strategies
----------

All strategies share the universal core and differ only in how they propose the
next batch and what ``report()`` returns. The three built-ins are complementary —
coverage (``random``), diversity (``qd``) and exploitation (``optuna``).

random
^^^^^^

Uniformly samples each ``search_space`` dimension every batch — memoryless, no
``strategy_parameters``. It is the **coverage** baseline: it makes no assumptions
and explores the whole space evenly, so it is the reference a smarter strategy
should beat, and a robust choice when you simply want broad sampling.
``report()`` ranks every evaluation by the objective (``best`` = the top one).
Ships in the base install.

.. code-block:: yaml

   search:
     strategy: random
     # no strategy_parameters

qd — quality-diversity (pyribs MAP-Elites)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Fills an **archive** of behaviorally *distinct* high-objective parameter sets,
binned by **measures** (behavior axes). With ``failure_rate`` as the objective and
behavior measures (e.g. ``max_tilt``, ``drift_dist``), the archive becomes a map of
the *different kinds* of failures — it answers "how many qualitatively distinct
ways can this fail?", not just "what is the single worst case". Use it for
**diversity / behavior coverage**. ``report()`` returns the archive in
``SearchReport.extra`` (coverage, QD-score, elite count) plus the elite list.

``strategy_parameters``:

* ``archive.type`` — ``grid`` (per-measure ``bins``) or ``cvt`` (``cells``
  centroids; preferred for more than ~2 measures).
* ``archive.measures`` — the behavior axes, ``{name: {low, high, bins}}``; each
  name must be a measure the extractor returns (``bins`` applies to ``grid``).
* ``sigma`` — emitter step size as a fraction of each dimension's range
  (default ``0.1``).
* ``emitters`` — number of CMA-ME emitters (default ``1``).

Needs the extra: ``pip install 'robovast[qd]'``.

.. code-block:: yaml

   search:
     strategy: qd
     strategy_parameters:
       archive:
         type: cvt
         cells: 512
         measures:
           max_tilt:   {low: 0.0, high: 0.75}
           drift_dist: {low: 0.0, high: 3.5}
       sigma: 0.15
       emitters: 1

optuna — TPE / Bayesian optimization
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Sample-efficiently drives toward the **single best** (e.g. most failure-prone)
parameter set: it models the objective and concentrates samples where it expects
improvement. Use it for **exploitation** — finding one worst case in few
evaluations — the complement to ``random`` (coverage) and ``qd`` (diversity).
``report()`` ranks the trial history (``best`` = the top trial).

``strategy_parameters``:

* ``sampler`` — ``tpe`` (default, Tree-structured Parzen Estimator), ``cmaes``
  (CMA-ES; strong on smooth continuous spaces) or ``random``.
* ``constant_liar`` — for ``tpe``, improves batched (per-batch) asks by
  penalizing in-flight points (default ``true``).
* ``n_startup_trials`` — random trials before the model takes over (optional).

Needs the extra: ``pip install 'robovast[optuna]'``.

.. code-block:: yaml

   search:
     strategy: optuna
     strategy_parameters:
       sampler: tpe

Custom strategies/extractors are file-loadable too — the same
``./path.py:Class`` reference works for ``strategy``, ``extract`` and search
``postprocessing``. To write and register one, see
:ref:`extending-search-strategy` and :ref:`extending-extractor` in the developer
guide.

Repetitions and noisy systems
-----------------------------

Robotic systems are non-deterministic, so an objective is a point estimate over
``execution.runs`` repetitions (the extractor aggregates them). Every
``Evaluation`` carries ``n_samples`` so a noise-aware strategy can weigh
confidence, and a strategy may set ``ParamSet.n_reps`` to request more
repetitions for a borderline set (the loop groups a batch by effective
repetition count and launches each group accordingly).

Postprocessing: one mechanism, two lists
-----------------------------------------

Postprocessing plugins (``BasePostprocessingPlugin``) are loaded identically
wherever they appear — by entry-point name **or** a local ``./path.py:Class`` file
reference — via one shared resolver/runner. They are configured in two places:

* ``results_processing.postprocessing`` — runs at analysis time
  (``vast eval gui`` / ``vast results postprocess``).
* ``search.postprocessing`` — runs over each batch's results during a search,
  before ``extract``.

So a SUT writes **one** metrics plugin (e.g. ``./search/metrics.py:QuadMetrics``
turning ``trajectory.csv`` into per-run ``metrics.csv``) and lists it in either or
both places: the analysis notebooks and the search extractor then read the same
``metrics.csv`` — one source of truth, no duplicated metric logic.

Running a search
----------------

.. code-block:: bash

   vast execution local run

``vast execution local run`` is the single entry point: when the project ``.vast``
has a ``search:`` block it drives the search loop, otherwise it runs a batch.
Results, per-batch outputs and a live-queryable ``campaign.sqlite`` are written
under a timestamped campaign directory in the project results dir (override the
parent with ``--output``). See ``configs/examples/quadrotor_landing/`` for runnable random,
QD and Optuna variants over one shared scenario, sim and extract.
