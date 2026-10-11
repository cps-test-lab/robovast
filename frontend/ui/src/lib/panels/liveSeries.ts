// The pure half of a live time series: what the stream's rows become before they join the rows a
// query gave. A panel's `source` binding is applied in SQL for the history (`buildSeriesSql`) and
// here for the rows that follow it, so the two halves of one series agree about the filter, the
// thinning and the columns; and the series is bounded, because a stream has no LIMIT.

import type { DataRow } from '@robovast/panel-kit'
import type { TimeSeriesBinding } from './timeSeries'

/** How many rows a live series keeps. The oldest go first, and the source says how many did. Ten
 *  minutes of a 25 Hz column is 15k rows, so a run of ordinary length stays whole. */
export const MAX_LIVE_ROWS = 20000

/** The decimation state of one series: the last bucket kept per key, so one row per 1/hz second
 *  survives -- the bucket's first, which is what `buildSeriesSql`'s `DISTINCT ON` keeps of the
 *  history. */
export class Thinner {
  private readonly last = new Map<string, number>()

  constructor(private readonly hz: number, private readonly key?: string) {}

  /** Whether `row` is the first of its bucket, and so kept. */
  keep(row: DataRow, t: number): boolean {
    const bucket = Math.trunc(t * this.hz)
    const k = this.key ? String(row[this.key]) : ''
    if (this.last.get(k) === bucket) return false
    this.last.set(k, bucket)
    return true
  }

  /** Forget the buckets seen: after a gap the history is re-read, and its last bucket is not the
   *  stream's. Kept rows then may repeat a bucket once at the seam, which is one extra row. */
  reset(): void {
    this.last.clear()
  }
}

/** The state that shapes a stream's rows into one series, across batches. */
export interface LiveShape {
  binding: TimeSeriesBinding
  columns?: string[]
  thinner: Thinner | null
}

export function liveShape(binding: TimeSeriesBinding, columns?: string[]): LiveShape {
  const hz = Number(binding.decimate_hz)
  return {
    binding,
    columns,
    thinner: Number.isFinite(hz) && hz > 0 ? new Thinner(hz, binding.key) : null,
  }
}

/** The stream's rows as the binding reads them: those matching `filter`, one per bucket when
 *  thinned, narrowed to the columns asked for plus the time column and the key. Rows whose time
 *  does not parse are dropped, as `timeSeriesFromRows` drops them. */
export function shapeLiveRows(rows: DataRow[], shape: LiveShape): DataRow[] {
  const { binding, columns, thinner } = shape
  const timeCol = binding.time_column ?? 'timestamp'
  const match = Object.entries(binding.filter ?? {})
  const keep = columns?.length
    ? Array.from(new Set([timeCol, ...columns, ...(binding.key ? [binding.key] : [])]))
    : null
  const out: DataRow[] = []
  for (const row of rows) {
    const t = Number(row[timeCol])
    if (!Number.isFinite(t)) continue
    if (match.some(([col, val]) => String(row[col]) !== String(val))) continue
    if (thinner && !thinner.keep(row, t)) continue
    if (!keep) {
      out.push(row)
      continue
    }
    const narrow: DataRow = {}
    for (const c of keep) narrow[c] = row[c]
    out.push(narrow)
  }
  return out
}

/** Rows after the history: those later than its last sample. The stream is opened before the
 *  history is read, so what lands meanwhile overlaps it; equal times are the history's, and the
 *  stream's copies of them are dropped. */
export function afterHistory(history: DataRow[], add: DataRow[], timeOf: (r: DataRow) => number) {
  if (!history.length) return add
  const last = timeOf(history[history.length - 1])
  return add.filter((r) => timeOf(r) > last)
}

/** Append, keeping the newest `max`. Returns how many of the oldest went. */
export function appendBounded(
  rows: DataRow[], add: DataRow[], max = MAX_LIVE_ROWS,
): { rows: DataRow[]; dropped: number } {
  const all = rows.concat(add)
  const over = all.length - max
  return over > 0 ? { rows: all.slice(over), dropped: over } : { rows: all, dropped: 0 }
}
