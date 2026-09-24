// The live job log's pure half: addressing a run's job, shaping streamed rows for RunLogView,
// and bounding what accumulates.
//
// `jobNameOf` is tested against rows in the results tree's own shape because nothing type-checks
// the agreement between the two ends: the service's job-log endpoint takes a `job_name` of
// `<config>/<run>`, and a drift on either side 404s at runtime with nothing pointing at why.

import { describe, expect, it } from 'vitest'
import type { JobLogRow } from '@/lib/robovastClient'
import {
  appendRows,
  jobNameOf,
  liveRunLogData,
  NO_ROWS,
  parseFrame,
  toLogRow,
} from './useJobLogStream'

describe('jobNameOf', () => {
  it('addresses a job the way the service names one', () => {
    expect(jobNameOf('nominal', 3)).toBe('nominal/3')
  })

  it('names the job of every run row the tree offers', () => {
    // `run_view` rows, as `CAMPAIGN_RUNS_SQL` selects them: config and run id by name.
    const rows = [
      { config_name: 'fast', run_id: 0 }, { config_name: 'fast', run_id: 2 },
      { config_name: 'slow', run_id: 1 },
    ]
    expect(rows.map((r) => jobNameOf(String(r.config_name), Number(r.run_id))))
      .toEqual(['fast/0', 'fast/2', 'slow/1'])
  })
})

/** A row as the service sends one with nothing set: every field at its interface default. */
const jobRow = (over: Partial<JobLogRow> = {}): JobLogRow => ({
  wall_ts: null,
  time_source: 'none',
  container: '',
  node: '',
  level: '',
  severity: '',
  message: '',
  ...over,
})

describe('toLogRow', () => {
  it('keeps the stamp and leaves sim time to postprocessing', () => {
    expect(
      toLogRow(jobRow({
        wall_ts: 1700000000.5,
        time_source: 'stamp',
        container: 'robovast',
        node: 'nav',
        level: 'WARN',
        severity: 'warn',
        message: 'a\nb',
      })),
    ).toEqual({
      sim_time: null,
      wall_ts: 1700000000.5,
      time_source: 'stamp',
      in_window: 1,
      container: 'robovast',
      node: 'nav',
      source: 'stdout',
      level: 'WARN',
      severity: 'warn',
      message: 'a\nb',
    })
  })

  it('reads an unstamped, unclassified row as such', () => {
    const row = toLogRow(jobRow())
    expect(row.wall_ts).toBeNull()
    expect(row.time_source).toBe('none')
    expect(row.severity).toBe('other')
  })
})

describe('parseFrame', () => {
  it('maps a row array', () => {
    expect(parseFrame('[{"message":"hi"}]').map((r) => r.message)).toEqual(['hi'])
  })

  it('refuses a frame that is not a row array', () => {
    expect(() => parseFrame('"text"')).toThrow(/not a row array/)
  })
})

describe('appendRows', () => {
  const rows = (...ms: string[]) => ms.map((message) => toLogRow(jobRow({ message })))

  it('appends in arrival order', () => {
    const a = appendRows(NO_ROWS, rows('1', '2'))
    const b = appendRows(a, rows('3'))
    expect(b.rows.map((r) => r.message)).toEqual(['1', '2', '3'])
    expect(b.dropped).toBe(0)
  })

  it('keeps the newest rows and counts what it dropped', () => {
    const a = appendRows(NO_ROWS, rows('1', '2', '3'), 4)
    const b = appendRows(a, rows('4', '5', '6'), 4)
    expect(b.rows.map((r) => r.message)).toEqual(['3', '4', '5', '6'])
    expect(b.dropped).toBe(2)
    expect(appendRows(b, rows('7'), 4).dropped).toBe(3)
  })

  it('returns the same state for an empty frame', () => {
    const a = appendRows(NO_ROWS, rows('1'))
    expect(appendRows(a, [])).toBe(a)
  })
})

describe('liveRunLogData', () => {
  it('is one run with no clock, verdict or table to miss', () => {
    const d = liveRunLogData(appendRows(NO_ROWS, [toLogRow(jobRow({ message: 'x' }))]))
    expect(d).toMatchObject({
      singleRun: true,
      clock: null,
      verdict: null,
      truncated: false,
      missingTable: false,
      total: 1,
      simTimes: [],
    })
  })
})
