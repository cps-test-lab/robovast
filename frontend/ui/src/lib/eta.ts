// How long the current batch — and, for a search, the batches budget — still has to run.
//
// Everything here is scoped to the CURRENT BATCH, because that is the scope the status
// itself reports: `runs` resets every batch (see RunProgress), so the only clock those
// counters can be read against is `batch_since`, which resets with them. The campaign's
// own `started_at` was used for this once and coincides with the batch start only for
// batch 0 — which is exactly why a search past its first round had no estimate at all.

import type { BudgetItem, JobCounts, Status } from './robovastClient'

/** Runs of this batch that will never deliver: their job is dead.
 *
 * Live where the status is not. `runs.no_result` is written only when the batch closes
 * (the progress poller writes `completed` and `total` and nothing else), so while the
 * batch runs the job counts are the only source — and a run counted as pending until the
 * batch ends is one the estimate keeps waiting for. */
export function noResultRuns(status: Status, counts?: JobCounts): number {
  return counts?.failed ?? status.runs.no_result
}

/** Runs that have reached a terminal state, delivered or not.
 *
 * The one number the runs line displays, the meter paints, and the estimate divides by,
 * so the three cannot disagree — they did, and a dead job read as finished to the meter
 * and as still pending to the other two. Clamped to `total` because `completed` is
 * clamped by the controller and the live failure count is not: the two settle a moment
 * apart, and that moment must not print `41/40`. */
export function finishedRuns(status: Status, counts?: JobCounts): number {
  const { runs } = status
  return Math.min(runs.total, runs.completed + noResultRuns(status, counts))
}

/** Seconds until the current batch's runs are done, or null when it can't be stated.
 *
 * A throughput figure — wall-clock per finished run, across however many jobs the lane
 * runs at once — so it needs to know nothing about parallelism. It inherits that model's
 * one bias: a batch's tail finishes at falling parallelism, so the last few runs take
 * slightly longer than the average projects.
 *
 * Null rather than a guess when nothing has finished yet: with no completed run there is
 * no rate, and inventing one would put a number on the screen that no observation backs. */
export function estimateEtaSeconds(
  status: Status,
  counts: JobCounts | undefined,
  terminal: boolean,
): number | null {
  if (terminal || !status.batch_since) return null
  const done = finishedRuns(status, counts)
  if (status.runs.total <= 0 || done <= 0) return null
  const elapsed = Date.now() / 1000 - status.batch_since
  if (!(elapsed > 0)) return null
  return (status.runs.total - done) * (elapsed / done)
}

/** Whether a budget row is the BATCH COUNTER — the one row that can be read as rounds.
 *
 * `kind` is the authority, never the label: the label is the criterion type only for `batches`
 * and `time` and the user's own metric or objective name otherwise (see CriterionProgress), so
 * matching text alone would treat a metric somebody named `batches` as the round counter.
 *
 * The fallback exists because `kind` is younger than the campaigns that have to render. It is
 * documented as `None` on a status written before the field existed, and a finished campaign's
 * budget is replayed from the `outcome.json` its controller wrote at the time — so every campaign
 * that ran before `kind` shipped reports `null` here forever. Without the fallback those cards
 * silently lose both the batches ETA and the objective chart, which is every campaign now on the
 * page. Only consulted when `kind` is absent, so a modern status still decides on `kind` alone:
 * a metric named `batches` there carries `kind: "metric"` and is correctly refused. */
export function isBatchesBudget(b: BudgetItem): boolean {
  return b.kind == null ? b.label === 'batches' : b.kind === 'batches'
}

