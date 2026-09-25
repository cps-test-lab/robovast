// TimeSeriesSource: the common interface every state-at-time panel renders. A run's data (from any
// source -- a rosbag converted to CSV, a sim that writes CSV directly, and later a live buffer) lands
// as "a table with a time column"; a TimeSeriesSource wraps one such table as a time-indexed view.
//
// This is the seam that keeps the panels source-agnostic and free of duplicated plumbing: the
// CAST-to-REAL (a TEXT results column), the sort-by-time, and the nearest-sample lookup all
// live here, once. A panel binds a `{ table, time_column }` from its .vast spec, gets a
// TimeSeriesSource, and just renders `at(t)` / `upTo(t)` / `all()`.
//
// A run that is still recording is the same source with a tail: the history comes through SQL
// once, and the rows the provider's live subscription delivers after it are appended here, so a
// panel re-renders with a longer series when rows land and never asks again on its own. On `eof`
// -- and after a gap in the stream -- the history is re-read once (see `useLiveTail`).

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { lastAtOrBefore, type DataProvider, type DataRow } from '@robovast/panel-kit'
import { isLiveProvider } from './dataProvider'
import { afterHistory, appendBounded, liveShape, shapeLiveRows } from './liveSeries'

/** How a .vast spec names a time series: a results table, its time column (default `timestamp`), and
 *  an optional equality filter to isolate one series from a multi-keyed table (e.g. `{ frame: base_link }`). */
export interface TimeSeriesBinding {
  table: string
  time_column?: string
  filter?: Record<string, string | number>
  /** Thin the rows to one sample per 1/hz second across the WHOLE run, in SQL.
   *
   *  Without it a run longer than `max_rows / rate` is cut at the HEAD: the row cap is a LIMIT after
   *  ORDER BY time, so the chart ends mid-run rather than getting coarser, and the service clamps
   *  that cap at 5000 no matter what a panel asks for. Rule of thumb: 4000 / run seconds. */
  decimate_hz?: number
  /** The column identifying one series in a multi-keyed table (`poses` is keyed by `frame`).
   *
   *  Needed with `decimate_hz` unless `filter` already isolates one series: a bucket keeps one row
   *  from ONE key, so undecimated-looking series simply disappear. */
  key?: string
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
  /** Whether the query hit the row cap, i.e. these are the first samples of the run and the rest is
   *  missing. Comes from the service, which fetches one row past the cap to know -- a row count
   *  cannot tell, since rows with an unparseable time are dropped below. */
  truncated: boolean
  /** The service's explanation when it truncated for a reason other than the row cap (the reply-size
   *  ceiling). Null for a plain row-cap truncation, which the panels already word themselves. */
  truncationNote: string | null
  /** How many of the OLDEST rows a live series let go to stay within its bound
   *  (`MAX_LIVE_ROWS`): the newest are kept, so the series starts later than the run did. Zero
   *  for a series read whole. */
  dropped: number
  /** The columns present on the rows (from the first sample). */
  columns: string[]
  /** The numeric time (seconds) extracted from a row, using this source's time column. */
  timeOf(row: DataRow): number
}

const DEFAULT_TIME_COLUMN = 'timestamp'

/** Build a TimeSeriesSource from rows already loaded (via DataProvider.series or any other origin).
 *  Rows are sorted by the coerced time here, so callers need not pre-sort. */
export function timeSeriesFromRows(
  rows: DataRow[],
  timeColumn = DEFAULT_TIME_COLUMN,
  truncated = false,
  truncationNote: string | null = null,
  dropped = 0,
): TimeSeriesSource {
  const timeOf = (row: DataRow) => Number(row[timeColumn])
  // Sort a shallow copy by numeric time; drop rows whose time isn't a finite number so lookups and
  // the range stay well-defined.
  const sorted = rows
    .filter((r) => Number.isFinite(timeOf(r)))
    .slice()
    .sort((a, b) => timeOf(a) - timeOf(b))
  const times = sorted.map(timeOf)
  const columns = sorted.length ? Object.keys(sorted[0]) : []

  return {
    range() {
      if (!sorted.length) return null
      return [times[0], times[times.length - 1]]
    },
    at(t) {
      if (!sorted.length) return null
      const i = lastAtOrBefore(times, t)
      // Before the first sample, clamp to the earliest so a panel still shows a defined state.
      return sorted[i >= 0 ? i : 0]
    },
    upTo(t) {
      const i = lastAtOrBefore(times, t)
      return i >= 0 ? sorted.slice(0, i + 1) : []
    },
    all() {
      return sorted
    },
    truncated,
    truncationNote,
    dropped,
    columns,
    timeOf,
  }
}

