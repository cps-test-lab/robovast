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
 * Reading this off the budget list is what the card used to do for the CHART as well, which is
 * why a runs-bounded search had no batch counter, no ETA and no objective diagram at all --
 * six of the eight shipped `nav_search` examples, including the Halton campaign whose runs
 * budget is deliberate (see nav_search_random.vast: bounding by runs is what makes the two
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
