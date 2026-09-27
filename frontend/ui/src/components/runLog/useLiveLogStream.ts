// What the live logs have in common: rows streamed over SSE as JSON row arrays, a bounded buffer of
// them shaped as the `RunLogData` that `RunLogView` renders, and the footer that says how to read
// it. The campaign log and a job's log differ only in the row they receive, how it maps onto the
// view's row, and the words they use -- which is what `LiveLogKind` carries.

import { useEffect, useMemo, useState } from 'react'
import { useLiveStream, type LiveState } from '@/lib/liveStream'
import type { LogRow, RunLogData } from './useRunLog'

/** How many rows the live view keeps. The oldest go first, and the view says how many did. */
export const MAX_LIVE_ROWS = 20000

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

/** The live rows as the view's data: wall time only, one scope, nothing to cut at. */
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

/** One kind of live log: its row, and the words its errors and footer use. */
export interface LiveLogKind<Row> {
  /** What the log is called in an error: `job log`, `campaign log`. */
  name: string
  toRow: (row: Row) => LogRow
  /** The footer once the log is complete. */
  complete: string
  /** The footer while rows are still arriving. */
  live: string
}

/** Parse one `data:` frame: a JSON array of `Row`. Throws on anything else, so a frame the
 *  client cannot read surfaces as an error instead of a log that silently skips lines. */
export function parseRowFrame<Row>(kind: LiveLogKind<Row>, data: string): LogRow[] {
  const parsed: unknown = JSON.parse(data)
  if (!Array.isArray(parsed)) throw new Error(`${kind.name} frame is not a row array`)
  return (parsed as Row[]).map(kind.toRow)
}

/** The footer line: what the reader must know to read the live log right. */
export function liveLogNote<Row>(
  kind: LiveLogKind<Row>,
  o: { dropped: number; eof: boolean; state: LiveState },
): string {
  const parts: string[] = []
  if (o.dropped > 0)
    parts.push(`Showing the newest ${MAX_LIVE_ROWS} lines; ${o.dropped} earlier lines dropped.`)
  if (o.eof) parts.push(kind.complete)
  else if (o.state === 'reconnecting' || o.state === 'closed') parts.push('Reconnecting…')
  else parts.push(kind.live)
  return parts.join(' ')
}

/** Stream one live log from `url`; `null` opens nothing. A change of `resetKey` is a new stream. */
export function useLiveLogStream<Row>(kind: LiveLogKind<Row>, url: string | null, resetKey: string) {
  const [live, setLive] = useState<LiveRows>(NO_ROWS)
  const [eof, setEof] = useState(false)
  const [error, setError] = useState<Error | undefined>(undefined)

  const { state, received, finish, generation } = useLiveStream(url, {
    resetKey,
    onMessage: (e) => {
      let add: LogRow[]
      try {
        add = parseRowFrame(kind, String(e.data))
      } catch (err) {
        setError(new Error(`unreadable ${kind.name} frame: ${(err as Error).message}`))
        finish()
        return
      }
      setLive((prev) => appendRows(prev, add))
    },
    events: {
      // An application error the server chose to surface (no such campaign or job, a filter it
      // does not know, unreadable files, …).
      streamerror: (e) => {
        let msg = `${kind.name} stream error`
        try {
          msg = String(JSON.parse(e.data))
        } catch {
          /* keep the generic message */
        }
        setError(new Error(msg))
        finish()
      },
      // The log is complete; closing deliberately keeps the watchdog from reopening it.
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
      note: liveLogNote(kind, { dropped: live.dropped, eof, state }),
    }),
    [kind, live, url, received, eof, error, state],
  )
}
