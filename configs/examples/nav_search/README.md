# nav_search — one crossing, nine ways of searching it

A TurtleBot 4 crosses a 10 × 10 m room to a goal 5 m away. A barrier stands between, with a
doorway in it, and the doorway is **not on the robot's map** — it only discovers it by
looking. One pedestrian walks back and forth across the route, and does not yield.

Every campaign here varies the same two things — how wide the doorway is, and when the
person is in the way — and differs **only in its `search:` block**. One world, one scenario,
one extractor. That is what makes a difference in their results attributable to the search
strategy rather than to the experiment.

## What each campaign answers

| file | strategy | the question | what you get |
|---|---|---|---|
| `nav_grid.vast` | *(batch, no search)* | what does exhaustive coverage cost, and still miss? | the reference map, and the run count the others are judged against |
| `nav_search_random.vast` | `random` | what fraction of situations fail? | the honest denominator, with an interval |
| `nav_search_halton.vast` | `halton` | the same estimate, for the same budget? | a visibly tighter interval — read against `random`, nothing else |
| `nav_search_tpe.vast` | `optuna` (tpe) | what is the single worst crossing? | that combination, and how few evaluations found it |
| `nav_search_cmaes.vast` | `optuna` (cmaes) | does an evolution strategy beat a model here? | convergence against `tpe` at equal budget |
| `nav_search_qd.vast` | `qd` | how many *kinds* of trouble are there? | an archive keyed on failure mode × clearance |
| `nav_search_boundary.vast` | `boundary` | where does it *start* failing? | the contour where robustness crosses zero |
| `nav_search_adaptive_reps.vast` | `optuna` + `repetitions` | the same answer for fewer runs? | runs spent, against `tpe` |
| `nav_search_minimax.vast` | `./search/minimax.py` | which tuning survives the worst? | the robust setting, and what it costs |

## What to compare with what

These pairings carry the findings, and none of them is visible from a single campaign:

- **`random` vs `halton`** — the width of the interval at an identical `runs:` budget.
- **`tpe` vs `cmaes`** — convergence on a smooth, low-dimensional space.
- **`tpe` vs `adaptive_reps`** — the same conclusion, and how much of the budget each spent
  confirming cells that were never in doubt.
- **`grid` vs everything** — what exhaustive coverage cost, and what it found that the
  searches missed (or did not).

Each pair must share the same budget. Two strategies given equal *batches* are not given
equal simulator once repetitions stop being constant, which is why the coverage pair is
bounded by `runs:`.

## The objective is a margin, not a verdict

`failure_rate` is a proportion over N runs, so with 3 runs it has four reachable values —
and against a sharp physical threshold nearly every cell lands on an endpoint. The campaign
that motivated this directory had **93.8% of its configurations scoring exactly 0.0 or
1.0**: every strategy hit the ceiling in a few draws and the comparison between them said
nothing.

So the objective here is a **robustness margin**: the worst of one signed margin per failure
mode (clearance, time, arrival), aggregated worst-case across repetitions rather than
averaged. Continuous, signed, negative means failed, and it grades what a verdict cannot.
Measured on a 16-cell grid run of this directory:

| metric | over the same 16 runs |
|---|---|
| `failure_rate` | every cell at exactly 0.0 or 1.0 |
| `min_clearance` | −0.072 … +0.607, continuous |

One cell passed with **a millimetre** of clearance. A verdict scores it identically to one
that passed with 0.4 m.

## How the world is put together

- **The doorway** is two `boxes` segments with a gap, filled per configuration by
  `variations/doorway.py`. It is absent from the map, so the global planner first routes
  straight through it and must replan once the lidar sees it.
- **`contact_monitor`** publishes `/collision` — a real contact force, latched. It is the
  **verdict**, and the scenario fails a trial on it.
- **`clearance_monitor`** publishes `/clearance` — how close the robot came, measured
  against real geometry in the simulator. It is the **gradient**, and it never ends a trial.
  The two `ignore:` lists must agree, or one reads zero forever while the other reads clean.
- **The walker** patrols across the route and does not yield (`avoidance: false`). The robot
  is the system under test, so the robot does the avoiding. Its `dwell` shifts the patrol's
  phase, which is how a campaign controls *when* the encounter happens.

Both failure mechanisms are real and separable, measured across the doorway axis:

| doorway | 0.4 m | 0.8 m | 1.2 m | 1.6 m |
|---|---|---|---|---|
| failures (of 4) | 4 | 3 | 1 | 1 |
| collisions | 0 | 3 | 1 | 1 |
| mechanism | planner **refuses**; never approaches | squeezes through, **hits the walker** | mostly passes | mostly passes |

## Budgets

The budgets here are **demo-sized** — a few hundred runs, under an hour each on a quiet
lane. A research budget would be considerably larger; they are small so the directory can be
run through end to end, not because these are the numbers to publish.

At ~25 s of driving per run plus bring-up, and with the lane's concurrency, expect tens of
minutes per campaign.

## Running one

```console
vast workspace init . --name nav-search
vast execution local run           # or start it through the service / MCP
```

Pilot through **`nav_grid.vast`** — `config_filter` is refused for search campaigns (a
search has no named configurations until it proposes them), so a single-cell dry run has to
go through the batch-mode file.

## Notes for anyone extending this

- **`set_yaml_value` needs `import osc.dataops`.** The parser's complaint is
  "BehaviorInvocation uses unknown behavior", which reads like a typo in the action name.
- **`rosbags_to_csv` writes `rosbag2_<topic>.csv`**, not `<topic>.csv`.
- **The ground-truth arrival radius is not nav2's `xy_goal_tolerance`.** nav2 declares
  success against its estimated pose at the instant it stops; the metric measures ground
  truth at the last recorded sample. Runs that *passed* ended 0.23–0.51 m out, so comparing
  against the planner's 0.25 scored most of them as maximum-severity failures.
- **Check that a factor axis actually spans outcomes before trusting a sweep.** An earlier
  version of the grid used doorway widths of 0.8–2.6 m, every one of which the robot passed
  — so the geometry contributed nothing and every failure came from the walker.
