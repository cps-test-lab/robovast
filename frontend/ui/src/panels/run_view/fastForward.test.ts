import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PlaybackClock } from '@robovast/panel-kit'
import { fastForward, nextSpeed } from './fastForward'

describe('nextSpeed', () => {
  it('cycles 1, 2, 4, 8 and wraps back to real time', () => {
    expect([1, 2, 4, 8].map(nextSpeed)).toEqual([2, 4, 8, 1])
  })

  it('steps a speed outside the list to 2', () => {
    expect(nextSpeed(3)).toBe(2)
  })
})

describe('fastForward', () => {
  // The clock drives playback from requestAnimationFrame, which node does not have; a frame that
  // never fires keeps `t` still, so only the playing flag and the speed are under test.
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })
  afterEach(() => vi.unstubAllGlobals())

  const clock = () => {
    const c = new PlaybackClock()
    c.setRange(0, 10)
    return c
  }

  it('starts a paused clock and advances its speed', () => {
    const c = clock()
    fastForward(c)
    expect(c.getSnapshot()).toMatchObject({ playing: true, speed: 2 })
  })

  it('only cycles the speed of a playing clock', () => {
    const c = clock()
    c.play()
    c.setSpeed(8)
    fastForward(c)
    expect(c.getSnapshot()).toMatchObject({ playing: true, speed: 1 })
  })

  it('replays from the start when paused at the end', () => {
    const c = clock()
    c.seek(10)
    fastForward(c)
    expect(c.getSnapshot()).toMatchObject({ playing: true, speed: 2, t: 0 })
  })
})
