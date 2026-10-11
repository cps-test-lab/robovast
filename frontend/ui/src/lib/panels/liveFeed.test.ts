// The live subscription's protocol, against a fake EventSource: one socket per run carrying the
// union of the tables asked for, a wider socket when a new table is named, `gap` for the readers a
// reopen may have cost rows, `eof` once and to late subscribers too.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ANY_TABLE, LiveRunFeed, parseBatch, type EventSourceLike, type FeedEvent } from './liveFeed'

class FakeSource implements EventSourceLike {
  readyState = 0
  onopen: ((ev: Event) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  closed = false
  private handlers = new Map<string, ((ev: MessageEvent) => void)[]>()

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (ev: MessageEvent) => void) {
    this.handlers.set(type, [...(this.handlers.get(type) ?? []), listener])
  }

  close() {
    this.closed = true
    this.readyState = 2
  }

  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  send(type: string, data: string) {
    for (const h of this.handlers.get(type) ?? []) h({ data } as MessageEvent)
  }
}

let sockets: FakeSource[]
let feed: LiveRunFeed

beforeEach(() => {
  vi.useFakeTimers()
  sockets = []
  feed = new LiveRunFeed(
    (tables) => `/live?tables=${tables.join(',')}`,
    (url) => {
      const s = new FakeSource(url)
      sockets.push(s)
      return s
    },
  )
})

afterEach(() => {
  feed.close()
  vi.useRealTimers()
})

const heard = (table: string) => {
  const events: FeedEvent[] = []
  feed.subscribe(table, (e) => events.push(e))
  return events
}

describe('parseBatch', () => {
  it('reads a batch frame and refuses anything else', () => {
    expect(parseBatch('{"table":"sim_poses","rows":[{"a":1}]}')).toEqual({
      table: 'sim_poses', rows: [{ a: 1 }],
    })
    expect(() => parseBatch('{"rows":[]}')).toThrow(/expected/)
    expect(() => parseBatch('[1,2]')).toThrow(/expected/)
  })
})

describe('LiveRunFeed', () => {
  it('opens one socket for the tables asked for, and widens it for a new one', () => {
    heard('sim_poses')
    expect(sockets.map((s) => s.url)).toEqual(['/live?tables=sim_poses'])
    heard('sim_poses') // the same table again: nothing reopens
    heard('joint_states')
    expect(sockets.map((s) => s.url)).toEqual([
      '/live?tables=sim_poses', '/live?tables=joint_states,sim_poses',
    ])
    expect(sockets[0].closed).toBe(true)
  })

  it('routes a batch to its table, and to the wildcard', () => {
    const poses = heard('sim_poses')
    const joints = heard('joint_states')
    const any = heard(ANY_TABLE)
    sockets[1].open()
    sockets[1].send('batch', '{"table":"sim_poses","rows":[{"frame":"a"}]}')
    expect(poses).toEqual([{ kind: 'batch', table: 'sim_poses', rows: [{ frame: 'a' }] }])
    expect(joints).toEqual([])
    expect(any).toHaveLength(1)
  })

  it('tells the readers already there about a gap when the socket is reopened', () => {
    const poses = heard('sim_poses')
    sockets[0].open()
    expect(poses).toEqual([]) // the first open of the first socket is not a gap
    heard('joint_states')
    sockets[1].open()
    expect(poses).toEqual([{ kind: 'gap' }])
    // The browser's own retry: the same socket opens again.
    sockets[1].open()
    expect(poses).toEqual([{ kind: 'gap' }, { kind: 'gap' }])
  })

  it('ends with eof, closes the socket, and tells a late subscriber at once', async () => {
    const poses = heard('sim_poses')
    sockets[0].open()
    sockets[0].send('eof', '')
    expect(poses).toEqual([{ kind: 'eof' }])
    expect(sockets[0].closed).toBe(true)
    expect(feed.done).toBe(true)
    const late = heard('joint_states')
    await Promise.resolve()
    expect(late).toEqual([{ kind: 'eof' }])
    expect(sockets).toHaveLength(1) // no socket for a run that is over
  })

  it('reports a stream error by its message', () => {
    const poses = heard('sim_poses')
    sockets[0].open()
    sockets[0].send('streamerror', 'no such run')
    expect(poses).toEqual([{ kind: 'error', message: 'no such run' }])
  })

  it('replaces a socket that has gone silent', () => {
    const poses = heard('sim_poses')
    sockets[0].open()
    // Two watchdog ticks: the first finds the socket exactly a margin old, the second past it.
    vi.advanceTimersByTime(31_000)
    expect(sockets).toHaveLength(2)
    expect(sockets[0].closed).toBe(true)
    sockets[1].open()
    expect(poses).toEqual([{ kind: 'gap' }])
  })

  it('keeps a socket that heartbeats', () => {
    heard('sim_poses')
    sockets[0].open()
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(5_000)
      sockets[0].send('heartbeat', '')
    }
    expect(sockets).toHaveLength(1)
  })
})
