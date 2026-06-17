# Quadrotor landing — search example (random / QD / Optuna)

A toy 2D quadrotor must hover and land on a pad at `x = 0` in wind. It is
deliberately simple ([files/quadrotor_sim.py](files/quadrotor_sim.py),
single file, numpy only) but **stochastic** — wind gusts and sensor noise mean a
given parameter set can land on one run and fail on the next.

All variants share one scenario, sim and `extract` module; only the `search`
`strategy` (and its `strategy_parameters`) differ:

| File | Mode | Run with |
|---|---|---|
| [quadrotor_landing.vast](quadrotor_landing.vast) | **batch** grid (`ParameterVariationList`) | `vast execution local run` |
| [quadrotor_landing_search.vast](quadrotor_landing_search.vast) | **random** search — coverage baseline | `vast execution local run` |
| [quadrotor_landing_qd.vast](quadrotor_landing_qd.vast) | **quality-diversity** (pyribs) — archive of *distinct* failures | `vast execution local run` (`pip install 'robovast[qd]'`) |
| [quadrotor_landing_optuna.vast](quadrotor_landing_optuna.vast) | **Optuna TPE** — efficient single worst-case | `vast execution local run` (`pip install 'robovast[optuna]'`) |

## Variation points (search space)

| Parameter | Meaning |
|---|---|
| `thrust_gain` | horizontal control aggressiveness |
| `mass` | vehicle mass [kg] |
| `wind_strength` | mean horizontal wind acceleration [m/s²] |
| `descent_rate` | commanded descent speed [m/s] |

## Failure modes (each coupled to two parameters)

The failures are **regions in the joint space**, so a search has to explore
combinations — no single parameter explains a failure:

| Outcome | Cause | Coupling |
|---|---|---|
| `hard_crash` | touchdown speed too high (max thrust is an absolute force, so achievable deceleration `T_MAX/mass − g` shrinks with mass) | `mass × descent_rate` |
| `tip_over` | tilt exceeds the limit (aggressive lean overshoots in gusts) | `wind_strength × thrust_gain` |
| `drift_miss` | ends off the pad (too little lean authority to counter drift) | `wind_strength × (low) thrust_gain` |
| `landed` | clean landing on the pad | — |

## Scoring: the `extract` module

The sim writes only the **raw** `trajectory.csv` (`t, x, z, vx, vz, tilt`) per
run. [search/extract.py](search/extract.py) (`QuadExtract`) is the one
SUT-specific scoring step — referenced from the `.vast` as
`./search/extract.py:QuadExtract` and parameterizable via `extract.params`. It
derives, aggregated over a config's runs:

- **objective** `failure_rate` (from `test.xml`),
- **measures** `max_tilt`, `drift_dist`, `landing_speed`, `control_effort`
  (from `trajectory.csv`).

The QD archive's `strategy_parameters.archive.measures` select/bound which
measures form the behavior space. The same module is reused for analysis: the
batch `.vast` lists `extract_to_csv` under `results_processing.postprocessing`,
which runs `QuadExtract` and writes a per-config `metrics.csv` — one source of
truth, no duplicated metric logic.

## Visualization

`analysis/` notebooks (wired via `evaluation.visualization`, matplotlib only):

- `analysis_run.ipynb` — x–z descent path + tilt, **plus an animated GIF**
  (reads `trajectory.csv`).
- `analysis_qd.ipynb` — reads `campaign.sqlite`: objective distribution and 2-D
  projections of the behavior space coloured by objective (the discovered failure
  landscape; works for random/QD/Optuna runs).
- `analysis_campaign.ipynb` / `analysis_config.ipynb` — per-config metrics for
  batch runs (from `extract_to_csv`).

See `docs/search.rst` for the generic `search` schema and how strategies /
extractors plug in (including local-file and multi-objective extensions).
