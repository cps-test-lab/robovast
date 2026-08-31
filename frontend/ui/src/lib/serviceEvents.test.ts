import { describe, expect, it } from 'vitest'

import { eventTone, hasMore, newestFirst } from './serviceEvents'
import type { ServiceEvent } from './robovastClient'

const ev = (seq: number): ServiceEvent =>
  ({ seq, at: 0, kind: 'request.refused', severity: 'error', actor: '', subject_type: '',
     subject_id: '', message: '', payload: {} })

describe('newestFirst', () => {
  it('reverses the cursor order the route serves', () => {
    // The route is oldest-first because a caller resuming a position asks for what came after
    // its seq. A person opening the panel wants what just happened.
    expect(newestFirst([ev(1), ev(2), ev(3)]).map((e) => e.seq)).toEqual([3, 2, 1])
  })

  it('does not mutate what it was handed', () => {
    const given = [ev(1), ev(2)]
    newestFirst(given)
    expect(given.map((e) => e.seq)).toEqual([1, 2])
  })
})

describe('hasMore', () => {
  it('offers more only when the page came back full', () => {
    // A short page IS the whole record; offering more there promises what the next request
    // cannot deliver.
    expect(hasMore(50, 50)).toBe(true)
    expect(hasMore(12, 50)).toBe(false)
    expect(hasMore(0, 50)).toBe(false)
  })
})

describe('eventTone', () => {
  it('maps the severities the service sends', () => {
    expect(eventTone('error')).toBe('error')
    expect(eventTone('warning')).toBe('warning')
    expect(eventTone('success')).toBe('success')
  })

  it('falls back rather than throwing on a severity this build predates', () => {
    // `kind` is an open vocabulary and severity may widen with it; a panel that threw on an
    // unknown one would black out on the very event somebody upgraded to see.
    expect(eventTone('catastrophe')).toBe('info')
  })
})
