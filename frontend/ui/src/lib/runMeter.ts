// What a campaign's run meter paints, as numbers.
//
// One definition, because the campaign card now draws that meter twice: full width when the card is
// open, and short inside the header row when it is collapsed. Those two must agree — a reader who
// folds a card open to check a red segment is checking THE SAME BAR — and the surest way to keep
// them agreeing is that neither of them owns the arithmetic.
//
// Markup stays in `components/`; this is the same split `lib/eta.ts` and `lib/detailsGeometry.ts`
// already make, and the reason those have tests while the charts do not.

import type { CampaignSummary, JobCounts, Status } from './robovastClient'
import type { MeterSegment } from '@/components/MeterBar'
import { finishedRuns, noResultRuns } from './eta'

/** The stacked regions of a campaign's run meter, left to right.
 *
 * Green is *successes*, not "produced a result": `runs.completed` counts every run that delivered a
 * result artifact, including the ones whose own verdict is a failure, so painting `completed` green
 * reported a campaign whose every trial failed as fully passed. The two failure axes the status
 * keeps apart stay apart here too — a trial that ran and failed is solid red, a run that delivered
 * nothing is the dimmer red.
 *
 * `running` stays last: `MeterBar` clamps the running offset but does not rescale, so a transient
 * over-100% sum clips the final segment — and what is still running is the least final thing to
 * lose. */
export function runMeterSegments(status: Status, counts?: JobCounts): MeterSegment[] {
  const { runs } = status
  if (runs.total <= 0) return []
  const succeeded = Math.max(0, runs.completed - runs.failed)
  return [
    { fraction: succeeded / runs.total, color: 'success.main' },
    { fraction: runs.failed / runs.total, color: 'error.main' },
    { fraction: noResultRuns(status, counts) / runs.total, color: 'error.main', opacity: 0.45 },
    { fraction: (counts?.running ?? 0) / runs.total, color: 'info.main', striped: true },
  ]
}

/** The share done, as a whole percent — the label the meter carries inside its track.
 *
 *  A percentage stays the same width as a campaign's run count grows; the counts themselves are on
 *  the meter's hover (`MiniRunMeter`'s `done` fact).
 *
 *  Uses `finishedRuns` so the label, the painted fraction and the ETA cannot disagree — see the
 *  note there. With no denominator there is no share to state, so the bar goes unlabelled rather
 *  than claiming 0%. */
export function runMeterText(status: Status, counts?: JobCounts): string {
  const total = status.runs.total
  if (total <= 0) return ''
  return `${Math.round((finishedRuns(status, counts) / total) * 100)}%`
}

/** A `RunProgress` from a campaign LISTING, for the moment before the live status arrives.
 *
 * The collapsed row is drawn from the campaign stream, which arrives for the whole page at once,
 * while `getStatus` is one request per card. Without this the meters paint empty and fill in one by
 * one as those replies land — a page that looks like it is still loading long after it is readable.
 *
 * It is a first paint, not a second source of truth: the status supersedes it the moment it
 * arrives, and it must, because the summary cannot express one thing the status can. `num_runs`
 * counts runs that HAPPENED, not runs that were planned, so a stopped or crashed campaign has no
 * denominator here and its bar reads full until the real total lands. Nothing else is lost: the
 * three summary tallies map onto the status fields exactly. */
export function runsFromSummary(summary: CampaignSummary): Status['runs'] {
  const completed = summary.num_passed + summary.num_failed
  return {
    completed,
    total: summary.num_runs,
    failed: summary.num_failed,
    // Everything the run table recorded that is neither a pass nor a failed verdict: errored,
    // killed, invalidated and unknown runs. The summary does not break those out (the status
    // does), and they are all runs that delivered no standing verdict, which is what this axis is.
    no_result: Math.max(0, summary.num_runs - completed),
    // Not "none were killed" — the listing does not carry these two at all, and they are folded
    // into `no_result` above. Zero here is the shape `RunProgress` demands, and nothing on the
    // meter path reads them; the live status is what reports them.
    killed: 0,
    invalid: 0,
  }
}
