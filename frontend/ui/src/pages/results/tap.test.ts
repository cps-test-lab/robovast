// The Now tap's pure half: the selection the panels name, the URL the toggle opens, the frames
// and the bounded tail. The toggle's state is the URL: off, or no live run, opens nothing.

import { describe, expect, it } from 'vitest'
import type { PanelSpec } from '@/lib/panels/types'
import {
  appendTapLines,
  NO_TAP_LINES,
  panelTopics,
  parseTapEnd,
  parseTapLine,
  tapNote,
  tapUrl,
  TAP_SECONDS,
} from './tap'

const spec = (type: string, config: Record<string, unknown>): PanelSpec => ({
  type,
  config,
  position: {},
  resizable: true,
  minimizable: true,
  minimized: false,
  frameless: false,
  hidden: false,
  fixed: false,
})

describe('panelTopics', () => {
  it('collects each camera topic once, in panel order', () => {
    const specs = [
      spec('camera', { topic: '/cam/front' }),
      spec('camera', { source: { topic: '/cam/rear' } }),
      spec('camera', { topic: '/cam/front' }),
      spec('time_series', { table: 'poses' }),
    ]
    expect(panelTopics(specs)).toEqual(['/cam/front', '/cam/rear'])
  })

  it('is empty when no panel names a topic', () => {
    expect(panelTopics([spec('scene3d', {})])).toEqual([])
  })
})

describe('tapUrl', () => {
  it('opens nothing while off or without a live run', () => {
    expect(tapUrl(false, 'c', 'cfg/0', ['/a'])).toBeNull()
    expect(tapUrl(true, 'c', null, ['/a'])).toBeNull()
  })

  it('addresses the job and carries the selection and the bound', () => {
    const url = tapUrl(true, 'camp-1', 'cfg a/0', ['/a', '/b'])
    expect(url).toContain('/campaigns/camp-1/job-tap?')
    expect(url).toContain('job_name=cfg%20a%2F0')
    expect(url).toContain('selection=%2Fa%2C%2Fb')
    expect(url).toContain(`max_seconds=${TAP_SECONDS}`)
  })

  it('sends an empty selection, which the service answers with the topic list', () => {
    expect(tapUrl(true, 'c', 'cfg/0', [])).toContain('selection=&')
  })
})

describe('frames', () => {
  it('reads a line frame', () => {
    expect(parseTapLine('{"t_wall": 1.5, "line": "x: 1"}')).toEqual({ t_wall: 1.5, line: 'x: 1' })
  })

  it('refuses a frame that is not a line', () => {
    expect(() => parseTapLine('"text"')).toThrow(/not \{t_wall, line\}/)
  })

  it('reads an end frame, empty included', () => {
    expect(parseTapEnd('{"exit_code": 124, "timed_out": true}')).toEqual({
      exit_code: 124,
      timed_out: true,
    })
    expect(parseTapEnd('{}')).toEqual({ exit_code: null, timed_out: false })
    expect(parseTapEnd('')).toEqual({ exit_code: null, timed_out: false })
  })
})

describe('appendTapLines', () => {
  const lines = (...ls: string[]) => ls.map((line) => ({ t_wall: 0, line }))

  it('keeps the newest lines and counts what it dropped', () => {
    const a = appendTapLines(NO_TAP_LINES, lines('1', '2', '3'), 4)
    const b = appendTapLines(a, lines('4', '5', '6'), 4)
    expect(b.lines.map((l) => l.line)).toEqual(['3', '4', '5', '6'])
    expect(b.dropped).toBe(2)
  })

  it('returns the same state for an empty frame', () => {
    const a = appendTapLines(NO_TAP_LINES, lines('1'))
    expect(appendTapLines(a, [])).toBe(a)
  })
})

describe('tapNote', () => {
  it('names what is followed, and the topic list for nothing', () => {
    expect(tapNote({ selection: ['/a'], dropped: 0, end: null, state: 'open' })).toBe(
      'Following /a.',
    )
    expect(tapNote({ selection: [], dropped: 0, end: null, state: 'open' })).toContain(
      'listing what the run publishes',
    )
  })

  it('says how the tap ended, and that a bound reopens', () => {
    const base = { selection: ['/a'], dropped: 0, state: 'closed' as const }
    expect(tapNote({ ...base, end: { exit_code: 124, timed_out: true } })).toContain('reopens')
    expect(tapNote({ ...base, end: { exit_code: 0, timed_out: false } })).toContain('exit code 0')
    expect(tapNote({ ...base, end: { exit_code: null, timed_out: false } })).toContain('closed')
  })

  it('puts a refusal in the note ahead of the end', () => {
    expect(
      tapNote({
        selection: [],
        dropped: 0,
        end: { exit_code: null, timed_out: false },
        state: 'closed',
        error: new Error('no tap for roqsim'),
      }),
    ).toContain('no tap for roqsim')
  })
})
