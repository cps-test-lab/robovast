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

/** Seconds until the `batches` budget is exhausted, or null when that is not this row.
 *
 * The current batch's remaining time, plus a whole batch for each round after it — each
 * projected from the LIVE run rate rather than from an average of the rounds already
 * done. Every round draws `per_batch` parameter sets with the same repetition count, so
 * the rounds are equal-sized by construction and the current rate is the better
 * predictor of the next one; it also needs no per-batch history on the wire.
 *
 * Keyed on `kind`, never on `label`: the label is the criterion type only for `batches`
 * and `time`, and the user's own metric or objective name otherwise (see
 * CriterionProgress) — so matching the text would hang this estimate on a metric
 * somebody happened to name `batches`.
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
  if (b.kind !== 'batches' || runsEta === null || !status.batch_since) return null
  const perRun = (Date.now() / 1000 - status.batch_since) / finishedRuns(status, counts)
  // `current` is written when a round ENDS, so during round k it still reads k — the one
  // in flight is already covered by runsEta, hence the extra -1. Floored, because between
  // two rounds the count has advanced and the next batch has not begun.
  const remaining = Math.max(0, b.limit - (b.current ?? 0) - 1)
  return runsEta + remaining * status.runs.total * perRun
}