/** The criterion bounding a search's ROUNDS, or null when nothing bounds them.
 *
 * A search always has rounds -- it asks, runs, tells, repeats -- and a `batches` criterion is
 * one way to bound them, not what creates them. A campaign bounded by runs, time or
 * evaluations therefore has a round counter and an objective trajectory just the same, and
 * gets null here rather than nothing to show: the card renders its rounds either way and only
 * omits the meter, since there is no limit to measure against and inventing a denominator
 * would put a bound on screen that the campaign never declared.
 *
 * The CHART must not be driven off the budget list the same way: a runs-bounded search declares
 * no batch budget, so doing that leaves it with no batch counter, no ETA and no objective diagram
 * at all -- six of the eight shipped `nav_search` examples, including the Halton campaign whose
 * runs budget is deliberate (see nav_search_random.vast: bounding by runs is what makes the two
 * strategies comparable). */
export function batchesBudget(status: Status): BudgetItem | null {
  return status.budget.find(isBatchesBudget) ?? null
}

/** Seconds until the `batches` budget is exhausted, or null when that is not this row.
 *
 * The current batch's remaining time, plus a whole batch for each round after it — each
 * projected from the LIVE run rate rather than from an average of the rounds already
 * done. Every round draws `per_batch` parameter sets with the same repetition count, so
 * the rounds are equal-sized by construction and the current rate is the better
 * predictor of the next one; it also needs no per-batch history on the wire.
 *
 * Which row is the batch counter is `isBatchesBudget`'s question, not this one's.
 *
 * It answers when this BUDGET runs out, not when the search stops: any other stopping
 * criterion may fire first. That is the same honesty the runs estimate has — it says
 * when the work in front of it ends, not what happens next. */
export function estimateBatchesEtaSeconds(
  status: Status,
  counts: JobCounts | undefined,
  b: BudgetItem,
  runsEta: number | null,
): number | null {
  if (!isBatchesBudget(b) || runsEta === null || !status.batch_since) return null
  const perRun = (Date.now() / 1000 - status.batch_since) / finishedRuns(status, counts)
  // `current` is written when a round ENDS, so during round k it still reads k — the one
  // in flight is already covered by runsEta, hence the extra -1. Floored, because between
  // two rounds the count has advanced and the next batch has not begun.
  const remaining = Math.max(0, b.limit - (b.current ?? 0) - 1)
  return runsEta + remaining * status.runs.total * perRun
}

/** Seconds until the CAMPAIGN is done, or null when no honest estimate exists.
 *
 * Everything above is scoped to the current batch, because that is the scope the status reports.
 * A collapsed running row asks a different question -- when is this campaign finished -- and for a
 * SEARCH the batch answer is never the campaign answer: a search runs an unknown number of rounds,
 * so its current round's remaining time says nothing about the whole. Showing it there would not
 * be a rough answer, it would be a wrong one, and a search that has just started its sixth of six
 * rounds would claim the same "~10 min left" as one starting its first.
 *
 * So a search is estimated from what BOUNDS IT, never from its current round:
 *
 * - a **`batches`** criterion -- the rounds still to come, via `estimateBatchesEtaSeconds`;
 * - a **`runs`** criterion -- the runs still to come, at the rate this batch is achieving.
 *
 * Both are projected from the LIVE per-run rate rather than an average over past rounds: every
 * round draws the same number of parameter sets with the same repetition count, so the rounds are
 * equal-sized by construction and the current rate is the better predictor of the next one.
 *
 * With **both**, the smaller wins -- the campaign stops at whichever criterion fires first, so the
 * larger one describes a moment this campaign will never reach.
 *
 * With **neither**, null. The remaining criteria in the stopping vocabulary (`target_objective`,
 * `no_improvement`, `metric`, `evaluations`) cannot be converted into a time: three of them fire on
 * a value nothing can project, and an evaluation is a parameter set whose run count this does not
 * know. The row then shows nothing, which is what "not known" looks like -- a dash would read as a
 * value. (`time` COULD be converted, and deliberately is not yet; see the plan.)
 *
 * A campaign in `batch` mode has exactly one batch, so its batch estimate IS its campaign
 * estimate and is returned unchanged. Null is also what anything with no finished run yet gets,
 * from `estimateEtaSeconds`: with no completed run there is no rate, and a rate invented from zero
 * observations is not an estimate.
 *
 * Like `estimateBatchesEtaSeconds`, this answers when the WORK runs out, not when the search
 * stops: a `no_improvement` or `target_objective` criterion may end it earlier. */
