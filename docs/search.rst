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
     search_space:              # typed dims: float / int / choice / bool
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
     budget:                    # resource caps (see "When does a search stop?")
     - batches: 20
     seed: 0
     # ---- strategy-specific (one block; the strategy validates it) ----
     strategy_parameters:
       archive: {type: cvt, cells: 512,
                 measures: {max_tilt: {low: 0.0, high: 0.75},
                            drift_dist: {low: 0.0, high: 3.5}}}
       sigma: 0.15

Concepts
--------

**search_space** — a *typed* mapping of variable name to domain. Four dimension
types: ``float {low, high, log}``, ``int {low, high, log, step}``,
``choice {values}`` (categorical) and ``bool`` (sugar for a two-value
categorical). Malformed domains are rejected at config-validation time. A
variable either maps directly to a scenario parameter (the simple-sweep case) or
feeds a ``variations:`` template — see `Searching complex variations`_.

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

Searching complex variations
-----------------------------

Mapping a ``search_space`` dimension straight onto a scenario parameter only
covers simple sweeps (the quadrotor example: ``thrust_gain`` → scenario param).
The **complex** variation plugins (``PathVariationRandom``, ``ObstacleVariation``,
``FloorplanVariation``, …) instead *calculate* scenario content from many
parameters — most of which should stay **fixed** while only a few are
**searched**. For that, a ``search:`` block may carry a ``variations:`` template
(and an optional fixed ``parameters:`` block), **identical in shape** to a batch
``configuration`` block. Fixed parameters are written inline; searched ones are
referenced with a ``$name`` (or ``${name}``) marker naming a ``search_space``
dimension:

.. code-block:: yaml

   search:
     strategy: random
     search_space:
       path_length:  {type: float, low: 5.0, high: 15.0}
       obstacle_amt: {type: int,   low: 0,   high: 5}
       path_seed:    {type: int,   low: 0,   high: 100000}
     variations:
     - PathVariationRandom:
         start_pose: "@start_pose"     # @name = scenario-param reference (in-plugin)
         goal_poses: "@goal_poses"
         num_goal_poses: 3
         path_length: $path_length     # $name = searched variable (substituted)
         num_paths: 1                  # FIXED scalar -> exactly one config
         seed: $path_seed
     - ObstacleVariation:
         name: static_objects
         count: 1
         obstacle_configs:
         - amount: $obstacle_amt        # searched
           max_distance: 0.1
         seed: $path_seed
     extract: {plugin: ...}
     objectives: [{name: ..., direction: maximize}]
     per_batch: 8
     budget:
     - batches: 5

For each proposed parameter set the framework deep-copies the template and
substitutes every marker with the sampled value, then runs the **same**
generation chain as batch mode — no change to the variation plugins. A
``search_space`` dimension *not* referenced anywhere in the template falls back
to a direct scenario parameter (so the quadrotor example, which has no
``variations:`` block, behaves exactly as before).

Marker rules:

* A marker matches only when the **entire** value is ``$name`` or ``${name}``;
  the sampled value is substituted **verbatim**, preserving its type (an ``int``
  dim stays an ``int``). There is no mid-string interpolation.
* ``$$`` is an escaped literal ``$`` (a leading ``$$`` collapses to one ``$``).
* This is **disjoint** from the ``@name`` convention, which references a
  *scenario-file parameter* and is resolved **inside** the variation plugin —
  ``$name`` is substituted by the search framework *before* the plugin runs.
* Every ``$name`` must name a declared ``search_space`` dimension (checked at
  config-validation time).

.. important::

   **One parameter set must produce exactly one config.** A search proposes a
   point, evaluates it, and tells the strategy the result, so the mapping is
   1:1. A variation that expands combinatorially breaks this. Make every
   expanding parameter **scalar**: ``PathVariationRandom`` ``num_paths: 1`` with a
   scalar ``path_length``/``num_goal_poses_per_m``; ``ObstacleVariation``
   ``count: 1`` with a single ``amount``/``max_distance`` per ``obstacle_configs``
   entry; ``FloorplanVariation`` ``num_variations: 1``. The framework raises a
   clear error naming the offending parameter set if a variation expands.

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

When does a search stop?
------------------------

Two parallel lists of typed criteria decide when a search ends — **``budget``**
(resource caps) and **``stopping``** (convergence / quality). All entries are
**OR-combined**: the search stops as soon as *any* one fires. At least one
criterion (across the two) is **required** — a search needs a way to end.
Everything is evaluated centrally by the controller after each batch against a
uniform progress snapshot, so the **same criteria work for every strategy**
(``random``, ``qd``, ``optuna``) with no per-strategy code.

.. code-block:: yaml

   search:
     budget:                 # resource caps — "how much will I spend?"
     - batches: 50
     - time: 3600
     stopping:               # convergence / quality — "stop early on results"
     - target_objective: 0.9
     - no_improvement: {patience: 5, min_delta: 0.01}
     - metric: {name: coverage, op: '>=', value: 0.8}

Each entry is a single-key mapping (like ``variations``): the key is the criterion
name; a scalar is shorthand for its main field (``- batches: 50``), and
multi-field criteria use a nested mapping (``- metric: {name: ..., value: ...}``).

**budget** — progress-independent resource caps:

* ``batches`` — stop after this many ask/tell batches (with fixed ``per_batch``
  and ``execution.runs`` this already bounds total evaluations and executions).
* ``time`` — stop after this many seconds of wall-clock time since the search started.

**stopping** — result-dependent early-exits:

* ``target_objective`` — stop when the best objective reaches ``value``
  (direction-aware: ``>=`` for ``maximize``, ``<=`` for ``minimize``).
* ``no_improvement`` — stop when the best objective has not improved by more than
  ``min_delta`` (default ``0``) for ``patience`` consecutive batches.
* ``metric`` — stop when a strategy-reported metric (anything in
  ``SearchReport.extra``, e.g. QD ``coverage`` / ``qd_score``) satisfies
  ``op value`` (``op`` ∈ ``>= <= > <``, default ``>=``); a metric the strategy does
  not report never fires.

A budget cap is recommended so runtime is bounded; with only ``stopping`` the run
is bounded solely by convergence (the controller logs a warning).
``target_objective`` / ``no_improvement`` require a single objective (validated).

**Progress + outcome.** On ``vast execution local run`` the controller logs a
progress line after each batch showing every criterion's current value vs its
limit, e.g.
``📊 batches 3/50 | coverage 0.21/0.30 | failure_rate 0.97/0.9``.
When the search ends, the fired criterion is **persisted** on the ``campaign`` row
of ``campaign.db`` (``stop_kind``, ``stop_reason``, ``batches``,
``elapsed_s`` — directly SQL-queryable) and mirrored in
``SearchReport.extra['stop']``; the campaign analysis notebook prints it.

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
Results, per-batch outputs and a live-queryable ``campaign.db`` are written
under a timestamped campaign directory in the project results dir (override the
parent with ``--output``). See ``configs/examples/quadrotor_landing/`` for runnable random,
QD and Optuna variants over one shared scenario, sim and extract.
