// What the stream's rows become before they join a series: the binding's filter and thinning
// applied the way SQL applies them to the history, the overlap with the history dropped, and the
// bound that keeps a live series from growing without limit.
import { describe, expect, it } from 'vitest'

import { afterHistory, appendBounded, liveShape, shapeLiveRows } from './liveSeries'

const row = (timestamp: number, frame: string, x = 0) => ({ timestamp, frame, 'position.x': x, extra: 1 })

describe('shapeLiveRows', () => {
  it('applies the filter, drops rows without a time, and narrows to the columns asked for', () => {
    const shape = liveShape({ table: 'poses', filter: { frame: 'base' } }, ['position.x'])
    const out = shapeLiveRows([row(1, 'base', 5), row(1, 'prop'), { frame: 'base' }], shape)
    expect(out).toEqual([{ timestamp: 1, 'position.x': 5 }])
  })

  it('keeps the first row of each bucket per key when thinned, across batches', () => {
    const shape = liveShape({ table: 'poses', decimate_hz: 1, key: 'frame' })
    const first = shapeLiveRows([row(0.1, 'a', 1), row(0.5, 'a', 2), row(0.2, 'b', 3)], shape)
    expect(first.map((r) => r['position.x'])).toEqual([1, 3])
    const second = shapeLiveRows([row(0.9, 'a', 4), row(1.1, 'a', 5)], shape)
    expect(second.map((r) => r['position.x'])).toEqual([5])
    // After a gap the history is re-read and the buckets start over.
    shape.thinner!.reset()
    expect(shapeLiveRows([row(1.2, 'a', 6)], shape)).toHaveLength(1)
  })

  it('reads a TEXT time column', () => {
    const shape = liveShape({ table: 'poses' })
    expect(shapeLiveRows([{ timestamp: '2.5' }], shape)).toEqual([{ timestamp: '2.5' }])
  })
})

describe('afterHistory', () => {
  it('keeps the rows later than the history\'s last sample', () => {
    const timeOf = (r: Record<string, unknown>) => Number(r.timestamp)
    const history = [row(1, 'a'), row(2, 'a')]
    expect(afterHistory(history, [row(2, 'a'), row(3, 'a')], timeOf)).toEqual([row(3, 'a')])
    expect(afterHistory([], [row(2, 'a')], timeOf)).toEqual([row(2, 'a')])
  })
})

describe('appendBounded', () => {
  it('keeps the newest rows and counts the oldest it let go', () => {
    const { rows, dropped } = appendBounded([row(1, 'a'), row(2, 'a')], [row(3, 'a'), row(4, 'a')], 3)
    expect(rows.map((r) => r.timestamp)).toEqual([2, 3, 4])
    expect(dropped).toBe(1)
    expect(appendBounded([row(1, 'a')], [], 3)).toEqual({ rows: [row(1, 'a')], dropped: 0 })
  })
})
