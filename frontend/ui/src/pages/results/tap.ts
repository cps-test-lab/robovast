// The run view's Now tap: what a live run's simulator is publishing at this moment, relayed by the
// service from a command it starts in the run's simulation container (`GET .../job-tap`). Off by
// default and opened only on the reader's word, because a tap is recorded against the run as a
// probe -- a process the service started runs in the simulator's container while it is on.
//
// The pure half: which topics the view's panels name (the selection a tap opens with), the URL,
// the frames, and the bounded tail of lines it keeps. The hook below wires them to the stream.

import { useEffect, useMemo, useState } from 'react'
import { useLiveStream, type LiveState } from '@/lib/liveStream'
import { robovast } from '@/lib/robovastClient'
import type { PanelSpec } from '@/lib/panels/types'

/** How long one tap follows before the service ends it; the Now toggle reopens on its own. */
export const TAP_SECONDS = 60

/** How many lines the tail keeps. The oldest go first, and the note says how many did. */
export const MAX_TAP_LINES = 2000

/** One relayed line, as the service sends it. */
export interface TapLine {
  t_wall: number
  line: string
}

/** How the tap ended: the command's exit code, or null when the reader closed it first. */
export interface TapEnd {
  exit_code: number | null
  timed_out: boolean
}

/** The topics the view's panels read live -- a camera panel's `topic`, or its `source.topic` --
 *  each once, in panel order. What a tap follows by default: the same streams the reader is
 *  already watching, so the tail explains the panels rather than something else. */
export function panelTopics(specs: PanelSpec[]): string[] {
  const out: string[] = []
  for (const spec of specs) {
    const config = spec.config as { topic?: unknown; source?: { topic?: unknown } }
    for (const topic of [config.topic, config.source?.topic]) {
      if (typeof topic === 'string' && topic && !out.includes(topic)) out.push(topic)
    }
  }
  return out
}

/** The tap's URL for one job, or null while the tap is off or there is no live run to tap. */
export function tapUrl(
  on: boolean,
  campaignId: string,
  jobName: string | null,
  selection: string[],
  seconds = TAP_SECONDS,
): string | null {
  if (!on || !jobName) return null
  return robovast.jobTapStreamUrl(campaignId, jobName, selection, seconds)
}

/** Parse one `line` frame. Throws on anything else, so an unreadable frame is an error rather
 *  than a tail that silently skips lines. */
export function parseTapLine(data: string): TapLine {
  const parsed: unknown = JSON.parse(data)
  if (!parsed || typeof parsed !== 'object' || typeof (parsed as TapLine).line !== 'string')
    throw new Error('tap frame is not {t_wall, line}')
  return { t_wall: Number((parsed as TapLine).t_wall), line: (parsed as TapLine).line }
}

/** Parse the `eof` frame; an empty body is an end with nothing to say. */
export function parseTapEnd(data: string): TapEnd {
  const parsed = (data ? JSON.parse(data) : {}) as Partial<TapEnd>
  return {
    exit_code: typeof parsed.exit_code === 'number' ? parsed.exit_code : null,
    timed_out: !!parsed.timed_out,
  }
}

/** The lines held so far, and how many older ones were dropped to stay within the bound. */
export interface TapTail {
  lines: TapLine[]
  dropped: number
}

export const NO_TAP_LINES: TapTail = { lines: [], dropped: 0 }

/** Append lines, keeping the newest `max`. */
export function appendTapLines(prev: TapTail, add: TapLine[], max = MAX_TAP_LINES): TapTail {
  if (!add.length) return prev
  const all = prev.lines.concat(add)
  const over = all.length - max
  if (over <= 0) return { lines: all, dropped: prev.dropped }
  return { lines: all.slice(over), dropped: prev.dropped + over }
}

/** The footer line: what the reader must know to read the tail right. */
export function tapNote(o: {
  selection: string[]
  dropped: number
  end: TapEnd | null
  state: LiveState
  error?: Error
}): string {
  const parts: string[] = []
  parts.push(
    o.selection.length
      ? `Following ${o.selection.join(', ')}.`
      : 'No panel names a topic: listing what the run publishes.',
  )
  if (o.dropped > 0) parts.push(`Showing the newest ${MAX_TAP_LINES} lines; ${o.dropped} dropped.`)
  if (o.error) parts.push(o.error.message)
  else if (o.end)
    parts.push(
      o.end.timed_out
        ? `The ${TAP_SECONDS}s bound was reached; the tap reopens.`
        : o.end.exit_code === null
          ? 'The tap was closed.'
          : `The command ended with exit code ${o.end.exit_code}.`,
    )
  else if (o.state === 'reconnecting' || o.state === 'closed') parts.push('Reconnecting…')
  return parts.join(' ')
}

/** Follow one job's tap while `on`. Each bound reached reopens the stream, so "on" means the tail
 *  keeps moving until the reader turns it off or the tap is refused. */
export function useTap(on: boolean, campaignId: string, jobName: string | null, selection: string[]) {
  const [tail, setTail] = useState<TapTail>(NO_TAP_LINES)
  const [end, setEnd] = useState<TapEnd | null>(null)
  const [error, setError] = useState<Error | undefined>(undefined)
  // Bumped when a bound is reached, so the same URL opens again as a new stream.
  const [round, setRound] = useState(0)

  const key = selection.join(',')
  const url = tapUrl(on, campaignId, jobName, selection)
  const resetKey = `${campaignId}/${jobName ?? ''}/${key}/${round}`

  const { state, received, finish, generation } = useLiveStream(url, {
    resetKey,
    events: {
      line: (e) => {
        try {
          const line = parseTapLine(String(e.data))
          setTail((prev) => appendTapLines(prev, [line]))
        } catch (err) {
          setError(new Error(`unreadable tap frame: ${(err as Error).message}`))
          finish()
        }
      },
      // The service refused the tap (not running, no tap for this simulator, one already open):
      // said in the tail, and the toggle stays on so the reader sees why rather than a blank.
      streamerror: (e) => {
        let msg = 'tap stream error'
        try {
          msg = String(JSON.parse(e.data))
        } catch {
          /* keep the generic message */
        }
        setError(new Error(msg))
        finish()
      },
      eof: (e) => {
        const ended = parseTapEnd(String(e.data))
        setEnd(ended)
        finish()
        // The bound ended a tap the reader still wants: open the next one. A command that ended
        // on its own (the topic list, an exit) is left as it ended.
        if (ended.timed_out) setRound((n) => n + 1)
      },
    },
  })

  // A new stream starts from nothing the service holds, so the tail starts over with it; a
  // reopened round keeps the lines, since the reader is still watching the same thing.
  useEffect(() => {
    setEnd(null)
    setError(undefined)
  }, [generation, resetKey])
  useEffect(() => {
    setTail(NO_TAP_LINES)
  }, [campaignId, jobName, key, on])

  return useMemo(
    () => ({
      tail,
      end,
      error,
      isPending: !!url && !received && !tail.lines.length && !end && !error,
      note: tapNote({ selection, dropped: tail.dropped, end, state, error }),
    }),
    [tail, end, error, url, received, selection, state],
  )
}
