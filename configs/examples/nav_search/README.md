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
| `nav_search_halton.vast` | `halton` | the same estimate, for the same budget? | the fraction from an evenly-covering sample — read against `random`, nothing else |
| `nav_search_tpe.vast` | `optuna` (tpe) | what is the single worst crossing? | that combination, and how few evaluations found it |
| `nav_search_cmaes.vast` | `optuna` (cmaes) | does an evolution strategy beat a model here? | convergence against `tpe` at equal budget |
| `nav_search_qd.vast` | `qd` | how many *kinds* of trouble are there? | an archive keyed on failure mode × clearance |
| `nav_search_boundary.vast` | `boundary` | where does it *start* failing? | the contour where robustness crosses zero |
| `nav_search_adaptive_reps.vast` | `optuna` + `repetitions` | the same answer for fewer runs? | runs spent, against `tpe` |
| `nav_search_minimax.vast` | `./search/minimax.py` | which tuning survives the worst? | the robust setting, and what it costs |

## What to compare with what

These pairings carry the findings, and none of them is visible from a single campaign:

- **`random` vs `halton`** — the same fraction, drawn two ways, at an identical `runs:`
  budget. One campaign of each gives two point estimates; to compare their *spread* run
  each over several `seed:` values, because a spread is a property of an estimator across
  repeats and not something a single campaign can report.
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

**Each margin is divided by a SCALE, never by its own threshold.** This is the whole design,
and getting it wrong is not a detail — the first version of this objective divided by
thresholds and came out *worse than the `failure_rate` it replaced*. A threshold answers
*did it fail*; a scale answers *by how much*. Divide by the threshold and you conflate them:
with the 0.05 m contact threshold as denominator the clearance margin carried 20 per metre
against the arrival margin's 1.67, so `min()` returned whichever margin had the tightest
denominator rather than whichever failure was nearest.

```text
robustness = min( (min_clearance - contact)  / clearance_scale,
                  (timeout - t_trial)        / timeout,
                  (arrival_radius - d_goal)  / path_scale )
```

**A scale is the reach of its own term**, and the three have to be comparable or the
deepest one decides every score. `path_scale` is the scenario's traverse, (−2.5, 0) →
(2.5, 0), because a run that never arrives can be short by the whole of it. `clearance_scale`
is *not* the room the widest doorway offers: a run cannot be clear by that much and fail, and
it cannot penetrate an obstacle by more than a few centimetres either, because contact ends
the trial. The clearance term's reach is that penetration depth, ~0.1 m. The `timeout` margin
was always of this form, and it is the one that never needed a floor.

Scaled this way a full-penetration contact reaches about −1.0 and a robot that never left the
start reaches −0.88, so the worst of the three is whichever failure is nearest rather than
whichever term happens to have the longest run. Scale the clearance term by the doorway
instead and it caps near −0.16, `min()` returns the goal margin whenever the robot fails to
arrive, and the objective ranks a robot that safely stopped short below one that hit the
pedestrian.

Measured over the 48 cells of one `nav_search_halton` run, `failure_rate` took 4 distinct
values with 30 of 48 (62.5%) on an endpoint — the cliff this replaced.

**Two changes are easy to confuse here, so they are separated by measurement** — 16 cells at
15 repeats each, one thing varied at a time:

| | distinct values | cells ≤ −1 |
|---|---|---|
| thresholds as denominators, **floor at −1** | **2/16** | 15/16 |
| thresholds as denominators, **no floor** | **16/16** | 15/16 |
| scales as denominators, no floor | **16/16** | **0/16** |

Removing the clamp is what restores the distinct values; the denominators are unchanged
between the first two rows. What the scales buy is different and also necessary: they put the
margins in a range where the three can be compared, which is the paragraph above. Expect a
clamp, not a denominator, to be what flattens this objective if it is ever changed again.

The sign agrees on 48 of 48 cells: re-normalising does not move the verdict, only its
resolution. The tightest cell sits at −0.007, and one cell of the earlier 16-cell grid
passed with **a millimetre** of clearance — a verdict scores that identically to one that
passed with 0.4 m.

