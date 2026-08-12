// Keyframes: following the clock when a panel's samples are too big to preload.
//
// This is the counterpart to the host's TimeSeriesSource. That one covers a recording small enough to
// load whole and index in memory (`at(t)` is then a binary search over RAM). Some recordings are not:
// a nav2 occupancy grid is thousands of cells per frame, which is why it is stored re-encoded and
// served one frame at a time. A panel showing those must fetch per clock position, and that is where
// staleness bugs live -- the fetch is asynchronous while the clock is not, so what is drawn can lag
// what the cursor says without anything reporting it.
//
// Two pieces, and the split matters: `frameValidity` decides *whether the frame in hand still answers
// for the cursor* (pure, testable, no I/O), and `useKeyframePump` decides *when to go ask again*
// (stateful, owns the trailing edge). Neither knows what a frame contains.
//
// The planned live view is the second consumer: it drives the same clock from `/clock` over a
// rosbridge buffer, and `spanOpen` below is the one place a recording and a live stream disagree.

import { useEffect, useRef } from 'react'
import type { ClockSource } from './clock'

/** Minimum wall-clock gap between fetch rounds.
 *
 *  A **trailing-edge** throttle, not a leading-edge one: an event arriving inside the gap arms a timer
 *  rather than being dropped. Dropping it is what let a fast scrub leave the last cursor position
 *  unfetched, with the panel then showing an older frame indefinitely. */
export const DEFAULT_MIN_INTERVAL_MS = 120

/** How many sample periods away from a frame the cursor may sit before that frame stops being an
 *  honest answer.
 *
 *  Inside a regularly-published span the nearest frame is at most *half* a period away, so two periods
 *  flags a genuine publishing gap or an off-the-end clamp without ever flagging ordinary jitter. This
 *  is the one tuned number here: a deliberately bursty publisher (long idle stretches by design) will
 *  be reported stale during them, which is the honest answer but may want revisiting. */
export const STALE_PERIODS = 2

export interface FrameValidity {
  /** Earliest cursor time this frame is still the nearest recorded one for. */
  validFrom: number
  /** Latest such time. Refetch only outside [validFrom, validTo] -- inside it, a request would return
   *  this same frame, so the interval is exactly the cache key for "would asking again change
   *  anything". */
  validTo: number
  /** Largest honest |cursor - frame| before the frame should be treated as absent rather than current.
   *  `Infinity` means "cannot go stale" (see the single-frame case below). */
  staleAfter: number
}

export interface FrameContext {
  /** The frame's own timestamp. */
  t: number
  /** The previous recorded timestamp for this stream, or null if `t` is the first. */
  tPrev: number | null
  /** The next recorded timestamp, or null if `t` is the newest known. */
  tNext: number | null
}

export interface ValidityOptions {
  /**
   * Whether more frames may still arrive after `tNext`/`t`.
   *
   * This exists because `tNext === null` otherwise means two opposite things, and the correct
   * behaviour flips between them. On a **closed** recording it means "there is no later frame, ever",
   * so the frame answers for all later times. On an **open** live stream it means "this is the leading
   * edge", so the frame must be re-requested the moment the cursor moves past it — treating it as
   * valid forever is precisely the frozen-but-presented-as-current bug this module exists to prevent.
   * A postprocessed run passes `false`; a live provider passes `true`.
   */
  spanOpen: boolean
  /**
   * The shortest interval, in **seconds**, at which the consumer can actually refresh this frame —
   * normally the pump's `minIntervalMs / 1000`.
   *
   * Without it the threshold is derived from the publish rate alone, which inverts for any topic
   * faster than the fetch cadence. A nav2 local costmap at 50 Hz gives a 40 ms threshold while a round
   * trip plus throttle is ~140 ms, so the frame is judged stale far sooner than it could possibly be
   * replaced and the layer is blanked almost permanently — showing "nearest frame 0.1 s away" for data
   * that is, in fact, as fresh as this panel can ever get.
   *
   * A frame older than the publish period because *we* sampled coarsely is not stale data; it is our
   * own sampling, and reporting it as a gap is a false verdict. What the threshold is for is real
   * gaps — nav2 stopped publishing, or the cursor sits off the end of the span — which are seconds,
   * not tens of milliseconds.
   */
  refreshFloorSec: number
}

/**
 * Derive both the refetch interval and the staleness threshold from the two timestamps recorded either
 * side of a frame. One function so it stays evident that these are the same two inputs answering two
 * different questions.
 */
export function frameValidity(frame: FrameContext, opts: ValidityOptions): FrameValidity {
  const { t, tPrev, tNext } = frame
  const validFrom = tPrev === null ? -Infinity : (tPrev + t) / 2
  const validTo = tNext !== null ? (t + tNext) / 2 : opts.spanOpen ? t : Infinity

  // Local sample period, measured *around this frame* rather than averaged over the stream: a global
  // mean cannot follow a publisher whose rate changes mid-run, and -- more importantly -- cannot flag
  // the frame sitting at the edge of a long gap, which is the case that actually misleads.
  //
  // The SMALLER of the two adjacent gaps, not their mean. At the edge of a gap the two disagree by the
  // width of the gap, and the small side is the one that reports the publisher's real rate — averaging
  // instead lets the gap inflate the estimate until the threshold covers the gap, so the frame is
  // never called stale inside the very gap it borders. That is precisely the case the threshold exists
  // for, and it fails silently: the layer keeps drawing a frame from before a 30 s outage as though
  // it answered for the middle of it.
  const before = tPrev === null ? null : t - tPrev
  const after = tNext === null ? null : tNext - t
  const period =
    before !== null && after !== null ? Math.min(before, after) : (before ?? after ?? null)

  // A single known frame yields no period, so there is no honest threshold to apply -- inventing one
  // would be a fabricated verdict either way. This is also what exempts a latched topic (a static
  // /map publishes once): it is never reported stale because nothing says it should be. On an open
  // stream the panel is not left frozen regardless, because `validTo` above forces a re-request as
  // soon as the cursor passes the frame, and a threshold becomes computable the moment a second frame
  // exists.
  if (period == null || period <= 0) return { validFrom, validTo, staleAfter: Infinity }

  // Never stricter than what we can refresh (see `refreshFloorSec`). Note a fast-forwarding clock can
  // still outrun this, and that is left deliberate: at 4x the cursor really does move further between
  // samples than the data resolves, and saying so is the honest answer.
  const staleAfter = STALE_PERIODS * Math.max(period, opts.refreshFloorSec)

  return { validFrom, validTo, staleAfter }
}

