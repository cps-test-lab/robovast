import { describe, expect, it } from 'vitest'
import { logFooter, trimHead } from './LogPanel'

// The state this exists for: the stream is open with nothing in it, and that means two
// opposite things. The server flushes the response headers before its first pull, so a log
// that is merely slow to read (a cluster log is a storage read over a tunnel — tens of
// seconds) looks exactly like one that has genuinely produced nothing. Deciding from the
// socket alone made every such wait render as an authoritative "(no output yet)".
const base = { end: null, errorMsg: null, state: 'open', received: false, empty: true } as const

describe('logFooter', () => {
  it('is busy on an open stream the server has not spoken on yet', () => {
    expect(logFooter(base)).toEqual({ text: 'loading…', kind: 'busy' })
  })

  it('is busy before the socket is even up', () => {
    expect(logFooter({ ...base, state: 'connecting' })).toEqual({ text: 'loading…', kind: 'busy' })
  })

  // A heartbeat is the server saying "I read it, there was nothing" — the only thing that
  // turns the empty body into a fact about the log.
  it('calls the log empty once a frame has arrived', () => {
    expect(logFooter({ ...base, received: true })).toEqual({
      text: '(no output yet)',
      kind: 'note',
    })
  })

  it('says nothing under a log that has content', () => {
    expect(logFooter({ ...base, received: true, empty: false })).toBeNull()
  })

  // Terminal: nothing will ever be written, so this is not a wait and must not spin.
  it('reports a finished empty log as having none', () => {
    expect(logFooter({ ...base, end: 'eof' })).toEqual({ text: '(no log)', kind: 'note' })
  })

  it('keeps the error verbatim, over every other state', () => {
    expect(logFooter({ ...base, end: 'error', errorMsg: 'pod gone', state: 'closed' })).toEqual({
      text: 'stream error: pod gone',
      kind: 'error',
    })
  })

  it('names an error even when the server sent no message with it', () => {
    expect(logFooter({ ...base, end: 'error' })).toEqual({
      text: 'stream error: unknown',
      kind: 'error',
    })
  })

  // A dropped transport, not an empty log: it spins, and it does so with text already on
  // screen — which is why the reconnect branch is tested with `empty: false` too.
  it.each(['reconnecting', 'closed'] as const)('reports a %s transport', (state) => {
    expect(logFooter({ ...base, state, received: true })).toEqual({
      text: 'reconnecting…',
      kind: 'busy',
    })
    expect(logFooter({ ...base, state, received: true, empty: false })).toEqual({
      text: 'reconnecting…',
      kind: 'busy',
    })
  })

  // The stream is *deliberately* closed after `eof`; treating that as a broken transport
  // would put a spinner under every finished log.
  it('does not call a deliberately closed stream a reconnect', () => {
    expect(logFooter({ ...base, end: 'eof', state: 'closed', empty: false })).toBeNull()
  })
})

// --- trimHead ---------------------------------------------------------------------

// A campaign's infrastructure log runs to tens of megabytes, and the body is rebuilt from
// the whole buffer on every delta — so what an unbounded buffer costs is not memory but a
// full re-render of hundreds of thousands of spans, twice a second, for a 320px window.
const KEEP_CHARS = 512 * 1024
const line = (i: number) => `line ${i} ${'x'.repeat(80)}`
const huge = Array.from({ length: 20000 }, (_, i) => line(i)).join('\n')

describe('trimHead', () => {
  it('leaves a log that fits exactly as it is', () => {
    const small = 'a\nb\nc'
    expect(trimHead(small)).toBe(small)
  })

  it('keeps the end and drops the beginning', () => {
    const kept = trimHead(huge)
    expect(kept.length).toBeLessThanOrEqual(KEEP_CHARS + 64)
    expect(kept.endsWith(line(19999))).toBe(true)
    expect(kept).not.toContain(line(0))
  })

  it('says that it dropped something', () => {
    expect(trimHead(huge)).toContain('earlier output not shown')
  })

  it('resumes on a whole line, never mid-word', () => {
    const body = trimHead(huge).split('\n').slice(1)
    expect(body[0]).toMatch(/^line \d+ x+$/)
  })

  // Trimming happens on every append once the cap is reached, so a marker that survived
  // its own trim would leave the panel showing a growing stack of notices.
  it('carries exactly one marker however often it runs', () => {
    let text = huge
    for (let i = 0; i < 5; i++) text = trimHead(`${text}\n${line(90000 + i)}`)
    expect(text.split('earlier output not shown')).toHaveLength(2)
    expect(text.startsWith('[…')).toBe(true)
  })
})
