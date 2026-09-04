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

Three further files test claims the nine above cannot settle on their own:

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
something this directory could previously *compute* — they were prose. `analysis/compare.py`
is that missing half. It reads each campaign's own `campaign.db`, which carries the scored
cells **and** the `.vast` that produced them, so nothing about what a campaign is gets
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

## What one full execution of this directory measured

Every campaign here run once at its committed budget, one seed each. Read these as **one draw
per strategy**: a stochastic search compared once settles nothing, and the numbers that matter
below are the ones where the strategies differ qualitatively rather than by a ratio.

| campaign | cells | runs | worst found | runs to reach -1.599 |
|---|---|---|---|---|
| `cmaes` | 32 | 250 | -1.651 | **58** |
| `qd` | 8 | 63 | -1.653 | 63 |
| `random` | 60 | 179 | -1.661 | 179 |
| `tpe` | 32 | 255 | -1.601 | 191 |
| `halton` | 60 | 177 | -1.599 | never |
| `boundary` | 32 | 256 | -1.355 | never |

**The cost column is counted a batch at a time**, and that is not a detail. A search proposes
a batch, its cells run in parallel and their scores return together, so a campaign that found
the depth in the third cell of a batch still paid for the whole batch and nobody could have
stopped sooner. Counted cell by cell the same table reads 43 / 39 / 155 / 135 -- numbers no
operator could ever have realised, and which flatter whichever sampler happened to order a
good cell early within its batch.

`qd`'s row is the weakest evidence in the table and is kept for that reason. It was run under
a `time:` budget on a contended lane, which bought it 63 runs where its siblings had 180-256,
so its figures rest on eight scored cells. That measurement is why the file now declares a
`runs:` budget like the others -- wall-clock is not a measure of compute on a shared cluster,
and a campaign budgeted in it is not comparable with one budgeted in runs.

Re-run at `runs: 240` it spent its budget properly (256 runs, four batches) and still scored
only 16 cells, because two of those four batches went unscored on a saturated lane -- see the
scheduling note under *Notes for anyone extending this*. Both QD numbers here are therefore
about the lane as much as about the strategy, and neither should be quoted against the others
without that caveat.

**QD fills its own archive worse than uniform sampling does**, and that is not a lane
artifact. Of the 40 archive cells, a single `random` campaign occupied 14 (coverage 0.350)
and `halton` 13, against QD's 5 -- and `random` reached 8 cells in 44 runs, more than QD
managed in 128. Its `coverage >= 0.25` criterion is therefore comfortably reachable and has
simply never fired. Across every campaign here, ~2900 runs, 16 of the 40 cells have been
occupied at least once (0.400); the other 24 are structurally dead, because `timeout` never
occurs and a collision cannot land in a positive-clearance bin. A mutation step too small to
leave the region around its elites is what this looks like, so `sigma` is where to start.

**At full budget every strategy lands in the same place**: the five deepest worst cases fall
within 0.06 of each other. The searches do not find *worse* crossings than uniform sampling
does -- they find them **sooner**, which is the property that matters when a budget is cut
short. CMA-ES reached -1.599 in 58 runs against random's 179, a factor of 3.

**The shallow end is much closer.** Reaching -1.0 costs random 44 runs and CMA-ES 58 -- the
searches are no faster at finding *a* failure, because failures here are dense (60% of cells)
and a sampler that spreads out first is buying information it does not need. *Search buys
severity, not falsification.* Where failures are common, the first batch of anything finds
one, and the search earns nothing until depth is what is being asked for.

**Only a uniform sampler reports a rate.** `random` says 60% of cells fail, 95% CI
[47%, 71%]; `halton` says 63% [51%, 74%] -- two estimates of one quantity, agreeing. TPE's
72% is not an estimate of anything, because it biased its own sample by construction. Neither
is the grid's 93.8%: a lattice is not a uniform draw (a quarter of its cells sit at the
narrowest doorway) and it spends more repetitions per cell. Exhaustiveness does not confer
unbiasedness.

