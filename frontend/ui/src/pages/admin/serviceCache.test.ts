import { describe, expect, it } from 'vitest'
import { clearableBytes, heldBytes } from './serviceCache'

const report = {
  caches: [
    { name: 'object-store fetch cache', size_bytes: 10_000, entries: 3 },
    { name: 'scene cache', size_bytes: 500, entries: 2 },
  ],
  kept: [
    {
      cache: 'object-store fetch cache',
      name: 'camp-1',
      size_bytes: 4_000,
      reason: 'the campaign is still running',
    },
  ],
  freed_bytes: 0,
  removed_entries: 0,
}

describe('serviceCache', () => {
  it('sums what every cache holds', () => {
    expect(heldBytes(report)).toBe(10_500)
  })

  it('offers only what a clear would actually free', () => {
    expect(clearableBytes(report)).toBe(6_500)
  })

  it('offers nothing when everything held is in use', () => {
    expect(clearableBytes({ ...report, kept: [{ ...report.kept[0], size_bytes: 10_500 }] })).toBe(
      0,
    )
  })
})
