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
     repetitions:               # OPTIONAL: how many runs each cell gets
       policy: adaptive         #   (see "Repetitions and noisy systems")
       min: 1
       max: 8
     budget:                    # resource caps (see "When does a search stop?")
     - batches: 20
     - runs: 800                #   what actually bounds wall-clock
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

*How* it aggregates is a real choice, and the obvious answer is usually the wrong
one. Averaging hides the run you care about — four comfortable landings and one that
nearly tipped over average to "comfortable" — and on a quality-diversity archive it
collapses the very spread the archive exists to map: measured on a quadrotor QD
campaign, behaviour measures averaged over five runs filled 3 of 512 cells, because
averaging pulled every cell toward the middle of the behaviour space before the
archive saw it. :func:`robovast.search.aggregate.aggregate` provides ``worst``
(the default), ``quantile`` (a pessimistic tail that one freak run cannot define) and
``mean`` (which must be asked for by name)::

   from robovast.search.aggregate import aggregate

   clearance = aggregate(per_run_clearances, how='worst')            # least room seen
   duration  = aggregate(per_run_times, how='quantile', quantile=0.1,
                         higher_is_safer=False)                      # slow tail

``higher_is_safer`` says which end is the bad end, and is deliberately *not* the
objective's ``maximize``/``minimize``: an adversarial search minimizes a safety margin
on purpose, and that margin is still a quantity where higher means safer. Conflating
the two aggregates from the wrong tail.

**objectives** — named optimized values with a ``direction`` (``maximize`` /
``minimize``). One entry gives a scalar search with a ``best``; **two or more give a
Pareto search**, whose deliverable is ``SearchReport.front`` — the set of evaluations no
other beats on every objective at once.

There is no ``best`` in that case, deliberately: nothing ranks "close but fast" against
"slow but safe" without a weighting the campaign never supplied, so nominating a winner
would invent one. ``target_objective`` and ``no_improvement`` are refused with more than
one objective for the same reason (they compare a scalar); bound such a search with
``batches`` / ``time`` / ``runs`` / ``evaluations``, or with a ``metric`` the strategy
reports.

The front is computed by the framework from whatever a strategy reports, so a strategy
does not have to know the concept — one whose optimiser tracks a front natively fills
``front`` itself and is left alone. For Optuna, multi-objective means
``strategy_parameters: {sampler: nsga2}``; the scalar samplers are refused by name with
several objectives rather than quietly optimising the first.

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

   **Zero configs is the other direction, and it is tolerated.** A draw can be
   unrealizable rather than misconfigured — a path too short to hold the obstacles the
   same draw asks for, say — and then the variation pipeline composes nothing for it.
   That set is recorded as ``composition_failed`` (visible in the store's ``unit``
   table), nothing runs for it, and the batch carries on with the rest. So ``tell()``
   may be handed **fewer evaluations than ``ask()`` proposed**, and a strategy has to
   cope: ingest what arrived, or — if its optimiser cannot take a short generation —
   skip that generation. Never fill the hole with a stand-in objective: the measures
   would have to be invented too, and an invented measure vector lands the fabrication
   in a real archive cell where the search then chases it.

Strategies
----------

All strategies share the universal core and differ only in how they propose the
next batch and what ``report()`` returns. The built-ins are complementary —
coverage (``random``, ``halton``), diversity (``qd``), exploitation (``optuna``)
and boundary tracing (``boundary``).

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

halton — low-discrepancy coverage
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The same job as ``random``, done better. Uniform draws clump and leave holes, so a
failure region can sit between samples and an estimate carries more variance than its
sample size suggests. A Halton sequence fills the space evenly *by construction*: the
same budget answers the same question with a tighter interval. When two campaigns exist
only to be compared — "does this strategy beat blind sampling?" — the blind one should at
least be good at being blind.

Scrambled by default and seeded from ``search.seed``: the textbook sequence is
deterministic, so two campaigns with different seeds would otherwise draw identical
points and their comparison would measure nothing. The sequence **continues across
batches**; restarting it per batch would re-draw the same points and cover less than
random. Ships in the base install.

.. code-block:: yaml

   search:
     strategy: halton
     strategy_parameters:
       scramble: true        # default; false gives the textbook (identical) sequence

