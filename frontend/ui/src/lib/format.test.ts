import { describe, expect, it } from 'vitest'
import { formatBytesPair } from './format'

// The sidebar's disk and store meters print through this. It exists because the memory
// bar's fixed-GiB style ("412/1863") stops being readable at the TiB scale a node
// filesystem reaches, and because both numbers must land in ONE unit to fit the track.
describe('formatBytesPair', () => {
  it('scales both numbers by the capacity unit, not each by its own', () => {
    // 0.4 TiB against 1.8 TiB: the used value stays in the capacity's unit rather than
    // rendering as "410 GiB", which would read as two unrelated magnitudes.
    expect(formatBytesPair(0.4 * 1024 ** 4, 1.8 * 1024 ** 4)).toBe('0.4/1.8 TiB')
  })

  it('uses GiB while the capacity is under a TiB', () => {
    expect(formatBytesPair(82 * 1024 ** 3, 512 * 1024 ** 3)).toBe('82/512 GiB')
  })

  it('switches to one decimal below ten, and rounds above it', () => {
    expect(formatBytesPair(1.44 * 1024 ** 3, 9 * 1024 ** 3)).toBe('1.4/9.0 GiB')
    expect(formatBytesPair(10.6 * 1024 ** 3, 64 * 1024 ** 3)).toBe('11/64 GiB')
  })

  it('renders an empty disk as zero rather than as nothing', () => {
    expect(formatBytesPair(0, 512 * 1024 ** 3)).toBe('0.0/512 GiB')
  })
})