export function campaignEtaSeconds(
  status: Status,
  counts: JobCounts | undefined,
  terminal: boolean,
): number | null {
  const batchEta = estimateEtaSeconds(status, counts, terminal)
  if (status.mode !== 'search') return batchEta
  if (batchEta === null || !status.batch_since) return null
  const done = finishedRuns(status, counts)
  if (done <= 0) return null
  const perRun = (Date.now() / 1000 - status.batch_since) / done
  const bounded: number[] = []
  for (const b of status.budget) {
    if (isBatchesBudget(b)) {
      const rounds = estimateBatchesEtaSeconds(status, counts, b, batchEta)
      if (rounds !== null) bounded.push(rounds)
    } else if (b.kind === 'runs' && b.limit > 0) {
      // `current` is the campaign's completed runs, not this batch's, so the remainder already
      // covers the rest of the current round as well as every round after it.
      bounded.push(Math.max(0, b.limit - (b.current ?? 0)) * perRun)
    }
  }
  return bounded.length ? Math.min(...bounded) : null
}

/** The criterion kinds that can be read as a FRACTION OF WORK SPENT.
 *
 * Exactly `search.budget`'s vocabulary (see common/config.py: BudgetCriterion), and that split is
 * the whole rule: a budget row is a monotone resource cap, so `current / limit` is a share of
 * something. A `stopping` row is a result-dependent early exit and is not:
 *
 * - `target_objective` — `current` is the objective itself, so the quotient is not in [0,1]. It is
 *   direction-dependent, can start on either side of the target, and is `null` until the first
 *   result lands.
 * - `no_improvement` — `stale_batches` RESETS TO 0 on an improvement, so a ring driven by it would
 *   run backwards. It is a countdown to stopping, not progress through anything.
 * - `metric` — the criterion carries an `op` (`>=`, `<=`, `>`, `<`) that decides which side
 *   satisfies it, and `op` is not on the wire. A `<=` metric at 0.1/0.8 is already SATISFIED and
 *   would draw as 12% — "barely started" for a campaign about to stop.
 *
 * An ALLOWLIST rather than a denylist of the stopping kinds, so a criterion kind added later is
 * refused until somebody has decided it is monotone. Getting that wrong silently draws a
 * meaningless arc; getting it wrong the other way just omits one. */
const FRACTIONABLE_KINDS: ReadonlySet<string> = new Set([
  'runs',
  'batches',
  'evaluations',
  'time',
])

/** A budget row with a `time` row's `current` brought up to date from the search's origin.
 *
 * The TS mirror of `budget_positions` in `robovast_client/robovast/client/status.py`, which the
 * Python readers (the MCP's progress figure and status dict, the CLI's budget line) share. A change
 * to either must be made looking at the other.
 *
 * Only `time` is derived, because it is the only criterion whose value is a pure function of
 * wall-clock: it is published from `stop.progress()`, which runs once per batch, so on the wire it
 * steps per round instead of ticking. Everything else is a count the controller writes when it
 * actually changes.
 *
 * Deriving it here rather than having the controller republish it is deliberate and is the reason
 * `search_since` exists: a budget row rewritten from wall-clock would advance the controller's
 * progress signal on every poll, and no time-budgeted search could ever be reported stalled again.
 *
 * Clamped to `limit` (a search stops when the criterion fires, so elapsed past the cap is time it
 * did not spend) and left untouched when no origin was published — a batch campaign, or a status
 * recovered from disk. The published `current` is then stale by at most one round, which is better
 * than derived from an origin nobody wrote.
 */
export function budgetPosition(b: BudgetItem, status: Status): BudgetItem {
  if (b.kind !== 'time' || status.search_since == null || !(b.limit > 0)) return b
  const elapsed = Date.now() / 1000 - status.search_since
  if (!(elapsed >= 0)) return b
  return { ...b, current: Math.min(elapsed, b.limit) }
}

