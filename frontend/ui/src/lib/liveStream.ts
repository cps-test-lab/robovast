import { useCallback, useEffect, useRef, useState } from 'react'
import { useActiveView } from '@/lib/activeView'

// A self-healing EventSource.
//
// The browser's own EventSource covers the easy failure — a connection that drops while
// you are watching — and nothing else. Two gaps are left, and both show up the same way:
// you switch back to the tab and the page is quietly out of date, with no hint that it is.
//
//  - **It gives up.** After enough consecutive failures the browser stops retrying and
//    parks the stream in `readyState === CLOSED`, from which it will never reopen itself.
//    Reading `readyState !== CLOSED` as "still trying" skips the "reconnecting" state in
//    exactly the closed case — backwards: the one case that needs help would be the one
//    case nothing is done about.
//
//  - **It cannot tell a quiet stream from a dead one.** Suspend the laptop, or tear down
//    the `kubectl port-forward` the service is reached through, and the socket becomes a
//    zombie: no error fires, `readyState` stays OPEN, and not one further byte will ever
//    arrive. Nothing observable separates that from a campaign that simply has not changed
//    in a while — unless the server keeps saying so. It does: every quiet tick of these
//    streams is a `heartbeat` event. It has to be an *event* rather than the SSE comment such
//    keepalives usually are, because comments are invisible to the client — they keep proxies
//    happy and tell the browser nothing.
//
// So every frame stamps a clock, and coming back to the stream checks that clock — coming back
// to the tab, and equally switching to the page the stream feeds, which is the same event one
// level in (see lib/activeView.tsx). A stream that is closed, or that has gone silent for longer
// than the server could plausibly be quiet, is thrown away and replaced. The Refresh button does
// the same thing on demand; it just is not the only way to get there.

/** Stream health, as far as the client can tell. */
export type LiveState = 'connecting' | 'open' | 'reconnecting' | 'closed'

/**
 * Silence that means the stream is dead rather than idle.
 *
 * The servers heartbeat every second, so this is a wide margin — a background tab whose
 * timers are throttled, or a service under load, must not be mistaken for a broken one. It
 * doubles as the retry cadence once a stream *is* broken, which is why it is not tighter:
 * a reconnect loop against a service that is down should stay cheap.
 */
const STALE_MS = 15_000

export interface LiveStreamOptions {
  /** Default (unnamed) `message` frames — the payload of most streams. */
  onMessage?: (e: MessageEvent) => void
  /**
   * Named SSE events besides `message`. The *names* are read once, when the stream opens,
   * so keep them constant; the handlers themselves are re-read on every event, so they may
   * close over fresh state.
   */
  events?: Record<string, (e: MessageEvent) => void>
  /** Tear down and rebuild the stream whenever this changes (a new log, a new campaign). */
  resetKey?: string | number
}

/**
 * Subscribe to an SSE endpoint, and keep the subscription honest.
 *
 * Returns the stream's state, a `received` flag, a `reconnect()` for a user-driven refresh,
 * a `finish()` for the case where the *server* has said there is nothing more coming (an
 * `eof` frame on a terminal log), and a `generation` that counts connections this hook
 * opened itself.
 *
 * `received` separates "the socket is up" from "the server has said something", which
 * `state === 'open'` cannot: these servers flush the response headers immediately (an SSE
 * comment, so proxies release them) and only then run their first pull — and those pulls
 * are network I/O, up to tens of seconds for a log read across a cluster. So an open stream
 * with nothing in it means one of two opposite things, and only the arrival of a frame — a
 * delta or a `heartbeat`, which is the server saying "read it, there was nothing" — tells
 * them apart. A consumer that renders an empty state needs that: without it, every slow
 * first pull renders as an authoritative "nothing here". It stays true across the browser's
 * own reconnect, where the accumulated state survives too.
 *
 * `finish()` matters: without it the watchdog would read a deliberately closed stream as a
 * broken one and reopen it forever.
 *
 * `generation` matters to any consumer that *accumulates* frames rather than replacing
 * them. The browser's own reconnect replays `Last-Event-ID` and the server resumes from
 * exactly where it left off, so the accumulated text must survive it — but a socket this
 * hook opened carries no such header and the server starts from the top, so the same text
 * must be dropped first. The two are indistinguishable from `onopen` alone; a bumped
 * `generation` is what separates them.
 */
