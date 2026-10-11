// The live campaign log's pure half: shaping streamed rows for RunLogView, the level each row
// is coloured by, and the URL the stream is opened on -- which carries the filters, since the
// service applies them as it reads.

import { describe, expect, it } from 'vitest'
import { campaignLogQuery, type CampaignLogRow } from '@/lib/robovastClient'
import { liveNote, parseFrame, severityOfLevel, toLogRow } from './useCampaignLogStream'

/** A row as the service sends one with nothing set: every field at its interface default. */
const row = (over: Partial<CampaignLogRow> = {}): CampaignLogRow => ({
  phase: '',
  seq: 0,
  wall_ts: null,
  level: 'NOTE',
  logger: '',
  message: '',
  ...over,
})

describe('toLogRow', () => {
  it('puts the phase in the container column and the logger in the node column', () => {
    expect(
      toLogRow(row({
        phase: 'RUN',
        seq: 3,
        wall_ts: 1700000000.5,
        level: 'WARNING',
        logger: 'robovast.execution.controller',
        message: 'a\nb',
      })),
    ).toEqual({
      sim_time: null,
      wall_ts: 1700000000.5,
      time_source: 'stamp',
      in_window: 1,
      container: 'RUN',
      node: 'robovast.execution.controller',
      source: 'stdout',
      level: 'WARNING',
      severity: 'warn',
      message: 'a\nb',
    })
  })

  it('reads a NOTE row as unstamped and unclassified', () => {
    const r = toLogRow(row({ phase: 'BUILD', message: '#1 load' }))
    expect(r.wall_ts).toBeNull()
    expect(r.time_source).toBe('none')
    expect(r.severity).toBe('other')
    expect(r.container).toBe('BUILD')
  })
})

describe('severityOfLevel', () => {
  it('maps the levels the service reads, in either spelling', () => {
    expect(['DEBUG', 'INFO', 'NOTE'].map(severityOfLevel)).toEqual(['other', 'other', 'other'])
    expect(['WARN', 'WARNING'].map(severityOfLevel)).toEqual(['warn', 'warn'])
    expect(['ERROR', 'CRITICAL', 'FATAL'].map(severityOfLevel)).toEqual(['error', 'error', 'error'])
  })
})

describe('parseFrame', () => {
  it('maps a row array', () => {
    expect(parseFrame('[{"phase":"RUN","level":"INFO","message":"hi"}]').map((r) => r.message))
      .toEqual(['hi'])
  })

  it('refuses a frame that is not a row array', () => {
    expect(() => parseFrame('"text"')).toThrow(/not a row array/)
  })
})

describe('campaignLogQuery', () => {
  it('is empty for an unfiltered read', () => {
    expect(campaignLogQuery()).toBe('')
    expect(campaignLogQuery('', { phase: '', grep: '' })).toBe('')
  })

  it('carries the cursor and every filter the service applies', () => {
    expect(campaignLogQuery('c-1', { phase: 'run', minLevel: 'ERROR', grep: 'a b' }))
      .toBe('?cursor=c-1&phase=run&min_level=ERROR&grep=a+b')
  })
})

describe('liveNote', () => {
  it('says when the log is complete, and when it is still arriving', () => {
    expect(liveNote({ dropped: 0, eof: true, state: 'closed' })).toMatch(/complete/)
    expect(liveNote({ dropped: 0, eof: false, state: 'open' })).toMatch(/Live/)
    expect(liveNote({ dropped: 0, eof: false, state: 'reconnecting' })).toMatch(/Reconnecting/)
    expect(liveNote({ dropped: 2, eof: false, state: 'open' })).toMatch(/2 earlier lines dropped/)
  })
})
