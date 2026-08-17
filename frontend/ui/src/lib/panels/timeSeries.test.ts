// `truncated` is a property of the QUERY, not of the rows that survive it -- which is the whole
// reason it is threaded through here rather than recomputed as `rows.length >= cap`. A source drops
// rows whose time does not parse, so a row count under-reports truncation in exactly the case the
// warning exists for, and over-reports it for a table holding exactly `max_rows` rows.
import { describe, expect, it } from 'vitest'
import { timeSeriesFromRows } from './timeSeries'

const rows = [
  { timestamp: '2.0', x: '2' },
  { timestamp: '1.0', x: '1' },
  { timestamp: 'n/a', x: '9' },
]

describe('timeSeriesFromRows', () => {
  it('sorts by time and drops samples with no usable time', () => {
    const src = timeSeriesFromRows(rows)
    expect(src.all().map((r) => r.x)).toEqual(['1', '2'])
    expect(src.range()).toEqual([1, 2])
  })

  it('reports the query as complete unless told otherwise', () => {
    expect(timeSeriesFromRows(rows).truncated).toBe(false)
  })

  it('keeps truncation set even though a dropped row put the count under the cap', () => {
    const src = timeSeriesFromRows(rows, 'timestamp', true)
    expect(src.all()).toHaveLength(2) // < the 3 rows the query returned
    expect(src.truncated).toBe(true)
  })
})
