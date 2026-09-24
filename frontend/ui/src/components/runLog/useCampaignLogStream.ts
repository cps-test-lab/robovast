// A campaign's infrastructure log, live: the rows the service reads from the phase files under
// the campaign's `_execution/`, streamed over SSE and shaped as the `RunLogData` that `RunLogView`
// renders -- the phase in the container column, the logger in the node column.
//
// What a campaign row cannot carry, it leaves empty rather than guess: there is no simulator clock
// behind an infrastructure log, so every row is wall-time only and the view shows the wall offset
// from the first line; there is no verdict to cut a shutdown at.

import { useEffect, useMemo, useState } from 'react'
import { useLiveStream, type LiveState } from '@/lib/liveStream'
import { robovast, type CampaignLogQuery, type CampaignLogRow } from '@/lib/robovastClient'
import { appendRows, liveRunLogData, MAX_LIVE_ROWS, NO_ROWS, type LiveRows } from './useJobLogStream'
import type { LogRow, RunLogData } from './useRunLog'

/** The severity the view colours a row by, from the level the service read. `NOTE` -- an
 *  unstamped line -- has none, and reads as `other` like any unclassified row. */
export function severityOfLevel(level: string): string {
  switch (level.toUpperCase()) {
    case 'WARN':
    case 'WARNING':
      return 'warn'
    case 'ERROR':
    case 'CRITICAL':
    case 'FATAL':
      return 'error'
    default:
      return 'other'
  }
}

/** One streamed row in the view's row shape. */
export function toLogRow(r: CampaignLogRow): LogRow {
  return {
    sim_time: null,
    wall_ts: r.wall_ts ?? null,
    time_source: r.wall_ts == null ? 'none' : 'stamp',
    in_window: 1,
    // The phase takes the container column: it is what a campaign log is faceted by.
    container: r.phase,
    node: r.logger,
    source: 'stdout',
    level: r.level,
    severity: severityOfLevel(r.level),
    message: r.message,
  }
}

/** Parse one `data:` frame: a JSON array of CampaignLogRow. Throws on anything else, so a
 *  frame the client cannot read surfaces as an error instead of a log that silently skips
 *  lines. */
export function parseFrame(data: string): LogRow[] {
  const parsed: unknown = JSON.parse(data)
  if (!Array.isArray(parsed)) throw new Error('campaign log frame is not a row array')
  return (parsed as CampaignLogRow[]).map(toLogRow)
}

/** The footer line: what the reader must know to read the live log right. */
export function liveNote(o: { dropped: number; eof: boolean; state: LiveState }): string {
  const parts: string[] = []
  if (o.dropped > 0)
    parts.push(`Showing the newest ${MAX_LIVE_ROWS} lines; ${o.dropped} earlier lines dropped.`)
  if (o.eof) parts.push("The campaign's log is complete.")
  else if (o.state === 'reconnecting' || o.state === 'closed') parts.push('Reconnecting…')
  else parts.push('Live: rows arrive as the campaign writes them.')
  return parts.join(' ')
}

/** Stream one campaign's infrastructure log, narrowed to `query` by the service. */
export function useCampaignLogStream(campaignId: string, query: CampaignLogQuery = {}) {
  const [live, setLive] = useState<LiveRows>(NO_ROWS)
  const [eof, setEof] = useState(false)
  const [error, setError] = useState<Error | undefined>(undefined)

  const url = robovast.campaignLogStreamUrl(campaignId, query)
  // The URL carries the filters, so a change of either is a new stream.
  const resetKey = url

  const { state, received, finish, generation } = useLiveStream(url, {
    resetKey,
    onMessage: (e) => {
      let add: LogRow[]
      try {
        add = parseFrame(String(e.data))
      } catch (err) {
        setError(new Error(`unreadable campaign log frame: ${(err as Error).message}`))
        finish()
        return
      }
      setLive((prev) => appendRows(prev, add))
    },
    events: {
      // An application error the server chose to surface (no such campaign, a filter it does
      // not know, …).
      streamerror: (e) => {
        let msg = 'campaign log stream error'
        try {
          msg = String(JSON.parse(e.data))
        } catch {
          /* keep the generic message */
        }
        setError(new Error(msg))
        finish()
      },
      // The campaign's log is complete; closing deliberately keeps the watchdog from reopening it.
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
      data: liveRunLogData(live) as RunLogData,
      // Pending until the server has spoken: an open socket with no frame is a first read still
      // in flight, not an empty log.
      isPending: !received && !live.rows.length && !eof && !error,
      error,
      note: liveNote({ dropped: live.dropped, eof, state }),
    }),
    [live, received, eof, error, state],
  )
}