/** The rows a live run delivered after the history, shaped as the binding reads them.
 *
 *  Subscribed for a live provider only; a finished run has no tail. The tail is bounded like the
 *  series it joins, and is emptied when the stream reports a gap or the end of the run -- both
 *  times the history is re-read through SQL, which then holds what the tail held. `version`
 *  moves on every change so a memo over the tail recomputes. */
function useLiveTail(
  data: DataProvider,
  binding: TimeSeriesBinding,
  columns: string[] | undefined,
  queryKey: readonly unknown[],
) {
  const queryClient = useQueryClient()
  const tail = useRef<{ rows: DataRow[]; dropped: number }>({ rows: [], dropped: 0 })
  const [version, setVersion] = useState(0)
  const keyString = JSON.stringify(queryKey)
  useEffect(() => {
    if (!isLiveProvider(data) || !data.live) return
    const shape = liveShape(binding, columns)
    tail.current = { rows: [], dropped: 0 }
    return data.subscribeLive(binding.table, (event) => {
      if (event.kind === 'batch') {
        const add = shapeLiveRows(event.rows, shape)
        if (!add.length) return
        const next = appendBounded(tail.current.rows, add)
        tail.current = { rows: next.rows, dropped: tail.current.dropped + next.dropped }
        setVersion((v) => v + 1)
      } else if (event.kind === 'gap' || event.kind === 'eof') {
        tail.current = { rows: [], dropped: 0 }
        shape.thinner?.reset()
        setVersion((v) => v + 1)
        queryClient.invalidateQueries({ queryKey })
      }
      // An error is followed by `eof`, which is handled above; the message is the feed's to show.
    })
    // The binding and the columns are in the key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, keyString, queryClient])
  return { tail: tail.current, version }
}

/** `history` with the live rows after it, bounded. */
function withTail(
  history: TimeSeriesSource,
  tail: { rows: DataRow[]; dropped: number },
  timeCol: string,
): TimeSeriesSource {
  if (!tail.rows.length && !tail.dropped) return history
  const all = history.all()
  const add = afterHistory(all, tail.rows, history.timeOf)
  const { rows, dropped } = appendBounded(all, add)
  return timeSeriesFromRows(
    rows, timeCol, history.truncated, history.truncationNote, tail.dropped + dropped)
}

/** Resolve a binding to a TimeSeriesSource by bulk-loading the run's rows once through the provider.
 *  `columns` narrows the SELECT (the time column is always included). */
export async function buildTimeSeriesSource(
  binding: TimeSeriesBinding,
  data: DataProvider,
  columns?: string[],
  maxRows?: number,
): Promise<TimeSeriesSource> {
  const timeCol = binding.time_column ?? DEFAULT_TIME_COLUMN
  // The key column has to survive into the rows for the GROUP BY to be honest about what it kept.
  const named = columns?.length ? [timeCol, ...columns, ...(binding.key ? [binding.key] : [])] : null
  const page = await data.seriesPage(binding.table, {
    timeCol,
    columns: named ? Array.from(new Set(named)) : undefined,
    match: binding.filter,
    maxRows,
    decimate: binding.decimate_hz ? { hz: binding.decimate_hz, key: binding.key } : undefined,
  })
  return timeSeriesFromRows(page.rows, timeCol, page.truncated, page.note ?? null)
}

/** React Query wrapper so panels get `{ data: source, isPending, error }` and share the cache by
 *  (run, table, time_column, columns) -- the run scope first, because table names repeat across
 *  campaigns and a shared cache would otherwise hand one campaign's rows to another. Panels index into
 *  the returned source with the clock's `t`. */
export function useTimeSeries(
  binding: TimeSeriesBinding,
  data: DataProvider,
  columns?: string[],
  maxRows?: number,
) {
  const timeCol = binding.time_column ?? DEFAULT_TIME_COLUMN
  const queryKey = [
    'time-series',
    data.scope,
    binding.table,
    timeCol,
    binding.filter ?? null,
    binding.key ?? null,
    binding.decimate_hz ?? null,
    columns ?? null,
    maxRows ?? null,
  ]
  const query = useQuery({
    queryKey,
    queryFn: () => buildTimeSeriesSource(binding, data, columns, maxRows),
    retry: false,
  })
  const { tail, version } = useLiveTail(data, binding, columns, queryKey)
  const merged = useMemo(
    () => (query.data ? withTail(query.data, tail, timeCol) : query.data),
    // `version` is what says the tail changed; the ref's identity does not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [query.data, version, timeCol],
  )
  return { ...query, data: merged }
}

/** How a .vast spec names a *multi-keyed* table: one series per distinct value of `key`. `poses` is
 *  the canonical one -- keyed by `frame`, it holds every TF frame a run recorded, which is one series
 *  per moving thing in the world rather than one per table. */
export interface TimeSeriesGroupBinding extends TimeSeriesBinding {
  /** Required here, where every distinct value becomes its own series. */
  key: string
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
  const page = await data.seriesPage(binding.table, {
    timeCol,
    columns: cols,
    match: binding.filter,
    maxRows,
    decimate: binding.decimate_hz ? { hz: binding.decimate_hz, key: binding.key } : undefined,
  })
  const byKey = new Map<string, DataRow[]>()
  for (const row of page.rows) {
    const k = row[binding.key]
    if (k == null) continue
    const list = byKey.get(String(k))
    if (list) list.push(row)
    else byKey.set(String(k), [row])
  }
  // The cap applied to the one combined query, so it truncated all of these series or none.
  return new Map(
    Array.from(byKey, ([k, list]) =>
      [k, timeSeriesFromRows(list, timeCol, page.truncated, page.note ?? null)] as const),
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
  const queryKey = [
    'time-series-groups',
    data.scope,
    binding.table,
    binding.key,
    timeCol,
    binding.filter ?? null,
    binding.decimate_hz ?? null,
    columns ?? null,
    maxRows ?? null,
  ]
  const query = useQuery({
    queryKey,
    queryFn: () => buildTimeSeriesGroups(binding, data, columns, maxRows),
    retry: false,
  })
  const { tail, version } = useLiveTail(data, binding, columns, queryKey)
  const merged = useMemo(() => {
    if (!query.data || (!tail.rows.length && !tail.dropped)) return query.data
    // The tail by key, then each series takes its own; a key the history never saw -- a body
    // that appeared mid-run -- becomes a series of its own.
    const byKey = new Map<string, DataRow[]>()
    for (const row of tail.rows) {
      const k = row[binding.key]
      if (k == null) continue
      const list = byKey.get(String(k))
      if (list) list.push(row)
      else byKey.set(String(k), [row])
    }
    const out = new Map(query.data)
    for (const [k, rows] of byKey) {
      const history = out.get(k) ?? timeSeriesFromRows([], timeCol)
      out.set(k, withTail(history, { rows, dropped: tail.dropped }, timeCol))
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data, version, timeCol, binding.key])
  return { ...query, data: merged }
}

/** Re-read a query of a live run's table whenever the table grows.
 *
 *  For a reader that builds something other than a time series from the rows (the scenario tree)
 *  and re-derives it from the whole table: a batch invalidates the query, coalesced so a table
 *  that grows every tick is re-read at most once per `minGapMs`. Nothing is asked of a finished
 *  run, and the run's end re-reads once. */
export function useLiveTable(
  data: DataProvider,
  table: string,
  queryKey: readonly unknown[],
  minGapMs = 1000,
) {
  const queryClient = useQueryClient()
  const keyString = JSON.stringify(queryKey)
  useEffect(() => {
    if (!isLiveProvider(data) || !data.live) return
    let last = 0
    let pending: ReturnType<typeof setTimeout> | null = null
    const refetch = () => {
      pending = null
      last = Date.now()
      queryClient.invalidateQueries({ queryKey })
    }
    const unsubscribe = data.subscribeLive(table, (event) => {
      if (event.kind === 'error') return
      if (event.kind !== 'batch') {
        if (pending != null) clearTimeout(pending)
        refetch()
        return
      }
      if (pending != null) return
      const wait = minGapMs - (Date.now() - last)
      if (wait <= 0) refetch()
      else pending = setTimeout(refetch, wait)
    })
    return () => {
      unsubscribe()
      if (pending != null) clearTimeout(pending)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, table, keyString, minGapMs, queryClient])
}