Halton rather than Sobol for one reason: Sobol needs direction-number tables and in
practice a scipy dependency, and a *baseline* that only runs when an extra is installed
is not a baseline. Halton's known weakness is high dimensions, where the larger prime
bases correlate — it is refused above 20 dimensions rather than quietly degrading, and a
search space of the size these campaigns declare is nowhere near that.

boundary — trace a level set
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

"Maximize failures" has a trivial answer — crank the worst factor to its limit — and a
search that finds it reports something you could have guessed. The engineering question
is narrower: *where does it start failing?* No budget spent deep inside the failure
region answers that, and a maximizing search spends all of it there.

``boundary`` samples the contour where the objective crosses a stated ``level`` instead:
zero for a signed margin (the failure boundary), 0.5 for a rate (the coin-flip contour,
where the outcome is genuinely uncertain). ``level`` is required — which contour matters
is a property of the experiment, and a default would silently trace the wrong one.

.. code-block:: yaml

   search:
     strategy: boundary
     objectives:
     - {name: clearance_margin, direction: minimize}
     strategy_parameters:
       level: 0.0            # required: the contour to trace
       neighbours: 5         # k for the surrogate
       candidates: 512       # scored per proposal -- arithmetic, not runs
       exploration: 0.35     # keeps a batch spread along the contour

**The level is a strategy parameter, not an objective direction.** ``direction`` means
"which way is better", and a target answers a different question: there is no better, only
nearer. Putting it on the objective would also collide by name with
``stopping: target_objective``, which is a stopping criterion rather than an optimisation
target — two different ``target``\ s in one file format is a confusion nobody needs.

The surrogate is inverse-distance-weighted k-nearest-neighbour, needing nothing beyond
numpy. A Gaussian process would model the landscape better and would also make the
strategy unavailable without an optional extra, on campaigns whose budgets are tens of
evaluations rather than thousands — the regime where a GP's advantage is smallest. With
fewer than two evaluations there is no level to seek, so a cold start draws from the same
low-discrepancy sequence ``halton`` uses; nothing is wasted, since those points are what
the model is built from. ``report()`` gives ``level`` and ``closest_to_level``: a boundary
search that never approached its contour found no boundary, and that must be visible
rather than inferred from a best-objective number that means nothing here.

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


**A measure may be categorical.** The most useful behaviour axis is often not a number --
"it collided" / "it timed out" / "it never reached the goal" is the answer an engineer
wants -- so an axis states either ``low``/``high`` or ``values``, never both::

   archive:
     type: grid
     measures:
       failure_mode:  {values: [collision, timeout, goal_miss, stuck]}
       min_clearance: {low: 0.0, high: 1.5}

The categories fix the axis: *k* of them give *k* bins, one per category, so the bounds
and bin count are derived rather than restated (two sources of truth for one fact could
disagree). An extractor returns the category **by name**; a name the archive does not
declare is refused, because clamping or dropping it would put a behaviour the archive
cannot represent into a cell that means something else, and a diversity map whose cells
mean the wrong thing is worse than one missing a cell.

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
     - evaluations: 200      # parameter sets scored
     - runs: 800             # individual executions
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
* ``evaluations`` — stop after this many parameter sets have been **scored**. A draw
  that composed to nothing, or whose every run was lost, never reaches ``tell()`` and
  is not counted.
* ``runs`` — stop after this many individual **executions**. Counted from what each
  batch asks for, so it bounds wall-clock rather than results.

``evaluations`` and ``runs`` are two counts and not one because neither predicts the
other: one evaluation costs as many runs as it was given repetitions. While every cell
gets the same ``execution.runs`` the product ``batches × per_batch × runs`` predicts
the total, and ``batches`` alone is enough; a strategy that varies repetitions per
parameter set (``ParamSet.n_reps``) breaks that product, which is when a run cap starts
earning its place. It is also what makes two strategies comparable — a fair contest
gives both the same number of executions, not the same number of batches.

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

Robotic systems are non-deterministic, so an objective is never a measurement —
it is a **point estimate** over ``execution.runs`` repetitions, which the extractor
aggregates. Every ``Evaluation`` carries ``n_samples`` so a strategy can weigh how
much to trust it.