There is no floor, and that is deliberate. A margin past −1 means what it says — missed by
more than the whole scale — and a clamp would replace that with a tie. Contact reaches past
−1 routinely, which is the point: once the worst crossings stop sharing a value, an
adversarial search can keep descending after it finds its first failure instead of going
blind.

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

Both failure mechanisms are real and separable, measured across the doorway axis — 32 runs
per width, the grid's 8 repetitions at each of its 4 walker phases:

| doorway | 0.4 m | 0.8 m | 1.2 m | 1.6 m |
|---|---|---|---|---|
| failures (of 32) | 31 | 16 | 14 | 19 |
| collisions | 0 | 16 | 14 | 19 |
| mechanism | planner **refuses**; never approaches | squeezes through, **hits the walker** | | |

Only the first column is a property of the doorway. At every width that the robot will
attempt, failure and collision are **the same event** — 16 of 16, 14 of 14, 19 of 19 — so past
0.4 m the geometry decides only whether the robot goes, and the walker decides whether it
gets through.

Which is why the axis is **not monotone**: 1.6 m fails more often than 1.2 m. Read the grid
by column instead and the reason is plain — the walker's phase is the stronger factor, and
`dwell: 0` is the worst column at every width, including 8 of 8 at the widest doorway:

| doorway \ dwell | 0.0 s | 2.0 s | 4.0 s | 6.0 s |
|---|---|---|---|---|
| 0.4 m | 7 | 8 | 8 | 8 |
| 0.8 m | 7 | 6 | 1 | 2 |
| 1.2 m | 6 | 4 | 0 | 4 |
| 1.6 m | 8 | 5 | 3 | 3 |

**Four runs per width cannot see any of this**, and an earlier version of this table read
them as 4/3/1/1 with the two widest widths described as mostly passing. The outcome is
bimodal (see the objective, above): a cell has a probability of failing rather than a
robustness, so a handful of draws per width ranks the widths by which of them happened to
draw the bad mode. The ordering it produced was an artifact of the sample size, not a
property of the world.

## Budgets

The budgets here are **demo-sized** — a few hundred runs, under an hour each on a quiet
lane. A research budget would be considerably larger; they are small so the directory can be
run through end to end, not because these are the numbers to publish.

At ~25 s of driving per run plus bring-up, and with the lane's concurrency, expect tens of
minutes per campaign.

## Running one

```console
vast workspace init . --name nav-search
vast workspace run nav-search nav_search_random.vast   # or through the service / MCP
```

Pilot through **`nav_grid.vast`** — `config_filter` is refused for search campaigns (a
search has no named configurations until it proposes them), so a single-cell dry run has to
go through the batch-mode file.

## Notes for anyone extending this

- **Start from the campaign that varies the channel you need.** A `.vast` reaches three
  surfaces and they are not interchangeable: `sim:` writes into the compiled world (every
  campaign here), `sut:` rewrites the system under test's own config files, and `scenario:`
  sets scenario parameters. `nav_search_minimax.vast` is the one that uses all three.
- **A `sut:` source needs a scenario parameter to land on.** Staging gives each configuration
  its own rewritten copy at `/config/<config-name>/<path>` and drops the original from
  `run_files`, so exactly one copy exists; the trial finds it because RoboVAST rewrites *a
  scenario parameter whose value is the source's declared path*. A parameter left at its
  `.osc` default is not one the campaign set, so nothing is rewritten and the trial launches
  a path that is no longer there. Declare it on the `scenario:` channel, as
  `nav_search_minimax.vast` does with `params_file`.
- **`rosbags_to_csv` writes `rosbag2_<topic>.csv`**, not `<topic>.csv`.
- **The ground-truth arrival radius is not nav2's `xy_goal_tolerance`.** nav2 declares
  success against its estimated pose at the instant it stops; the metric measures ground
  truth at the last recorded sample. Runs that *passed* ended 0.23–0.51 m out, so comparing
  against the planner's 0.25 scored most of them as maximum-severity failures.
- **Check that a factor axis actually spans outcomes before trusting a sweep.** An earlier
  version of the grid used doorway widths of 0.8–2.6 m, every one of which the robot passed
  — so the geometry contributed nothing and every failure came from the walker.
