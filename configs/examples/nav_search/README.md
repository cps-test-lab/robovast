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

Two further files test claims the nine above cannot settle on their own:

| file | the question | why the nine cannot answer it |
|---|---|---|
| `nav_search_random_6d.vast` | does uniform sampling still work over six factors? | the two-factor space is small enough to saturate, which flatters it |
| `nav_search_tpe_6d.vast` | does the search advantage return once it cannot? | same, in the other direction |

## What to compare with what

These pairings carry the findings, and none of them is visible from a single campaign:

- **`random` vs `halton`** — the same fraction, drawn two ways, at an identical `runs:`
  budget. One campaign of each gives two point estimates; to compare their *spread* run
  each over several `seed:` values, because a spread is a property of an estimator across
  repeats and not something a single campaign can report.
- **`tpe` vs `cmaes`** — convergence on a smooth, low-dimensional space.
- **`tpe` vs `adaptive_reps`** — what one budget buys when repetitions are allocated rather
  than fixed: how much of it each spent confirming cells that were never in doubt.
- **`grid` vs everything** — what exhaustive coverage cost, and what it found that the
  searches missed (or did not).

Each pair must share the same budget. Two strategies given equal *batches* are not given
equal simulator once repetitions stop being constant, which is why the coverage pair is
bounded by `runs:`.

### Computing them: `analysis/compare.py`

Each campaign's notebook is scoped to one campaign, so none of the pairings above is
something a notebook can compute; `analysis/compare.py` computes them. It reads each
campaign's own `campaign.db`, which carries the scored cells **and** the `.vast` that
produced them, so nothing about what a campaign is gets
passed in on the command line: the strategy, its budget and its seed come from the record,
and a comparison cannot be labelled with a strategy the campaign did not run.

```console
mkdir -p /tmp/nav && for c in <campaign_id> ...; do
  vast files get /results/$c/campaign.db /tmp/nav/$c.db
done
python analysis/compare.py /tmp/nav/*.db
```

It prints the roster, then the four pairings, and **refuses to present an unequal
comparison as a fair one** — the equal-budget rule above is checked against the two
campaigns' declared budgets rather than left to whoever reads the output. Pairings whose
campaigns were not passed in are named at the end, because a section that printed nothing
looks exactly like one that had nothing to say.

The one asymmetry it has to handle: the grid scores no `robustness` (batch mode declares no
extractor), so where a search reports `robustness < 0` the grid reports *whether any
repetition of a cell failed*. That is the same question `aggregate: worst` asks, and which
of the two was used is printed beside every figure.

## The objective is a margin, not a verdict

`failure_rate` is a proportion over N runs, so with 3 runs it has four reachable values —
and against a sharp physical threshold nearly every cell lands on an endpoint. Every
strategy then hits the ceiling in a few draws, and the comparison between them says nothing.

So the objective here is a **robustness margin**: the worst of one signed margin per failure
mode (clearance, time, arrival), aggregated worst-case across repetitions rather than
averaged. Continuous, signed, negative means failed, and it grades what a verdict cannot.

**Each margin is divided by a SCALE, never by its own threshold.** This is the whole design,
and getting it wrong is not a detail — the same margins divided by thresholds score
*worse than `failure_rate`*. A threshold answers
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
is its own scale and needs no floor.

Scaled this way a full-penetration contact reaches about −1.0 and a robot that never left the
start reaches −0.88, so the worst of the three is whichever failure is nearest rather than
whichever term happens to have the longest run. Scale the clearance term by the doorway
instead and it caps near −0.16, `min()` returns the goal margin whenever the robot fails to
arrive, and the objective ranks a robot that safely stopped short below one that hit the
pedestrian.

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

## Budgets

The budgets here are **demo-sized** — a few hundred runs each. A research budget would be
considerably larger; they are small so the directory can be run through end to end, not
because these are the numbers to publish.

Campaigns that share a cluster contend for it: each takes longer, and a campaign can abort
before its first batch when a contended node fails calibration. Run them one or two at a
time if the results are meant to be compared with each other.

## Running one

```console
vast workspace init . --name nav-search
vast workspace run nav-search nav_search_random.vast   # or through the service / MCP
```

Pilot through **`nav_grid.vast`** — `config_filter` is refused for search campaigns (a
search has no named configurations until it proposes them), so a single-cell dry run has to
go through the batch-mode file.

Each campaign carries its own analysis view, which the Results Explorer executes
server-side: `analysis/nav_search_<strategy>.ipynb` for the eight searches, and
`analysis/nav_grid.ipynb` for the reference grid. Every one of them ends in a block that
states **what that campaign is for**, computed from its own data rather than written into
the prose — so a view cannot claim a finding its campaign does not support. Reading several
campaigns against each other is `analysis/compare.py`, above.

## Notes for anyone extending this

- **Start from the campaign that varies the channel you need.** A `.vast` reaches three
  surfaces and they are not interchangeable: `sim:` writes into the compiled world (every
  campaign here), `sut:` rewrites the system under test's own config files, and `scenario:`
  sets scenario parameters. `nav_search_minimax.vast` is the one that uses all three.
- **A `sut:` source needs nothing in the `.vast` to reach the trial.** Staging gives each
  configuration its own rewritten copy at `/config/<path>` — the declared path, where the
  campaign's copy would have been — and drops the original from `run_files`, so exactly one
  copy exists and it is the running cell's. The trial finds it by writing the ordinary path
  relative to its own directory, which is that mount.
- **`rosbags_to_csv` names a topic's table `rosbag2_<topic>`**, not `<topic>`.
- **The ground-truth arrival radius is not nav2's `xy_goal_tolerance`.** nav2 declares
  success against its estimated pose at the instant it stops; the metric measures ground
  truth at the last recorded sample, which can lie farther from the goal, so scoring arrival
  against the planner's tolerance counts runs that passed as failures.
- **Check that a factor axis actually spans outcomes before trusting a sweep.** Over a range
  of doorway widths the robot passes at every value, the geometry contributes nothing and
  every failure comes from the walker.
