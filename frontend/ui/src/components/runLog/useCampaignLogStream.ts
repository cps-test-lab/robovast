// A campaign's infrastructure log, live: the rows the service reads from the phase files under
// the campaign's `_execution/`, streamed over SSE and shaped as the `RunLogData` that `RunLogView`
// renders -- the phase in the container column, the logger in the node column.
//
// What a campaign row cannot carry, it leaves empty rather than guess: there is no simulator clock
// behind an infrastructure log, so every row is wall-time only and the view shows the wall offset
// from the first line; there is no verdict to cut a shutdown at.

import type { LiveState } from '@/lib/liveStream'
import { robovast, type CampaignLogQuery, type CampaignLogRow } from '@/lib/robovastClient'
import { liveLogNote, parseRowFrame, useLiveLogStream, type LiveLogKind } from './useLiveLogStream'
import type { LogRow } from './useRunLog'

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

const CAMPAIGN_LOG: LiveLogKind<CampaignLogRow> = {
  name: 'campaign log',
  toRow: toLogRow,
  complete: "The campaign's log is complete.",
  live: 'Live: rows arrive as the campaign writes them.',
}

/** Parse one `data:` frame: a JSON array of CampaignLogRow. */
export const parseFrame = (data: string): LogRow[] => parseRowFrame(CAMPAIGN_LOG, data)

/** The footer line: what the reader must know to read the live log right. */
export const liveNote = (o: { dropped: number; eof: boolean; state: LiveState }): string =>
  liveLogNote(CAMPAIGN_LOG, o)

/** Stream one campaign's infrastructure log, narrowed to `query` by the service. */
export function useCampaignLogStream(campaignId: string, query: CampaignLogQuery = {}) {
  const url = robovast.campaignLogStreamUrl(campaignId, query)
  // The URL carries the filters, so a change of either is a new stream.
  return useLiveLogStream(CAMPAIGN_LOG, url, url)
}