export function useLiveStream(url: string, opts: LiveStreamOptions = {}) {
  const { resetKey = '' } = opts
  // Handlers are re-read at dispatch time so a re-render's fresh closures are the ones that
  // run, without the subscription itself churning on every render.
  const optsRef = useRef(opts)
  optsRef.current = opts

  const [state, setState] = useState<LiveState>('connecting')
  const [epoch, setEpoch] = useState(0)
  // Whether the *server* has spoken on this connection, as opposed to the socket being up.
  // See the `received` note on the return value.
  const [received, setReceived] = useState(false)
  const esRef = useRef<EventSource | null>(null)
  const lastFrame = useRef(0)
  const finished = useRef(false)

  const reconnect = useCallback(() => {
    finished.current = false
    setEpoch((n) => n + 1)
  }, [])

  const finish = useCallback(() => {
    finished.current = true
    esRef.current?.close()
  }, [])

  useEffect(() => {
    finished.current = false
    setState('connecting')
    setReceived(false)
    const es = new EventSource(url)
    esRef.current = es
    lastFrame.current = Date.now()

    const stamp = () => {
      lastFrame.current = Date.now()
    }
    // A frame is a stamp plus the news that the server has produced something. `onopen` is
    // deliberately not one: the headers are flushed before the server's first pull runs.
    const frame = () => {
      stamp()
      setReceived(true)
    }
    es.onopen = () => {
      stamp()
      setState('open')
    }
    es.onmessage = (e) => {
      frame()
      setState('open')
      optsRef.current.onMessage?.(e)
    }
    // Quiet tick. Nothing to render — its whole job is to prove the socket still carries
    // bytes, which is what the watchdog below reads.
    es.addEventListener('heartbeat', frame)
    for (const name of Object.keys(optsRef.current.events ?? {})) {
      es.addEventListener(name, (e) => {
        frame()
        optsRef.current.events?.[name]?.(e as MessageEvent)
      })
    }
    es.onerror = () => {
      // CLOSED is the terminal one: the browser has stopped retrying and this stream is
      // over unless something reopens it. Anything else is a blip it is already handling.
      setState(es.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting')
    }

    return () => {
      es.close()
      esRef.current = null
    }
  }, [url, resetKey, epoch])

  const check = useCallback(() => {
    if (finished.current || document.visibilityState !== 'visible') return
    const es = esRef.current
    const silent = Date.now() - lastFrame.current > STALE_MS
    if (!es || es.readyState === EventSource.CLOSED || silent) reconnect()
  }, [reconnect])

  // Coming back to the tab is the moment staleness becomes visible, so it is the moment to
  // check — plus a timer, so a stream that dies while being watched heals too, and `online`,
  // for the laptop that just found a network again.
  useEffect(() => {
    document.addEventListener('visibilitychange', check)
    window.addEventListener('online', check)
    const timer = window.setInterval(check, STALE_MS)
    return () => {
      document.removeEventListener('visibilitychange', check)
      window.removeEventListener('online', check)
      window.clearInterval(timer)
    }
  }, [check])

  // And switching to the page a stream feeds, which is the same moment for a view that is kept
  // mounted while hidden: the tab never changed visibility, so none of the listeners above fire,
  // yet a stream that zombied meanwhile is exactly as stale — and the campaign list is the first
  // thing read on arriving at the monitor. Without this it goes on saying nothing, unlabelled,
  // until the watchdog's next tick; `reconnecting…` only appears once the check has run.
  //
  // Cheap by construction: the servers heartbeat every second and the socket keeps delivering
  // while the page is hidden, so a healthy stream has a fresh clock here and nothing is rebuilt.
  const active = useActiveView()
  useEffect(() => {
    if (active) check()
  }, [active, check])

  return { state, received, reconnect, finish, generation: epoch }
}