Why a fixed repetition count wastes most of its runs
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``execution.runs`` spends the same number of runs on every cell, which is
simultaneously too many and too few. A cell whose runs all agree was decided by its
first one; a cell on a failure boundary is exactly where more samples buy something.

Measured on a quadrotor search campaign: **3 of 32 configurations produced a mixed
outcome across 5 repetitions**. The other 29 spent 5 runs each to establish a single
bit — 145 of 160 runs. At three milliseconds a run that is invisible; at ninety
seconds a run it is the campaign's whole budget.

The ``repetitions`` block
^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: yaml

   execution:
     runs: 3                  # still the default for every cell
   search:
     repetitions:
       policy: adaptive       # fixed | adaptive        (default: fixed)
       min: 1                 # floor: the cheapest a cell can be evaluated
       max: 8                 # ceiling: the cost guard
       neighbours: 5          # how many evaluated neighbours judge "contested"
       paired: false          # reuse one seed list across cells (see below)

Omitting the block entirely is not a policy of uniformity — it is the *absence* of a
policy, and every cell runs ``execution.runs`` times exactly as it always did.

* ``fixed`` — every cell gets the same count. Today's behaviour, stated explicitly.
* ``adaptive`` — a cell whose nearest already-evaluated neighbours **agree** gets
  ``min``; one sitting where they **disagree** gets up to ``max``.

Why disagreement, and not a confidence interval
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A confidence rule has to know what the objective *means*: a proportion is uncertain
near 0.5, a safety margin is uncertain near zero, a duration is never "uncertain" in
that sense at all. Such a rule must be told the objective's type and threshold, and is
wrong whenever it is told wrong.

Spread among nearby evaluations needs none of that. It reads how much the
*measurements* disagree, which is the thing extra samples actually resolve, and it
works unchanged for a rate, a margin or a time. Distances are measured in the
normalized unit cube, so a dimension in metres and one in percent contribute
comparably to "nearby" rather than whichever happens to have the larger raw range.

Two consequences worth knowing:

* With **no history** (the first batch) nothing is known about the landscape, so every
  cell gets ``min``. Guessing high would rebuild the uniform waste with a different
  constant.
* When **every observation so far agrees**, no neighbourhood can be contested and
  everything gets ``min``. That is the 29-of-32 case, and spending the floor on it is
  the correct answer, not a degenerate one.

A strategy still outranks the policy
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``ParamSet.n_reps`` is the strategy's own channel and is **never overridden**: the
policy only supplies a default for sets that did not ask for anything. A strategy that
reasons about noise itself therefore keeps full control, and the policy exists so that
strategies which do *not* — ``random``, ``qd``, ``optuna``, anything you write — still
benefit. This is why it is a policy layer rather than a noise-aware strategy: it
composes with every strategy instead of being one of them.

The loop groups each batch by effective repetition count and launches each group
accordingly, so a batch may become several execution groups.

Budgeting a search whose repetitions vary
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Once repetitions are adaptive, ``batches × per_batch × execution.runs`` no longer
predicts anything. Bound the campaign with ``budget: [{runs: N}]``, which counts
executions directly. This is also what makes two strategies comparable: a fair contest
gives both the same number of runs, not the same number of batches.

Pairing, and what it does not buy
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``paired: true`` reuses one seed list across every cell, so two cells are compared
run-for-run instead of only in distribution — a variance reduction that lets a real
difference show up in far fewer runs.

Be clear about its limits. Pairing covers the **simulator's** seeded noise. A system
under test running asynchronously in its own container — message timing, callback
order, CPU contention — is not replayable, so a single run is never reproducible even
paired, and every claim a search makes remains distributional: *"this configuration
fails about 40% of the time"*, never *"this run fails"*.

``seed_parameter`` names the variation channel the per-repetition seed is delivered on
(e.g. ``{sim: seed}``). Without it, repetitions still differ — they are simply
unseeded, so neither pairing nor replay is available.

Postprocessing: one mechanism, two lists
-----------------------------------------

Postprocessing plugins (``BasePostprocessingPlugin``) are loaded identically
wherever they appear — by entry-point name **or** a local ``./path.py:Class`` file
reference — via one shared resolver/runner. They are configured in two places:

* ``results_processing.postprocessing`` — runs at analysis time
  (``vast results postprocess``, then the web UI's Results views).
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
