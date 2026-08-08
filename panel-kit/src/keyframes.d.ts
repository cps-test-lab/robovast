import type { ClockSource } from './clock';
/** Minimum wall-clock gap between fetch rounds.
 *
 *  A **trailing-edge** throttle, not a leading-edge one: an event arriving inside the gap arms a timer
 *  rather than being dropped. Dropping it is what let a fast scrub leave the last cursor position
 *  unfetched, with the panel then showing an older frame indefinitely. */
export declare const DEFAULT_MIN_INTERVAL_MS = 120;
/** How many local sample periods away from a frame the cursor may sit before that frame stops being an
 *  honest answer.
 *
 *  Inside a regularly-published span the nearest frame is at most *half* a period away, so two periods
 *  flags a genuine publishing gap or an off-the-end clamp without ever flagging ordinary jitter. This
 *  is the one tuned number here: a deliberately bursty publisher (long idle stretches by design) will
 *  be reported stale during them, which is the honest answer but may want revisiting. */
export declare const STALE_PERIODS = 2;
export interface FrameValidity {
    /** Earliest cursor time this frame is still the nearest recorded one for. */
    validFrom: number;
    /** Latest such time. Refetch only outside [validFrom, validTo] -- inside it, a request would return
     *  this same frame, so the interval is exactly the cache key for "would asking again change
     *  anything". */
    validTo: number;
    /** Largest honest |cursor - frame| before the frame should be treated as absent rather than current.
     *  `Infinity` means "cannot go stale" (see the single-frame case below). */
    staleAfter: number;
}
/**
 * Derive both the refetch interval and the staleness threshold from the two timestamps recorded either
 * side of a frame. One function so it stays evident that these are the same two inputs answering two
 * different questions.
 *
 * @param t        the frame's own timestamp.
 * @param tPrev    the previous recorded timestamp for this stream, or null if `t` is the first.
 * @param tNext    the next recorded timestamp, or null if `t` is the newest known.
 * @param spanOpen whether more frames may still arrive after `tNext`/`t`.
 *
 * `spanOpen` exists because `tNext === null` otherwise means two opposite things, and the correct
 * behaviour flips between them. On a **closed** recording it means "there is no later frame, ever", so
 * the frame answers for all later times. On an **open** live stream it means "this is the leading
 * edge", so the frame must be re-requested the moment the cursor moves past it -- treating it as valid
 * forever is precisely the frozen-but-presented-as-current bug this module exists to prevent. A
 * postprocessed run passes `false`; a live provider passes `true`.
 */
export declare function frameValidity(t: number, tPrev: number | null, tNext: number | null, spanOpen: boolean): FrameValidity;
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
    fetchAt: (t: number) => Promise<boolean>;
    /** Defaults to {@link DEFAULT_MIN_INTERVAL_MS}. */
    minIntervalMs?: number;
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
export declare function useKeyframePump(clock: ClockSource, opts: KeyframePumpOptions): void;
