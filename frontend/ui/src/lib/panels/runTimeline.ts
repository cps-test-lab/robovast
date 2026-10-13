// useRunTimeline: put one run's timeline on its playback clock, and for a run still recording keep
// it growing with the rows and the clock following its edge. Shared by every view that shows a run
// over a clock -- the run view, and the 3D view beside a job's live log on the Campaigns page -- so
// "where does this run's time start and end" has one answer.

import { useEffect, useState } from 'react'
import type { DataRow, PlaybackClock } from '@robovast/panel-kit'
import type { LiveDataProvider } from './dataProvider'
import { ANY_TABLE } from './liveFeed'

/** Tables whose timestamp column can define the run's timeline; the union of their ranges is used.
 *  The fallback for a campaign that declares no `visualization.timeline`: the simulator's own pose
 *  table, and the postprocessed rosbag tables. */
export const TIME_TABLES = ['sim_poses', 'poses', 'behaviors', 'scenario_timestamps']

/** A campaign's declared `visualization.timeline`, as `/panels` serves it. */
export interface RunTimeline {
  table?: string | null
  time_column?: string | null
}

/** The widest time span the rows of a batch cover in `timeCol`, or null when none carries one. */
export function batchSpan(rows: DataRow[], timeCol: string): [number, number] | null {
  let lo = Infinity
  let hi = -Infinity
  for (const row of rows) {
    const t = Number(row[timeCol])
    if (!Number.isFinite(t)) continue
    if (t < lo) lo = t
    if (t > hi) hi = t
  }
  return lo <= hi ? [lo, hi] : null
}

/** Discover the run's range and set it on the clock, in order of authority:
 *    1. an explicit `visualization.timeline` (a sim's own table with a `t` column);
 *    2. the union of the standard time tables (the simulator's poses, the postprocessed nav ones).
 *
 *  For a live run the range is read the same way once, then grows with the rows: every batch the
 *  provider's stream delivers -- whichever table, since the tables the panels read are the ones the
 *  socket carries -- moves `hi` to its latest sample, and the clock follows. When the stream ends the
 *  range is read once more (the finalised tables) and following ends there.
 *
 *  `openAtEnd` seats a finished run's cursor at the end of its trial rather than at its start: a
 *  view that shows what a run is doing now shows where a run that is over ended up.
 *
 *  Returns what the live stream refused or could not read, in the service's words. The panels only
 *  stop moving when it happens, so the caller is the one place that can say why. */
export function useRunTimeline(
  provider: LiveDataProvider | null,
  clock: PlaybackClock,
  timeline: RunTimeline | null | undefined,
  { openAtEnd = false }: { openAtEnd?: boolean } = {},
): string | null {
  const tlTable = timeline?.table ?? undefined
  const tlCol = timeline?.time_column ?? undefined
  const [liveError, setLiveError] = useState<string | null>(null)
  useEffect(() => setLiveError(null), [provider])
  useEffect(() => {
    if (!provider) return
    let alive = true
    const timeCol = tlCol ?? 'timestamp'

    const readRange = async (): Promise<[number, number] | null> => {
      const lookups = tlTable
        ? [provider.timeRange(tlTable, tlCol).catch(() => null)]
        : TIME_TABLES.map((t) => provider.timeRange(t).catch(() => null))
      const valid = (await Promise.all(lookups)).filter((r): r is [number, number] => !!r)
      if (!valid.length) return null
      return [Math.min(...valid.map((r) => r[0])), Math.max(...valid.map((r) => r[1]))]
    }

    let haveRange = false
    const opened = readRange().then((range) => {
      if (!alive || !range) return
      haveRange = true
      clock.setRange(range[0], range[1])
      if (provider.live) clock.follow()
    })

    const unsubscribe = provider.live
      ? provider.subscribeLive(ANY_TABLE, (event) => {
          if (!alive) return
          if (event.kind === 'batch') {
            // A table with its own time column is not the timeline's; the declared one, or the
            // default, is what the rows are measured in.
            if (tlTable && event.table !== tlTable) return
            const span = batchSpan(event.rows, timeCol)
            if (!span) return
            const { lo, hi } = clock.getSnapshot()
            if (haveRange) {
              clock.setRange(Math.min(lo, span[0]), Math.max(hi, span[1]))
            } else {
              haveRange = true
              clock.setRange(span[0], span[1])
              clock.follow()
            }
          } else if (event.kind === 'error') {
            setLiveError(event.message)
          } else if (event.kind === 'eof') {
            readRange().then((range) => {
              if (!alive) return
              if (range) clock.setRange(range[0], range[1])
              clock.finish()
            })
          }
        })
      : null

    // Where the *trial* ended, which the range above deliberately does not encode: `setRange`
    // is the whole recording, so showing the shutdown phase restores it without re-querying.
    // Not for a live run: it has no verdict yet by definition, and the toggle says there is
    // nothing to trim, which is true until the run ends and the view becomes the finished run's.
    if (provider.live) clock.setVerdict(null)
    else {
      const verdict = provider
        .timeRange('scenario_timestamps')
        .catch(() => null)
        .then((range) => {
          // `[t, t]`: one row per run. Null for a run that reached no verdict, and for a
          // campaign postprocessed before the verdict was recorded -- the toggle then says
          // there is nothing to trim rather than trimming to an invented moment.
          if (alive) clock.setVerdict(range ? range[1] : null)
        })
      if (openAtEnd) {
        Promise.all([opened, verdict]).then(() => {
          if (alive) clock.seek(clock.getSnapshot().hi)
        })
      }
    }
    return () => {
      alive = false
      unsubscribe?.()
    }
  }, [provider, clock, tlTable, tlCol, openAtEnd])
  return liveError
}
