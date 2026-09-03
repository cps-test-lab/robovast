import { describe, expect, it } from 'vitest'
import { logFooter } from './LogPanel'

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
