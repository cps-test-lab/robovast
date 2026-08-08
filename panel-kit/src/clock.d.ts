export interface ClockSnapshot {
    t: number;
    playing: boolean;
    speed: number;
    lo: number;
    hi: number;
}
export declare class PlaybackClock {
    private _t;
    private _playing;
    private _speed;
    private _lo;
    private _hi;
    private listeners;
    private raf;
    private lastWall;
    private snap;
    subscribe: (fn: () => void) => (() => void);
    getSnapshot: () => ClockSnapshot;
    private emit;
    get t(): number;
    /** Set the timeline bounds; clamps the cursor into the new range and rewinds to the start. */
    setRange(lo: number, hi: number): void;
    seek(t: number): void;
    /** Seek to a fraction (0..1) of the range — used by the progress-bar click. */
    seekFraction(f: number): void;
    setSpeed(speed: number): void;
    play(): void;
    pause(): void;
    togglePlay(): void;
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
