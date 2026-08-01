// TimeSeriesSource: the common interface every state-at-time panel renders. A run's data (from any
// source -- a rosbag converted to CSV, a sim that writes CSV directly, and later a live buffer) lands
// as "a table with a time column"; a TimeSeriesSource wraps one such table as a time-indexed view.
//
// This is the seam that keeps the panels source-agnostic and free of duplicated plumbing: the
// CAST-to-REAL (all data.db columns are TEXT), the sort-by-time, and the nearest-sample lookup all
// live here, once. A panel binds a `{ table, time_column }` from its .vast spec, gets a
// TimeSeriesSource, and just renders `at(t)` / `upTo(t)` / `all()`. A future live view implements this
// same interface over a rosbridge buffer without any panel change.

import { useQuery } from '@tanstack/react-query'
import type { DataProvider, DataRow } from './dataProvider'

/** How a .vast spec names a time series: a data.db table, its time column (default `timestamp`), and
 *  an optional equality filter to isolate one series from a multi-keyed table (e.g. `{ frame: base_link }`). */
export interface TimeSeriesBinding {
  table: string
  time_column?: string
  filter?: Record<string, string | number>
}

export interface TimeSeriesSource {
  /** [min, max] of the time column, or null if the table has no rows. Feeds PlaybackClock.setRange. */
  range(): [number, number] | null
  /** State at t: the latest sample whose time is <= t (or the earliest sample if t precedes them all).
   *  null only when the series is empty. */
  at(t: number): DataRow | null
  /** All samples up to and including t, in time order (the trail / partial history). */
  upTo(t: number): DataRow[]
  /** Every sample, in time order (static chart lines). */
  all(): DataRow[]
  /** The columns present on the rows (from the first sample). */
  columns: string[]
  /** The numeric time (seconds) extracted from a row, using this source's time column. */
  timeOf(row: DataRow): number
}

const DEFAULT_TIME_COLUMN = 'timestamp'

/** Build a TimeSeriesSource from rows already loaded (via DataProvider.series or any other origin).
 *  Rows are sorted by the coerced time here, so callers need not pre-sort. */
export function timeSeriesFromRows(rows: DataRow[], timeColumn = DEFAULT_TIME_COLUMN): TimeSeriesSource {
  const timeOf = (row: DataRow) => Number(row[timeColumn])
  // Sort a shallow copy by numeric time; drop rows whose time isn't a finite number so lookups and
  // the range stay well-defined.
  const sorted = rows
    .filter((r) => Number.isFinite(timeOf(r)))
    .slice()
    .sort((a, b) => timeOf(a) - timeOf(b))
  const times = sorted.map(timeOf)
  const columns = sorted.length ? Object.keys(sorted[0]) : []

  // Index of the last sample with time <= t via binary search (rightmost). -1 if t precedes all.
  const lastAtOrBefore = (t: number): number => {
    let lo = 0
    let hi = times.length - 1
    let ans = -1
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if (times[mid] <= t) {
        ans = mid
        lo = mid + 1
      } else {
        hi = mid - 1
      }
    }
    return ans
  }

  return {
    range() {
      if (!sorted.length) return null
      return [times[0], times[times.length - 1]]
    },
    at(t) {
      if (!sorted.length) return null
      const i = lastAtOrBefore(t)
      // Before the first sample, clamp to the earliest so a panel still shows a defined state.
      return sorted[i >= 0 ? i : 0]
    },
    upTo(t) {
      const i = lastAtOrBefore(t)
      return i >= 0 ? sorted.slice(0, i + 1) : []
    },
    all() {
      return sorted
    },
    columns,
    timeOf,
  }
}

/** Resolve a binding to a TimeSeriesSource by bulk-loading the run's rows once through the provider.
 *  `columns` narrows the SELECT (the time column is always included). */
export async function buildTimeSeriesSource(
  binding: TimeSeriesBinding,
  data: DataProvider,
  columns?: string[],
): Promise<TimeSeriesSource> {
  const timeCol = binding.time_column ?? DEFAULT_TIME_COLUMN
  const cols = columns && columns.length ? Array.from(new Set([timeCol, ...columns])) : undefined
  const rows = await data.series(binding.table, { timeCol, columns: cols, match: binding.filter })
  return timeSeriesFromRows(rows, timeCol)
}

/** React Query wrapper so panels get `{ data: source, isPending, error }` and share the cache by
 *  (table, time_column, columns). Panels index into the returned source with the clock's `t`. */
export function useTimeSeries(binding: TimeSeriesBinding, data: DataProvider, columns?: string[]) {
  const timeCol = binding.time_column ?? DEFAULT_TIME_COLUMN
  return useQuery({
    queryKey: ['time-series', binding.table, timeCol, binding.filter ?? null, columns ?? null],
    queryFn: () => buildTimeSeriesSource(binding, data, columns),
    retry: false,
  })
}

/** How a .vast spec names a *multi-keyed* table: one series per distinct value of `key`. `poses` is
 *  the canonical one -- keyed by `frame`, it holds every TF frame a run recorded, which is one series
 *  per moving thing in the world rather than one per table. */
export interface TimeSeriesGroupBinding extends TimeSeriesBinding {
  key: string
  /** Cap the samples per series at this rate (see SeriesOptions.decimate). */
  decimate_hz?: number
}

/** Resolve a multi-keyed table to one TimeSeriesSource per key value, in **one** query.
 *
 * Deliberately not N queries: the number of series is a property of the *world* (a robot is 1, a
 * walker is 17 bones, each prop is 1 more), so per-series fetching would make the round-trip count
 * scale with how much is moving. One decimated query stays flat instead.
 */
export async function buildTimeSeriesGroups(
  binding: TimeSeriesGroupBinding,
  data: DataProvider,
  columns?: string[],
  maxRows?: number,
): Promise<Map<string, TimeSeriesSource>> {
  const timeCol = binding.time_column ?? DEFAULT_TIME_COLUMN
  const cols = columns?.length
    ? Array.from(new Set([binding.key, timeCol, ...columns]))
    : undefined
  const rows = await data.series(binding.table, {
    timeCol,
    columns: cols,
    match: binding.filter,
    maxRows,
    decimate: binding.decimate_hz ? { hz: binding.decimate_hz, key: binding.key } : undefined,
  })
  const byKey = new Map<string, DataRow[]>()
  for (const row of rows) {
    const k = row[binding.key]
    if (k == null) continue
    const list = byKey.get(String(k))
    if (list) list.push(row)
    else byKey.set(String(k), [row])
  }
  return new Map(
    Array.from(byKey, ([k, list]) => [k, timeSeriesFromRows(list, timeCol)] as const),
  )
}

/** React Query wrapper around {@link buildTimeSeriesGroups}. */
export function useTimeSeriesGroups(
  binding: TimeSeriesGroupBinding,
  data: DataProvider,
  columns?: string[],
  maxRows?: number,
) {
  const timeCol = binding.time_column ?? DEFAULT_TIME_COLUMN
  return useQuery({
    queryKey: [
      'time-series-groups',
      binding.table,
      binding.key,
      timeCol,
      binding.filter ?? null,
      binding.decimate_hz ?? null,
      columns ?? null,
      maxRows ?? null,
    ],
    queryFn: () => buildTimeSeriesGroups(binding, data, columns, maxRows),
    retry: false,
  })
}
