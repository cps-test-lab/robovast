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
  hi: number // range end
}

export class PlaybackClock {
  private _t = 0
  private _playing = false
  private _speed = 1
  private _lo = 0
  private _hi = 0
  private listeners = new Set<() => void>()
  private raf: number | null = null
  private lastWall = 0
  private snap: ClockSnapshot = { t: 0, playing: false, speed: 1, lo: 0, hi: 0 }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  getSnapshot = (): ClockSnapshot => this.snap

  private emit() {
    // Rebuild the cached snapshot exactly once per change so useSyncExternalStore sees a stable ref.
    this.snap = { t: this._t, playing: this._playing, speed: this._speed, lo: this._lo, hi: this._hi }
    for (const l of this.listeners) l()
  }

  get t() {
    return this._t
  }

  /** Set the timeline bounds; clamps the cursor into the new range and rewinds to the start. */
  setRange(lo: number, hi: number) {
    this._lo = lo
    this._hi = Math.max(hi, lo)
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
