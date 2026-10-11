// Follow mode, and the one property of `setRange` every live panel leans on: a growing range never
// moves the cursor of its own accord. Driven by a hand-cranked animation frame and clock, so each
// case says how much time passed and where the cursor must be after it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PlaybackClock } from './clock'

let frame: FrameRequestCallback | null = null
let now = 0

/** Advance the wall clock by `ms` and run the frame the clock scheduled, if any. */
const step = (ms: number) => {
  now += ms
  const f = frame
  frame = null
  f?.(now)
}

beforeEach(() => {
  now = 0
  frame = null
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    frame = cb
    return 1
  })
  vi.stubGlobal('cancelAnimationFrame', () => {
    frame = null
  })
  vi.spyOn(performance, 'now').mockImplementation(() => now)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('setRange', () => {
  it('clamps the cursor into the range and does not rewind it', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.seek(7)
    clock.setRange(0, 20)
    expect(clock.t).toBe(7)
    clock.setRange(0, 5)
    expect(clock.t).toBe(5)
  })
})

describe('follow', () => {
  it('starts one buffer behind the edge and advances at real time as the edge moves', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.follow(2.5)
    expect(clock.getSnapshot()).toMatchObject({ following: true, playing: true, t: 7.5 })
    // The recording grows in step with the wall clock: the cursor stays a buffer behind it.
    for (let i = 1; i <= 4; i++) {
      step(1000)
      clock.setRange(0, 10 + i)
    }
    expect(clock.t).toBeCloseTo(11.5, 5)
  })

  it('eases towards the end rather than running past it when no rows arrive', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.follow(2.5)
    // Ten seconds with no new rows: the cursor slows as it nears `hi` and never reaches it.
    const seen: number[] = []
    for (let i = 0; i < 10; i++) {
      step(1000)
      seen.push(clock.t)
    }
    expect(seen.every((t, i) => i === 0 || t >= seen[i - 1])).toBe(true)
    expect(clock.t).toBeLessThan(10)
    expect(clock.t).toBeGreaterThan(9)
    // The last second moved it less than the first did.
    expect(seen[9] - seen[8]).toBeLessThan(seen[1] - seen[0])
  })

  it('catches up at a bounded rate when the edge jumps ahead', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.follow(2.5)
    clock.setRange(0, 60)
    step(1000)
    // Twice real time at most: a burst of rows is a fast-forward, not a jump.
    expect(clock.t).toBeCloseTo(9.5, 5)
    expect(clock.t).toBeLessThan(57.5)
  })

  it('is left by seeking, and re-entered at the edge by follow()', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.follow(2.5)
    clock.seek(2)
    expect(clock.getSnapshot()).toMatchObject({ following: false, t: 2 })
    clock.setRange(0, 30)
    clock.follow(2.5)
    expect(clock.getSnapshot()).toMatchObject({ following: true, t: 27.5 })
  })

  it('is left by pausing', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.follow()
    clock.togglePlay()
    expect(clock.getSnapshot()).toMatchObject({ following: false, playing: false })
  })

  it('ends at the end of the range when the run finishes', () => {
    const clock = new PlaybackClock()
    clock.setRange(0, 10)
    clock.follow(2.5)
    clock.finish()
    expect(clock.getSnapshot()).toMatchObject({ following: false, playing: false, t: 10 })
    // A clock that was not following is not touched.
    clock.seek(3)
    clock.finish()
    expect(clock.t).toBe(3)
  })

  it('never starts before the range, however large the buffer', () => {
    const clock = new PlaybackClock()
    clock.setRange(5, 6)
    clock.follow(2.5)
    expect(clock.t).toBe(5)
  })
})
