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
| `nav_search_halton.vast` | `halton` | the same estimate, for the same budget? | **measured: no.** A marginally tighter spread, and a *different* estimate — see below |
| `nav_search_tpe.vast` | `optuna` (tpe) | what is the single worst crossing? | that combination, and how few evaluations found it |
| `nav_search_cmaes.vast` | `optuna` (cmaes) | does an evolution strategy beat a model here? | convergence against `tpe` — but **not at equal budget as shipped**, see below |
| `nav_search_qd.vast` | `qd` | how many *kinds* of trouble are there? | an archive keyed on failure mode × clearance |
| `nav_search_boundary.vast` | `boundary` | where does it *start* failing? | the contour where robustness crosses zero |
| `nav_search_adaptive_reps.vast` | `optuna` + `repetitions` | the same answer for fewer runs? | **measured: more runs, not fewer** — mean 4.0 reps against a fixed 3 |
| `nav_search_minimax.vast` | `./search/minimax.py` | which tuning survives the worst? | the robust setting, and what it costs. The only campaign here using the `sut:` channel |

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

**That rule is not kept by the files in this directory, and the first three findings above
did not survive contact with it.** Measured, at the committed budgets:

- **`random` vs `halton` is not a variance story.** Across 4 and 5 seeds at 180 runs each,
  halton's spread is 18% lower (sd 0.039 against 0.043) — a difference n=5 cannot separate
  from zero. What *is* separable is that the two disagree about the answer: 70.0% of the
  space failing against 61.7%, a gap larger than either strategy's own seed-to-seed noise
  (t = 3.0). Read that before reading any interval. And note the question needs SEEDS: an
  interval is a property of an estimator across resamples, and one campaign is one point.
- **`tpe` vs `cmaes` is not at equal budget.** Both are bounded by `batches: 6` *and* a
  `no_improvement` stop, so each stops when its own convergence rule fires — measured, 144
  runs against 96. To race them, bound both by `runs:` and drop the stopping rule, or the
  comparison measures the rule rather than the sampler.
- **`adaptive_reps` spends more, not less.** `min: 1, max: 6` averaged 4.0 repetitions over
  48 cells — 192 runs against `tpe`'s 144 for the same cell count. The allocation does
  discriminate (8 cells took one repetition, 17 took six), but a policy whose mean exceeds
  the fixed baseline cannot save budget against it. Compare at equal `runs:`, or narrow the
  range.

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

The scales come from what this directory already declares, so they move when the world does:
`clearance_scale` is half the widest doorway minus nav2's own `robot_radius` (1.6/2 − 0.18),
`path_scale` is the scenario's own traverse, (−2.5, 0) → (2.5, 0). The `timeout` margin was
always of this form, and it is the one that never needed a floor.

Measured over the 48 cells of one `nav_search_halton` run:

| objective | distinct values | cells at the floor |
|---|---|---|
| `failure_rate` | 4 | 30/48 (62.5%) |
| `robustness`, thresholds as denominators | 17 | **32/48 (66.7%)** |
| `robustness`, scales as denominators | **48** | **0/48** |

The sign agrees on 48 of 48 cells: re-normalising does not move the verdict, only its
resolution. The tightest cell sits at −0.007, and one cell of the earlier 16-cell grid
passed with **a millimetre** of clearance — a verdict scores that identically to one that
passed with 0.4 m.

**What the scale normalisation did not fix: the ORDER.** It removed the plateau, and that
part holds — every campaign since has produced one distinct value per cell with nothing on
a floor. But it left the two margins with wildly different *reach*, and an adversarial
search optimises reach. A refusal leaves the robot ~5 m short, so its goal margin is
(0.6 − 5.0)/5.0 = −0.88; a collision penetrates ~0.05 m, so its clearance margin is
(−0.05 − 0.05)/0.62 = −0.16. Clearance saturates near zero because contact is contact,
while the goal term ranges over the whole traverse.

Measured across four campaigns, the ranges do not overlap: **every collision cell scores
better than every refusal cell.** So the objective ranks "the robot safely declined to
squeeze through a narrow door" as several times worse than "the robot hit the person", and
`tpe` spends its budget descending into the refusal corner — where its worst cells sit
pinned against the `gap_width` lower bound, with *positive* clearance.

The consequence is not cosmetic: at the committed `clearance_scale`, `tpe`'s best score is
its batch-0 random draw and six batches of modelling never beat it. The adversarial
searches do not beat chance on this objective. Lowering `clearance_scale` to 0.10 (a
`search.extract.params` key — no code change) inverts the order, redirects the search to
collisions, costs nothing in resolution, and makes `tpe` descend for its whole budget.
Which rebalance is right is a question about what the directory is for; it is tracked in
cps-test-lab/robovast#199.

There is no floor, and that is deliberate. Once each margin is a fraction of its own scale
nothing reaches −1 unaided, so a clamp would only discard order it no longer needs to bound.
A margin past −1 means what it says — missed by more than the whole scale — and stays ordered
against its neighbours, which is what lets an adversarial search keep descending after it
finds its first failure instead of going blind.

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
vast workspace run nav-search nav_search_random.vast   # or through the service / MCP
```

Pilot through **`nav_grid.vast`** — `config_filter` is refused for search campaigns (a
search has no named configurations until it proposes them), so a single-cell dry run has to
go through the batch-mode file.

## Notes for anyone extending this

- **These files are templates, so the three variation channels should all appear in them,
  and today they do not.** `sim:` (the compiled world) is used by all nine campaigns.
  `sut:` (the system under test's own config files) is used only by
  `nav_search_minimax.vast`. `scenario:` (scenario parameters) is used by **none** of them —
  #171 moved minimax's `inflation_radius` and `max_speed` off that channel onto `sut:` and
  left nothing behind. Anyone copying this directory therefore has no worked example of one
  channel and a single example of another; see the next note, which is the same gap seen
  from the failure it causes.
- **The `sut:` channel needs a scenario parameter to land on.** Staging gives each cell a
  rewritten copy at `/config/<config-name>/<path>` and drops the original from `run_files`,
  so exactly one copy exists; what connects the trial to it is the rewrite of *a scenario
  parameter whose value is the source's declared path* (`docs/configuration.rst`). A
  parameter left at its `.osc` default is not one the campaign set, so nothing is rewritten
  and the trial launches the path staging removed — surfacing 120 s later as a localizer
  that never activates. Declare the path on the `scenario:` channel to arm it.
- **`rosbags_to_csv` writes `rosbag2_<topic>.csv`**, not `<topic>.csv`.
- **The ground-truth arrival radius is not nav2's `xy_goal_tolerance`.** nav2 declares
  success against its estimated pose at the instant it stops; the metric measures ground
  truth at the last recorded sample. Runs that *passed* ended 0.23–0.51 m out, so comparing
  against the planner's 0.25 scored most of them as maximum-severity failures.
- **Check that a factor axis actually spans outcomes before trusting a sweep.** An earlier
  version of the grid used doorway widths of 0.8–2.6 m, every one of which the robot passed
  — so the geometry contributed nothing and every failure came from the walker.
