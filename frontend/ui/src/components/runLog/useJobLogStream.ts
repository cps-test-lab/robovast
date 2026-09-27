// A running job's log, live: the rows the service parses from the log files the job's containers
// write, streamed over SSE and shaped as the `RunLogData` that `RunLogView` renders. The same view
// then reads a job while it runs and the merged `run_log` table once the campaign is ingested.
//
// What the live rows cannot carry, they leave empty rather than guess: `sim_time` comes from the
// clock map postprocessing builds, so every row is wall-time only and the view shows the wall
// offset from the first line; there is no verdict to cut the shutdown at, and no `/rosout` rows.

import type { LiveState } from '@/lib/liveStream'
import { robovast, type JobLogRow } from '@/lib/robovastClient'
import { liveLogNote, parseRowFrame, useLiveLogStream, type LiveLogKind } from './useLiveLogStream'
import type { LogRow } from './useRunLog'

/** `job_name` is `<config>/<run>`: how the service names the job that executes one run, and the
 *  two fields a run row of the results tree carries, so a run addresses its job with no lookup. */
export const jobNameOf = (configName: string, runId: number | string) =>
  `${configName}/${runId}`

/** One streamed row in the view's row shape. */
export function toLogRow(r: JobLogRow): LogRow {
  return {
    sim_time: null,
    wall_ts: r.wall_ts ?? null,
    time_source: r.time_source,
    // Every live row is inside the run: there is no clock window to fall outside of yet.
    in_window: 1,
    container: r.container,
    node: r.node,
    source: 'stdout',
    level: r.level,
    // The view's facets and severity steps read `other` for a row with no classification.
    severity: r.severity || 'other',
    message: r.message,
  }
}

const JOB_LOG: LiveLogKind<JobLogRow> = {
  name: 'job log',
  toRow: toLogRow,
  complete: "The job's log is complete.",
  live: 'Live: wall time only until the campaign is ingested.',
}

/** Parse one `data:` frame: a JSON array of JobLogRow. */
export const parseFrame = (data: string): LogRow[] => parseRowFrame(JOB_LOG, data)

/** The footer line: what the reader must know to read the live log right. */
export const liveNote = (o: { dropped: number; eof: boolean; state: LiveState }): string =>
  liveLogNote(JOB_LOG, o)

/** Stream one job's log. `jobName` null opens nothing. */
export function useJobLogStream(campaignId: string, jobName: string | null) {
  const url = jobName ? robovast.jobLogStreamUrl(campaignId, jobName) : null
  return useLiveLogStream(JOB_LOG, url, `${campaignId}/${jobName ?? ''}`)
}
