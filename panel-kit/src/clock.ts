// PlaybackClock: the single shared time source for a run-view. One writer (the playback panel);
// every other panel is a reader that subscribes and reads `t`. Time is in seconds on the run's rosbag
// timeline. Kept deliberately transport-agnostic: a future live view can drive the same clock from
// `/clock` without any panel change.
//
// It is an external store (not React state) so the ~display-rate `t` updates while playing don't
// re-render the whole tree -- the canvas costmap subscribes imperatively, and light readers opt in via
// `useClock` (useSyncExternalStore over a cached snapshot).

import { useSyncExternalStore } from 'react'

export interface ClockSnapshot {
  t: number // current time (seconds)
  playing: boolean
  speed: number // playback rate (1 = real time, 2 = fast-forward)
  lo: number // range start
  hi: number // range end -- the *effective* one, see `hideShutdown`
  /** When the run's scenario reached a verdict, or null if it recorded none. */
  verdict: number | null
  /** Whether `hi` is that verdict rather than the end of the recording. */
  hideShutdown: boolean
}

export class PlaybackClock {
  private _t = 0
  private _playing = false
  private _speed = 1
  private _lo = 0
  private _hiFull = 0
  private _verdict: number | null = null
  // On by default: a run's timeline is its *trial*. What follows the verdict is teardown --
  // nodes being killed, lifecycle transitions failing because their peer is already gone --
  // and replaying it stretches the bar past everything worth watching.
  private _hideShutdown = true
  private listeners = new Set<() => void>()
  private raf: number | null = null
  private lastWall = 0
  private snap: ClockSnapshot = {
    t: 0, playing: false, speed: 1, lo: 0, hi: 0, verdict: null, hideShutdown: true,
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  getSnapshot = (): ClockSnapshot => this.snap

  /** The end of the timeline as everything else means it: the verdict while the shutdown
   *  phase is hidden, otherwise the end of the recording. Never past `_hiFull` -- a verdict
   *  logged after the last recorded sample must not invent timeline to play through.
   *
   *  Every clamp below reads this rather than a stored `_hi`, which is what keeps seeking,
   *  scrubbing and playback consistent with the bar without any of them knowing about it. */
  private get _hi(): number {
    if (this._hideShutdown && this._verdict != null)
      return Math.max(this._lo, Math.min(this._verdict, this._hiFull))
    return this._hiFull
  }

  private emit() {
    // Rebuild the cached snapshot exactly once per change so useSyncExternalStore sees a stable ref.
    this.snap = {
      t: this._t, playing: this._playing, speed: this._speed, lo: this._lo, hi: this._hi,
      verdict: this._verdict, hideShutdown: this._hideShutdown,
    }
    for (const l of this.listeners) l()
  }

  get t() {
    return this._t
  }

  /** Set the timeline bounds; clamps the cursor into the new range and rewinds to the start.
   *
   *  `hi` is the end of the **recording**. Where the trial ended is `setVerdict`, kept apart
   *  so that showing the shutdown phase restores the full range rather than re-querying it. */
  setRange(lo: number, hi: number) {
    this._lo = lo
    this._hiFull = Math.max(hi, lo)
    this._t = Math.min(Math.max(this._t, this._lo), this._hi)
    this.emit()
  }

  /** When this run's scenario reached a verdict, in the same seconds as the range.
   *
   *  `null` means the run recorded none -- a campaign postprocessed before the verdict was
   *  recorded, or a run killed by its deadline before reaching one. Nothing is trimmed then,
   *  and the control that offers to says why it cannot. */
  setVerdict(t: number | null) {
    this._verdict = t
    this._t = Math.min(Math.max(this._t, this._lo), this._hi)
    this.emit()
  }

  /** End the timeline at the verdict (the default) or run it to the end of the recording. */
  setHideShutdown(on: boolean) {
    this._hideShutdown = on
    this._t = Math.min(Math.max(this._t, this._lo), this._hi)
    this.emit()
  }

  seek(t: number) {
    this._t = Math.min(Math.max(t, this._lo), this._hi)
    this.emit()
  }

  /** Seek to a fraction (0..1) of the range — used by the progress-bar click. */
  seekFraction(f: number) {
    this.seek(this._lo + Math.min(Math.max(f, 0), 1) * (this._hi - this._lo))
  }

  setSpeed(speed: number) {
    this._speed = speed
    this.emit()
  }

  play() {
    if (this._playing || this._hi <= this._lo) return
    if (this._t >= this._hi) this._t = this._lo // replay from the start
    this._playing = true
    this.lastWall = performance.now()
    this.raf = requestAnimationFrame(this.tick)
    this.emit()
  }

  pause() {
    this._playing = false
    if (this.raf != null) cancelAnimationFrame(this.raf)
    this.raf = null
    this.emit()
  }

  togglePlay() {
    this._playing ? this.pause() : this.play()
  }

  dispose() {
    if (this.raf != null) cancelAnimationFrame(this.raf)
    this.raf = null
    this.listeners.clear()
  }

  private tick = () => {
    const now = performance.now()
    const dt = (now - this.lastWall) / 1000
    this.lastWall = now
    this._t += dt * this._speed
    if (this._t >= this._hi) {
      this._t = this._hi
      this.emit()
      this.pause()
      return
    }
    this.emit()
    this.raf = requestAnimationFrame(this.tick)
  }
}

/** Subscribe a component to the clock; re-renders on any change. */
export function useClock(clock: PlaybackClock): ClockSnapshot {
  return useSyncExternalStore(clock.subscribe, clock.getSnapshot)
}

/** The read-only view of the clock a panel needs: the current time, and a change subscription.
 *
 *  Panels take this rather than the class so a source that is not a `PlaybackClock` (a live view driven
 *  from `/clock`) satisfies the same contract. */
export interface ClockSource {
  readonly t: number
  subscribe(fn: () => void): () => void
}
