// A running job's log, live: the rows the service parses from the log files the job's containers
// write, streamed over SSE and shaped as the `RunLogData` that `RunLogView` renders. The same view
// then reads a job while it runs and the merged `run_log` table once the campaign is ingested.
//
// What the live rows cannot carry, they leave empty rather than guess: `sim_time` comes from the
// clock map postprocessing builds, so every row is wall-time only and the view shows the wall
// offset from the first line; there is no verdict to cut the shutdown at, and no `/rosout` rows.

import { useEffect, useMemo, useState } from 'react'
import { useLiveStream, type LiveState } from '@/lib/liveStream'
import { robovast, type JobLogRow } from '@/lib/robovastClient'
import type { LogRow, RunLogData } from './useRunLog'

/** `job_name` is `<config>/<run>`: how the service names the job that executes one run, and the
 *  two fields a run row of the results tree carries, so a run addresses its job with no lookup. */
export const jobNameOf = (configName: string, runId: number | string) =>
  `${configName}/${runId}`

/** How many rows the live view keeps. The oldest go first, and the view says how many did. */
export const MAX_LIVE_ROWS = 20000

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

/** The rows held so far, and how many older ones were dropped to stay within the bound. */
export interface LiveRows {
  rows: LogRow[]
  dropped: number
}

export const NO_ROWS: LiveRows = { rows: [], dropped: 0 }

/** Append a frame's rows, keeping the newest `max`. */
export function appendRows(prev: LiveRows, add: LogRow[], max = MAX_LIVE_ROWS): LiveRows {
  if (!add.length) return prev
  const all = prev.rows.concat(add)
  const over = all.length - max
  if (over <= 0) return { rows: all, dropped: prev.dropped }
  return { rows: all.slice(over), dropped: prev.dropped + over }
}

/** Parse one `data:` frame: a JSON array of JobLogRow. Throws on anything else, so a frame the
 *  client cannot read surfaces as an error instead of a log that silently skips lines. */
export function parseFrame(data: string): LogRow[] {
  const parsed: unknown = JSON.parse(data)
  if (!Array.isArray(parsed)) throw new Error('job log frame is not a row array')
  return (parsed as JobLogRow[]).map(toLogRow)
}

/** The live rows as the view's data. One job is one run, so the scope is a single run. */
export function liveRunLogData(live: LiveRows): RunLogData {
  return {
    rows: live.rows,
    simTimes: [],
    simIndex: [],
    clock: null,
    singleRun: true,
    verdict: null,
    truncated: false,
    missingTable: false,
    total: live.rows.length,
  }
}

/** The footer line: what the reader must know to read the live log right. */
export function liveNote(o: { dropped: number; eof: boolean; state: LiveState }): string {
  const parts: string[] = []
  if (o.dropped > 0)
    parts.push(`Showing the newest ${MAX_LIVE_ROWS} lines; ${o.dropped} earlier lines dropped.`)
  if (o.eof) parts.push('The job\'s log is complete.')
  else if (o.state === 'reconnecting' || o.state === 'closed') parts.push('Reconnecting…')
  else parts.push('Live: wall time only until the campaign is ingested.')
  return parts.join(' ')
}

/** Stream one job's log. `jobName` null opens nothing. */
export function useJobLogStream(campaignId: string, jobName: string | null) {
  const [live, setLive] = useState<LiveRows>(NO_ROWS)
  const [eof, setEof] = useState(false)
  const [error, setError] = useState<Error | undefined>(undefined)

  const url = jobName ? robovast.jobLogStreamUrl(campaignId, jobName) : null
  const resetKey = `${campaignId}/${jobName ?? ''}`

  const { state, received, finish, generation } = useLiveStream(url, {
    resetKey,
    onMessage: (e) => {
      let add: LogRow[]
      try {
        add = parseFrame(String(e.data))
      } catch (err) {
        setError(new Error(`unreadable job log frame: ${(err as Error).message}`))
        finish()
        return
      }
      setLive((prev) => appendRows(prev, add))
    },
    events: {
      // An application error the server chose to surface (no such job, unreadable files, …).
      streamerror: (e) => {
        let msg = 'job log stream error'
        try {
          msg = String(JSON.parse(e.data))
        } catch {
          /* keep the generic message */
        }
        setError(new Error(msg))
        finish()
      },
      // The job's logs are complete; closing deliberately keeps the watchdog from reopening it.
      eof: () => {
        setEof(true)
        finish()
      },
    },
  })

  // A connection this hook opened carries no Last-Event-ID, so the server starts from the first
  // row and what is held must go first. The browser's own reconnect resumes from the cursor and
  // keeps the rows (the generation does not change).
  useEffect(() => {
    setLive(NO_ROWS)
    setEof(false)
    setError(undefined)
  }, [generation, resetKey])

  return useMemo(
    () => ({
      data: liveRunLogData(live),
      // Pending until the server has spoken: an open socket with no frame is a first read still
      // in flight, not an empty log.
      isPending: !!url && !received && !live.rows.length && !eof && !error,
      error,
      note: liveNote({ dropped: live.dropped, eof, state }),
    }),
    [live, url, received, eof, error, state],
  )
}
