import { describe, expect, it } from 'vitest'

import {
  insertFrameTime,
  jpegDataUrl,
  nearestFrameAtOrBefore,
  parseFrameEvent,
  parseFrameIndex,
} from './frameIndex'

describe('nearestFrameAtOrBefore', () => {
  const times = [1, 2.5, 4, 8]

  it('is the newest frame at or before t', () => {
    expect(nearestFrameAtOrBefore(times, 2.5)).toBe(2.5)
    expect(nearestFrameAtOrBefore(times, 3.9)).toBe(2.5)
    expect(nearestFrameAtOrBefore(times, 100)).toBe(8)
    expect(nearestFrameAtOrBefore(times, 1)).toBe(1)
  })

  it('is null before the first frame, and for an empty index', () => {
    expect(nearestFrameAtOrBefore(times, 0.5)).toBeNull()
    expect(nearestFrameAtOrBefore([], 3)).toBeNull()
  })

  it('maps every t between two frames to the same frame, so a scrub fetches once', () => {
    const picks = new Set([2.5, 2.6, 3, 3.99].map((t) => nearestFrameAtOrBefore(times, t)))
    expect([...picks]).toEqual([2.5])
  })
})

describe('insertFrameTime', () => {
  it('appends a newer time without re-sorting', () => {
    expect(insertFrameTime([1, 2], 3)).toEqual([1, 2, 3])
  })

  it('keeps the index sorted for an older time', () => {
    expect(insertFrameTime([1, 3], 2)).toEqual([1, 2, 3])
  })

  it('returns the same array for a time already in it', () => {
    const times = [1, 2, 3]
    expect(insertFrameTime(times, 2)).toBe(times)
  })

  it('starts an empty index', () => {
    expect(insertFrameTime([], 5)).toEqual([5])
  })
})

describe('parseFrameIndex', () => {
  it('reads the times, sorted, coercing numeric strings', () => {
    expect(parseFrameIndex({ topic: '/cam', times: [3, '1', 2] })).toEqual({
      topic: '/cam', times: [1, 2, 3],
    })
  })

  it('refuses another shape', () => {
    expect(() => parseFrameIndex({ times: [1] })).toThrow(/frame-index/)
    expect(() => parseFrameIndex({ topic: '/cam', times: [1, 'x'] })).toThrow(/finite/)
    expect(() => parseFrameIndex(null)).toThrow()
  })
})

describe('parseFrameEvent', () => {
  it('reads topic, t and the jpeg payload', () => {
    expect(parseFrameEvent('{"topic":"/cam","t":"2.5","jpeg_base64":"/9j/"}')).toEqual({
      topic: '/cam', t: 2.5, jpegBase64: '/9j/',
    })
  })

  it('refuses a frame without its picture or its moment', () => {
    expect(() => parseFrameEvent('{"topic":"/cam","t":1}')).toThrow(/live frame/)
    expect(() => parseFrameEvent('{"topic":"/cam","jpeg_base64":"x"}')).toThrow(/live frame/)
  })
})

describe('jpegDataUrl', () => {
  it('wraps the payload as an image data URL', () => {
    expect(jpegDataUrl('AAA=')).toBe('data:image/jpeg;base64,AAA=')
  })
})
