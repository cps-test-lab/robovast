export interface ClockSnapshot {
    t: number;
    playing: boolean;
    speed: number;
    lo: number;
    hi: number;
    /** When the run's scenario reached a verdict, or null if it recorded none. */
    verdict: number | null;
    /** Whether `hi` is that verdict rather than the end of the recording. */
    hideShutdown: boolean;
    /** Whether the cursor is tracking a growing `hi` (see `follow`). */
    following: boolean;
}
/** How far behind the newest sample a following clock sits, in seconds, unless `follow` says. */
export declare const DEFAULT_FOLLOW_BUFFER_S = 2.5;
export declare class PlaybackClock {
    private _t;
    private _playing;
    private _speed;
    private _lo;
    private _hiFull;
    private _verdict;
    private _hideShutdown;
    private _following;
    private _bufferS;
    private listeners;
    private raf;
    private lastWall;
    private snap;
    subscribe: (fn: () => void) => (() => void);
    getSnapshot: () => ClockSnapshot;
    private get _hi();
    private emit;
    get t(): number;
    /** Set the timeline bounds (the end of the **recording**). The cursor is clamped into the new
     *  range and otherwise left where it is -- a growing range never rewinds a reader. Where the
     *  trial ended is `setVerdict`. */
    setRange(lo: number, hi: number): void;
    /** When this run's scenario reached a verdict; `null` if it recorded none. */
    setVerdict(t: number | null): void;
    /** End the timeline at the verdict (the default) or run it to the end of the recording. */
    setHideShutdown(on: boolean): void;
    /** Move the cursor. Leaves follow mode. */
    seek(t: number): void;
    /** Seek to a fraction (0..1) of the range — used by the progress-bar click. */
    seekFraction(f: number): void;
    setSpeed(speed: number): void;
    play(): void;
    /** Pausing a following clock ends the following too. */
    pause(): void;
    togglePlay(): void;
    /** Track the end of a range that is still growing: the cursor sits `bufferS` behind `hi` and
     *  advances at real time as rows land, easing rather than jumping at the edge. Calling it again
     *  returns to the edge; seeking, scrubbing or pausing leaves the mode. */
    follow(bufferS?: number): void;
    /** The run has finished: following ends at `hi`, and the clock stops there. */
    finish(): void;
    private edge;
    private start;
    dispose(): void;
    private tick;
}
/** Subscribe a component to the clock; re-renders on any change. */
export declare function useClock(clock: PlaybackClock): ClockSnapshot;
/** The read-only view of the clock a panel needs: the current time, and a change subscription.
 *
 *  Panels take this rather than the class so a source that is not a `PlaybackClock` (a live view driven
 *  from `/clock`) satisfies the same contract. */
export interface ClockSource {
    readonly t: number;
    subscribe(fn: () => void): () => void;
}