export interface KeyframePumpOptions {
  /**
   * Fetch whatever this panel needs for cursor time `t`, resolving when done.
   *
   * Return **true** if a round actually did work, **false** if everything needed was already valid --
   * the pump uses that to avoid charging the throttle for a no-op and to know when to stop looping.
   *
   * Report your own errors (a panel typically surfaces them in its own error state). A rejection stops
   * the pump rather than retrying, so a persistently failing endpoint cannot turn into a request every
   * `minIntervalMs` forever; the next clock movement starts it again.
   */
  fetchAt: (t: number) => Promise<boolean>
  /** Defaults to {@link DEFAULT_MIN_INTERVAL_MS}. */
  minIntervalMs?: number
}

/**
 * Follow the clock, fetching at most one round at a time and always converging on the latest cursor
 * position.
 *
 * **The invariant that makes this correct: at most one round is ever in flight.** A result therefore
 * cannot be overtaken by an older one, and no generation counter or AbortController is needed. Worth
 * stating explicitly because a caller is expected to fan out *inside* `fetchAt` (the costmap panel
 * fetches its layers with `Promise.all`), which looks like it removed that serialisation but does not.
 *
 * **Every clock event is accounted for**, which is the whole point: it either starts a round, is
 * absorbed by the running round's post-round re-check, or arms a timer for the remainder of the
 * throttle. None is dropped. The earlier implementation dropped any event arriving while a fetch was in
 * flight and never re-checked afterwards, so a scrub ending in that window was never serviced at all.
 */
export function useKeyframePump(clock: ClockSource, opts: KeyframePumpOptions): void {
  const minIntervalMs = opts.minIntervalMs ?? DEFAULT_MIN_INTERVAL_MS
  // Held in a ref so the caller need not memoize its closure; re-subscribing per render would restart
  // the pump and defeat the throttle.
  const fetchAtRef = useRef(opts.fetchAt)
  fetchAtRef.current = opts.fetchAt

  const targetTRef = useRef(clock.t) // latest cursor position seen
  const servicedTRef = useRef<number | null>(null) // cursor position of the last completed round
  const pumpingRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // When the last round STARTED. The throttle is a minimum interval between round *starts*: a round
  // that itself took longer than the interval has already paid it, and must not then idle again.
  // (Measuring from the end instead adds a full interval of dead time to every round, which shows up
  // as exactly the lag this hook exists to remove.)
  const lastRoundStartRef = useRef(0)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true

    const clearTimer = () => {
      if (timerRef.current != null) clearTimeout(timerRef.current)
      timerRef.current = null
    }

    // One timer ref serves both the throttle wait and the between-rounds sleep. They cannot collide:
    // while a round is sleeping, `pumpingRef` is set, and `schedule` returns on that check first.
    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        timerRef.current = setTimeout(() => {
          timerRef.current = null
          resolve()
        }, ms)
      })

    const pump = async () => {
      pumpingRef.current = true
      let failed = false
      try {
        for (;;) {
          if (!aliveRef.current) return
          const t = targetTRef.current
          const startedAt = performance.now()
          const did = await fetchAtRef.current(t)
          servicedTRef.current = t
          if (!aliveRef.current) return
          // Nothing needed fetching, so nothing will until the clock moves again -- and looping here
          // on a moving clock would spin far faster than the display rate for no work.
          if (!did) return
          lastRoundStartRef.current = startedAt
          if (targetTRef.current === t) return // cursor held still: this round settled it
          const wait = minIntervalMs - (performance.now() - startedAt)
          if (wait > 0) await sleep(wait)
        }
      } catch (e) {
        failed = true
        throw e
      } finally {
        pumpingRef.current = false
        // Close the gap between the loop's last target check and this flag clearing. A clock event
        // landing in it saw `pumpingRef` still set, armed nothing, and would be exactly the dropped
        // trailing edge this hook exists to prevent. Skipped after a failure so a broken endpoint does
        // not become a retry storm.
        if (!failed && aliveRef.current && targetTRef.current !== servicedTRef.current) schedule()
      }
    }

    const schedule = () => {
      if (!aliveRef.current) return
      // A running round re-checks the target when it finishes; an armed timer will start one.
      if (pumpingRef.current || timerRef.current != null) return
      const wait = minIntervalMs - (performance.now() - lastRoundStartRef.current)
      if (wait > 0) {
        timerRef.current = setTimeout(() => {
          timerRef.current = null
          void pump()
        }, wait)
        return
      }
      void pump()
    }

    const onClock = () => {
      targetTRef.current = clock.t
      schedule()
    }
    onClock() // fetch for the mount position without waiting for the first clock change
    const unsubscribe = clock.subscribe(onClock)
    return () => {
      aliveRef.current = false
      clearTimer()
      unsubscribe()
    }
  }, [clock, minIntervalMs])
}