/** Whether this row is a budget cap whose share of work spent can be drawn.
 *
 * Same `kind`-over-`label` rule, and the same reason, as `isBatchesBudget`: the label is the
 * criterion type only for `batches` and `time`, so a metric the user named `runs` must not be
 * mistaken for the run cap. The pre-`kind` fallback matters here too — a campaign whose status was
 * written before `kind` shipped reports `null` forever, and without the fallback every historical
 * search would lose its arc. Only `batches` and `time` are recoverable from the label (they are the
 * two kinds whose label IS the type); a legacy `runs` or `evaluations` row is indistinguishable
 * from a user metric of that name, so it is refused rather than guessed at. */
export function isFractionableBudget(b: BudgetItem): boolean {
  if (b.kind == null) return b.label === 'batches' || b.label === 'time'
  return FRACTIONABLE_KINDS.has(b.kind)
}

/** What the rounds ring measures: the declared budget CLOSEST TO EXHAUSTING, or null when none is.
 *
 * The binding criterion, because the campaign stops at whichever fires first — so the row with the
 * greatest share is the one describing when this campaign actually ends, and any other describes a
 * moment it will never reach. The same rule the MCP's `_progress_from_status` applies server-side
 * (`max(current / limit)` over the budget) and the same rule `campaignEtaSeconds` below expresses
 * in time units, where "fires first" means the SMALLER duration. Three readers, one rule; a change
 * to any of them must be made looking at the other two.
 *
 * Null when nothing fractionable is declared, which is legal: validation requires one criterion
 * across `budget` AND `stopping` together, so a search bounded only by convergence has no
 * denominator at all. The ring then draws its bare track — no share is invented, exactly as before.
 *
 * `share` is clamped to [0,1]: `runs` is counted from what each batch ASKS FOR, before it runs, so
 * a final batch can carry the count past its own cap. */
export function ringBudget(
  status: Status,
): { item: BudgetItem; share: number } | null {
  let best: { item: BudgetItem; share: number } | null = null
  for (const raw of status.budget) {
    const item = budgetPosition(raw, status)
    if (!isFractionableBudget(item) || !(item.limit > 0) || item.current == null) continue
    const share = Math.max(0, Math.min(1, item.current / item.limit))
    if (best === null || share > best.share) best = { item, share }
  }
  return best
}

/** The comparison that makes a criterion fire, as a symbol to print.
 *
 * `>=` for a row written before `op` existed (a status from an older controller, or a finished
 * campaign replaying the `outcome.json` its controller wrote at the time). That is the correct
 * comparison for five of the seven kinds and is what every reader assumed before the field
 * shipped, so the fallback is the old behaviour rather than a guess. */
export function criterionOp(b: BudgetItem): string {
  return b.op ?? '>='
}

/** Whether a criterion's progress toward FIRING can be drawn as a share of something.
 *
 * A superset of `isFractionableBudget`: the four resource caps, plus `no_improvement`. That one
 * is the only `stopping` kind with a real floor -- `stale_batches` counts up from zero to
 * `patience`, so "2 of 3" is a genuine fraction and a bar is honest.
 *
 * Still false for the other two, and `op` does not change that. Knowing a `metric` fires at
 * `<= 0.8` says nothing about where it STARTED, so there is no denominator; the same is true of
 * an objective, whose initial value is whatever the first batch happened to measure. A bar needs
 * an origin, and those two have none -- so they get the comparison in words instead.
 *
 * `no_improvement` is deliberately absent from the RING's allowlist even though it is here: it
 * resets to zero on an improvement, and a ring that runs backwards reads as a bug. A static row
 * in the open card is a different claim -- "2 of 3 strikes" -- and survives the reset. */
export function hasDrawableFloor(b: BudgetItem): boolean {
  return isFractionableBudget(b) || b.kind === 'no_improvement'
}