**Boundary tracing is a different job from severity seeking, and they are mutually
exclusive.** The boundary tracer put 53% of its cells within +-0.05 of the failure contour,
median |robustness| 0.032; every other strategy's median sits at 0.45-0.81. The adversarial
searches are the *worst* at it (TPE 28%, CMA-ES 16%, against random's 30%) because they run
away from the boundary toward depth. One campaign cannot give both.

**The advantage is a property of budget against volume, not of the algorithm.** Run the same
two samplers over six factors instead of two -- `nav_search_random_6d.vast` and
`nav_search_tpe_6d.vast`, identical in every other respect -- and the ranking inverts:

| | 2 factors | 6 factors |
|---|---|---|
| random, worst found | **-1.661** | -1.430 |
| tpe, worst found | -1.601 | **-1.578** |
| random, runs to -1.0 | 44 | 187 |
| tpe, runs to -1.0 | 63 | 125 |
| random, reaches -1.5 | 179 runs | **never** |
| tpe, reaches -1.5 | 191 runs | 248 runs |

Uniform sampling wins at two factors because ~60 draws very nearly saturate a smooth square,
leaving a surrogate nothing to exploit. Add four dimensions at the same budget and it cannot
reach a depth the search still reaches. **A benchmark that reports either result alone is
misleading**; they are one law seen at two points.

**Adaptive repetitions reallocate budget; they do not save it -- and at an equal budget that
is worth more than saving.** Given tpe's own 240 runs, the policy spent 1-2 repetitions on
seven cells and 10-12 on nineteen (mean 8.57, *above* the fixed 8 it is read against) and
reached -1.599 in 109 runs where fixed repetitions needed 191, finding the deepest crossing
of any campaign here at -1.671. The reason is the objective: `aggregate: worst` on a bimodal
cell gets truer the more often that cell is sampled, so spending repetitions where the
outcome is still in doubt buys accuracy rather than economy.

**That result only exists because the budgets were matched**, and it is the sharpest argument
for the rule stated above. The same policy given a third less money reaches -1.374 and reads
as a clear loss. Nothing about the policy changed between those two campaigns; only the
budget did, and the unequal comparison measured the budget rather than the policy. That is
why `nav_search_adaptive_reps.vast` now declares tpe's 240, and why `analysis/compare.py`
refuses to present an unequal pair as a contest.

**The nested search answers a question about the STACK, not about the world.** Every other
campaign here measures the system as configured; `minimax` chooses the configuration. Over
five candidate tunings its worst case spanned 0.69 of robustness -- and the most robust was
the *least* cautious of them, low inflation with high speed. The mechanism is in the grid
above: inflating more makes the planner refuse a narrow doorway outright (32 of 32 failures
at 0.4 m, none of them by contact), and a faster robot is through the crossing before the
walker reaches it. Caution costs robustness here, which no single-configuration campaign
could have shown. No tuning in the searched range reached a positive worst case, so its
`outer_best >= 0.05` criterion never fired: within these bounds the failure is in the
situation and not in the configuration.

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
| failures (of 32) | 32 | 15 | 10 | 11 |
| collisions | 0 | 15 | 10 | 10 |
| mechanism | planner **refuses**; never approaches | squeezes through, **hits the walker** | | |

Only the first column is a property of the doorway. Past 0.4 m failure and collision are
very nearly the same event — 15 of 15, 10 of 10, 10 of 11 — so the geometry decides only
whether the robot goes, and the walker decides whether it gets through. The one failure at
1.6 m that was *not* a collision is the reason this is stated as "very nearly": the robot
can also simply run out of time.

The axis is **not monotone**: 1.6 m fails more often than 1.2 m. The grid read by column is
where the reason lives:

| doorway \ dwell | 0.0 s | 2.0 s | 4.0 s | 6.0 s |
|---|---|---|---|---|
| 0.4 m | 8 | 8 | 8 | 8 |
| 0.8 m | 5 | 6 | 1 | 3 |
| 1.2 m | 4 | 5 | 0 | 1 |
| 1.6 m | 5 | 2 | 1 | 3 |

**Read the two tables as one draw each, not as constants of this world.** Both were measured
at this file's full budget — 128 runs — and an earlier full-budget campaign produced
31/16/14/19 across the widths and a different pattern of columns. What survived both, and is
therefore a property of the world rather than of a sample:

- 0.4 m fails at every walker phase, and never by contact. The planner refuses a gap that
  narrow against the TB4's ~0.36 m footprint.
- Past 0.4 m, a failure is a collision.
- The width axis is not monotone.

What did **not** survive: which walker phase is worst at a given width (`dwell: 0` in one
campaign, `dwell: 2` at two of the four widths in the other), and which of the two factors
dominates marginally — the doorway spans 69% of failure rate here against the walker's 38%,
and the earlier campaign ranked them the other way round.

That is the bimodality of the objective showing up one level higher: a cell has a
*probability* of failing rather than a robustness, so eight repetitions pin down the coarse
structure and still leave the per-cell counts rattling. `analysis/nav_grid.ipynb` therefore
computes which factor dominates from the campaign in front of it and names it, rather than
repeating a winner fixed in prose — a view that asserted one would have been wrong here.

**Four runs per width cannot see even the coarse structure**, and an earlier version of this
table read them as 4/3/1/1 with the two widest widths described as mostly passing. That
ordering was an artifact of the sample size.

## Budgets

The budgets here are **demo-sized** — a few hundred runs each. A research budget would be
considerably larger; they are small so the directory can be run through end to end, not
because these are the numbers to publish.

At ~25 s of driving per run plus bring-up, and with the lane to itself, a campaign is tens of
minutes. **Sharing the lane changes that by an order of magnitude, and not only in wall
clock.** Running all nine at once on a four-node cluster alongside another user's work, each
campaign took hours rather than minutes, one aborted before its first batch because a
contended node could not be calibrated, and one spent half its budget on batches that were
never scored — see the scheduling note under *Notes for anyone extending this*. Run them one
or two at a time if the results are meant to be compared with each other.

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
- **A `sut:` source needs a scenario parameter to land on.** Staging gives each configuration
  its own rewritten copy at `/config/<config-name>/<path>` and drops the original from
  `run_files`, so exactly one copy exists; the trial finds it because RoboVAST rewrites *a
  scenario parameter whose value is the source's declared path*. A parameter left at its
  `.osc` default is not one the campaign set, so nothing is rewritten and the trial launches
  a path that is no longer there. Declare it on the `scenario:` channel, as
  `nav_search_minimax.vast` does with `params_file`.
- **Do not saturate the lane a search is running on -- its own scoring is what loses the
  race.** A search scores each batch before proposing the next, and on a cluster that means a
  small conversion Job scheduled alongside the batch's runs. The runs are the bulk and get
  placed; the little job does not, and a batch whose conversion never ran has no
  `nav_metrics.csv` for the extractor to read. It refuses that batch -- correctly, and it says
  exactly why -- but the runs are already spent. Measured on a contended lane: a 256-run QD
  campaign had two of four batches go unscored, so **half its simulator bought nothing**, and
  its archive was built from half the feedback it paid for.

  `no_sample` units in `campaign.db` are where this shows up, and a campaign with a high
  `no_sample` count should be read as a scheduling failure rather than as a search that found
  nothing: `SELECT status, count(*) FROM unit GROUP BY status`. Re-running postprocessing
  afterwards recovers the per-run metrics, but **not the search** -- the proposals were made
  blind and cannot be un-made.

- **`rosbags_to_csv` writes `rosbag2_<topic>.csv`**, not `<topic>.csv`.
- **The ground-truth arrival radius is not nav2's `xy_goal_tolerance`.** nav2 declares
  success against its estimated pose at the instant it stops; the metric measures ground
  truth at the last recorded sample. Runs that *passed* ended 0.23–0.51 m out, so comparing
  against the planner's 0.25 scored most of them as maximum-severity failures.
- **Check that a factor axis actually spans outcomes before trusting a sweep.** An earlier
  version of the grid used doorway widths of 0.8–2.6 m, every one of which the robot passed
  — so the geometry contributed nothing and every failure came from the walker.
