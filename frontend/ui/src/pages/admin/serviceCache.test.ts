import { describe, expect, it } from 'vitest'
import { clearableBytes, heldBytes } from './serviceCache'

const report = {
  caches: [
    { name: 'scene cache', size_bytes: 10_000, entries: 3 },
    { name: 'thumbnail cache', size_bytes: 500, entries: 2 },
  ],
  kept: [
    {
      cache: 'scene cache',
      name: 'world-1',
      size_bytes: 4_000,
      reason: 'a viewer is loading it right now',
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

// Every cache the service reports is one row and one share of the clear, whatever its name: the
// built campaign tables are a cache beside the compiled worlds, not a special case.
describe('a further cache', () => {
  const withTables = {
    ...report,
    caches: [...report.caches, { name: 'table cache', size_bytes: 2_000, entries: 7 }],
  }

  it('counts toward what is held and what a clear frees', () => {
    expect(heldBytes(withTables)).toBe(12_500)
    expect(clearableBytes(withTables)).toBe(8_500)
  })
})
