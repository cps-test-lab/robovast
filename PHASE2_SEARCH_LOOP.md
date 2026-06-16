# Phase 2 — Iterative search loop

> Step 2 of the robovast batch → search plan. Step 1 (multi-config-per-job
> packing) is implemented; this builds on it. **No Hydra** (evaluated and
> dropped — it is a CLI-launch framework that fights a long-running, API-driven
> loop). **Status: implemented** — see `docs/search.rst`, `src/robovast/search/`,
> and `tests/search/`.

## Goal

Keep batch/campaign mode and add a **true iterative search loop**: ask params →
run → score → tell → ask next. The loop is algorithm-agnostic; the goal is
surfacing as many **failures / near-failures** as possible. Quality-diversity
(an archive of distinct difficult parameter sets) is one strategy, not the
frame.

## What Phase 1 gives us (reused unchanged)

- **`Packer` / `JobSpec` / `build_jobs`** (`src/robovast/execution/packer.py`).
- **Multi-config job execution** — one job runs many parameter sets, simulator
  reset between sets; results stay keyed by config name. Packing is wired into
  **both** local (`execute_local.py`) and cluster (`cluster_execution.py`) — the
  prerequisite for this step is **satisfied**.
- **`configs_per_job`** items-per-job packing.
- **Per-config result layout** `<config>/<run>/test.xml`, read via
  `read_test_result()` / `get_vast_configuration_info()`
  (`common/campaign_data.py`).

## Config: the `search` block (`ConfigV1.search`, optional)

```yaml
search:
  # universal core (same shape for every strategy)
  strategy: qd                # plugin: robovast.search_strategies OR ./file.py:Cls
  search_space:               # typed, discriminated on `type`
    thrust_gain: {type: float, low: 0.3, high: 3.0}
  extract:                    # one scorer -> {objectives, measures}
    plugin: ./search/extract.py:QuadExtract   # entry-point name OR file ref
    params: {}                # parameterize the module from the .vast
  objectives:                 # named; >=1; per-objective direction
  - {name: failure_rate, direction: maximize}
  per_step: 16
  budget: {generations: 20}
  seed: 0
  postprocessing: []          # optional, search-only (NOT results_processing)
  # strategy-specific tuning (schema owned/validated by the strategy plugin)
  strategy_parameters:
    archive: {type: cvt, cells: 512, measures: {max_tilt: {low: 0, high: 0.75}}}
```

Absent ⇒ batch. `search_space` keys are dotted paths: a bare name overrides a
scenario parameter; `variations.<ClassName>.<param>` overrides a variation
parameter (collapsing it to one value). `per_step` (items-per-generation) and
`execution.configs_per_job` (items-per-job) are independent.

## Components (all in `src/robovast/search/`)

- **`SearchStrategy`** (`strategy.py`) — generic `ask`/`tell`/`is_done`/`report`;
  plugins under `robovast.search_strategies` (entry-point or file ref). Strategies
  receive full `Evaluation`s and validate their `strategy_parameters` via an
  optional `PARAMS_MODEL`. Implemented: **`RandomSearch`** (dependency-free),
  **`QDStrategy`** (pyribs MAP-Elites, `qd` extra), **`OptunaStrategy`** (TPE,
  `optuna` extra).
- **`Extractor`** (`extractor.py`, `extractors/failure_rate.py`) — the one
  scoring step: `extract(config_dir) -> {objectives, measures}`, parameterized and
  file-loadable. Built-in `failure_rate` (objective only). Replaces the earlier
  objective/descriptor split.
- **Search-space codec** (`space.py`) — typed `search_space` ↔ normalized unit
  vector, for vector optimizers (QD now; CMA-ES/BO later).
- **`load_ref`** (`common/plugin_ref.py`) — resolve a plugin by entry-point name
  or `./path.py:Class` (relative to the `.vast`); shared by search and reused in
  `results_processing` postprocessing.
- **Compose** (`compose.py`) — override injection: each `ParamSet` → one config
  via the existing `generate_scenario_variations()` chain.
- **`CampaignStore`** (`common/store.py`) — sqlite (campaign → generation → unit;
  objectives/measures/n_samples), single writer, live-queryable, resume seam.
- **`SearchLoop`** (`loop.py`) — ask → compose → launch (blocks) → optional search
  `postprocessing` → extract → record → tell. `LocalLauncher` reuses the batch
  run-script path.
- **`extract_to_csv`** (`results_processing/extract_plugins.py`) — postprocessing
  adapter that runs an extractor and writes per-config `metrics.csv`, so the same
  extract feeds search and the analysis notebooks.

## CLI

`vast execution local search` (routes to `SearchLoop` when a `search:` block is
present). Example: `configs/examples/quadrotor_landing/` — a toy stochastic
quadrotor with three two-parameter-coupled failure modes, with random, QD and
Optuna variants over one shared scenario/sim/extract; `analysis/` notebooks
visualise descents and the discovered failure landscape.

## Verification

`tests/search/` — config validation, RandomSearch sampling/budget/direction,
`failure_rate`/file-loaded extractor, codec round-trip, `load_ref`, QD + Optuna
over synthetic objectives (skipped without their extras), the dimension/source-
agnostic acceptance test, `extract_to_csv`, override routing, compose driving real
generation, and a fake-launcher loop e2e recording units to sqlite.

## Deferred

- Multi-objective Optuna (NSGA-II) — additive: `objectives` is already a named
  list; needs `directions=[…]` + a Pareto `front` in the report.
- Mid-loop resume from the persisted strategy state (schema ready).
- Cluster launcher for the loop (local implemented; cluster packing already in
  place).
