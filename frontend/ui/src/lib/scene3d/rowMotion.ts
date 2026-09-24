// rowMotion: the MotionSource built from a run's tables -- `sim_poses` (one row per body per
// sample: `timestamp`, `frame`, `position.x/y/z`, `orientation.x/y/z/w`) and `joint_states` (one
// row per joint per sample: `timestamp`, `joint`, `position`). The simulator's own recording,
// decoded to tables by the service, is where both come from; the same tables answer for a run that
// has finished and for one that is still recording.
//
// Two things the file-shaped source never had to do:
//
//  * **Window, and page.** A run's poses are a table of every body at every tick, far past what one
//    query may return, so the source loads a window of time around where the viewer is looking and
//    pages through it at the query's row cap; samples farther than `keepS` from the window last
//    asked for are let go. `fetch(t0, t1)` is the ask, and the panel makes it as the clock moves.
//  * **Follow.** A run still recording delivers the rows after the loaded history through
//    `RowReader.follow`; they are appended and `subscribe`rs hear it. A gap in the stream, and the
//    end of the run, re-read the window once through the query.
//
// Per track the samples are addressed by time; the source's sample index (`indexAt`/`apply`) is an
// index into the union of every track's times, which for a simulator recording every body per
// tick is simply the tick list. `apply` seats each track at its own sample nearest that tick --
// nearest, ties to the earlier, never blended: two states averaged is a pose the simulation never
// had (see motionSource.ts).
//
// Imports nothing of the host: the reader interface below is what the panel adapts its data
// provider to, so this directory stays extractable.

import type {
  MotionMeta,
  MotionRange,
  MotionSink,
  MotionSource,
  MotionTrack,
} from './motionSource'

export type Row = Record<string, unknown>

/** What a reader of a live table hears; the same protocol as the host's live feed. */
export type RowEvent =
  | { kind: 'batch'; rows: Row[] }
  | { kind: 'gap' }
  | { kind: 'eof' }
  | { kind: 'error'; message: string }

/** Where the rows come from. */
export interface RowReader {
  /** Rows of `table` with time in [t0, t1], ascending by time, at most `maxRows` of them -- and
   *  whether that cap cut the answer, in which case the rows are the FIRST `maxRows` by time. */
  page(
    table: string, t0: number, t1: number, maxRows: number,
  ): Promise<{ rows: Row[]; truncated: boolean }>
  /** The rows of `table` after the history, for a run still recording. Absent for a finished run. */
  follow?(table: string, listener: (event: RowEvent) => void): () => void
}

export interface RowMotionOptions {
  poseTable?: string
  jointTable?: string
  timeCol?: string
  /** The query's row cap: how many rows one page asks for. */
  pageRows?: number
  /** Seconds beyond the window last asked for that loaded samples are kept. */
  keepS?: number
  meta?: MotionMeta
}

export const DEFAULT_POSE_TABLE = 'sim_poses'
export const DEFAULT_JOINT_TABLE = 'joint_states'
const DEFAULT_TIME_COL = 'timestamp'
const DEFAULT_PAGE_ROWS = 5000
const DEFAULT_KEEP_S = 120

const POSE_COLUMNS = ['position.x', 'position.y', 'position.z',
  'orientation.w', 'orientation.x', 'orientation.y', 'orientation.z'] as const

/** A number the row holds, or NaN. A TEXT results column arrives as a string. */
const num = (v: unknown): number => (v == null || v === '' ? NaN : Number(v))

/** Index of the last `times[i] <= t`, or -1. */
function lastAtOrBefore(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length - 1
  let ans = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) {
      ans = mid
      lo = mid + 1
    } else hi = mid - 1
  }
  return ans
}

/** How many of `times` are below `t`: the index the first sample at or after `t` has. */
function lowerBound(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** Nearest sample, ties to the earlier, or -1 for no samples. */
function nearest(times: readonly number[], t: number): number {
  if (!times.length) return -1
  const i = lastAtOrBefore(times, t)
  if (i < 0) return 0
  if (i === times.length - 1) return i
  return t - times[i] <= times[i + 1] - t ? i : i + 1
}

/** One track's samples, sorted by time, one per time. */
class Track {
  readonly times: number[] = []
  readonly values: number[] = []

  constructor(readonly kind: 'joint' | 'pose', readonly name: string, readonly width: number) {}

  /** Insert or replace the sample at `t`. Appending is the common case and O(1). */
  insert(t: number, vals: readonly number[]): void {
    const n = this.times.length
    if (!n || t > this.times[n - 1]) {
      this.times.push(t)
      for (const v of vals) this.values.push(v)
      return
    }
    const i = lastAtOrBefore(this.times, t)
    if (i >= 0 && this.times[i] === t) {
      for (let k = 0; k < this.width; k++) this.values[i * this.width + k] = vals[k]
      return
    }
    this.times.splice(i + 1, 0, t)
    this.values.splice((i + 1) * this.width, 0, ...vals)
  }

  /** Drop the samples outside [lo, hi]. */
  trim(lo: number, hi: number): void {
    const from = lowerBound(this.times, lo)
    const to = lastAtOrBefore(this.times, hi) + 1
    if (to < this.times.length) {
      this.times.splice(to)
      this.values.splice(to * this.width)
    }
    if (from > 0) {
      this.times.splice(0, from)
      this.values.splice(0, from * this.width)
    }
  }
}

/** The sorted union of every track's times: the sample index space. */
class Ticks {
  readonly times: number[] = []

  add(t: number): void {
    const n = this.times.length
    if (!n || t > this.times[n - 1]) {
      this.times.push(t)
      return
    }
    const i = lastAtOrBefore(this.times, t)
    if (i >= 0 && this.times[i] === t) return
    this.times.splice(i + 1, 0, t)
  }

  trim(lo: number, hi: number): void {
    const from = lowerBound(this.times, lo)
    const to = lastAtOrBefore(this.times, hi) + 1
    if (to < this.times.length) this.times.splice(to)
    if (from > 0) this.times.splice(0, from)
  }

  clear(): void {
    this.times.length = 0
  }
}

export function openRowMotion(reader: RowReader, opts: RowMotionOptions = {}): MotionSource {
  const poseTable = opts.poseTable ?? DEFAULT_POSE_TABLE
  const jointTable = opts.jointTable ?? DEFAULT_JOINT_TABLE
  const timeCol = opts.timeCol ?? DEFAULT_TIME_COL
  const pageRows = opts.pageRows ?? DEFAULT_PAGE_ROWS
  const keepS = opts.keepS ?? DEFAULT_KEEP_S
  const meta: MotionMeta = { frame: 'world', timeBase: 'sim', ...opts.meta }

  const tracks = new Map<string, Track>()
  const trackList: MotionTrack[] = []
  const ticks = new Ticks()
  const listeners = new Set<() => void>()
  let disposed = false
  let complete = !reader.follow
  /** The span the loaded samples cover, or null before the first load. */
  let loaded: [number, number] | null = null
  /** The window last asked for: what a gap or the end of the run re-reads. */
  let asked: [number, number] | null = null
  /** Loads run one at a time, in the order asked. */
  let queue: Promise<void> = Promise.resolve()

  const notify = () => {
    if (disposed) return
    for (const l of [...listeners]) l()
  }

  const track = (kind: 'joint' | 'pose', name: string): Track => {
    const key = `${kind}:${name}`
    let t = tracks.get(key)
    if (!t) {
      t = new Track(kind, name, kind === 'pose' ? 7 : 1)
      tracks.set(key, t)
      trackList.push({ kind, name, ...(kind === 'joint' ? { unit: 'rad' } : {}) })
    }
    return t
  }

  /** Insert rows of one table. Returns the time span they covered, or null for none usable. */
  const insert = (table: string, rows: Row[]): [number, number] | null => {
    let lo = Infinity
    let hi = -Infinity
    for (const row of rows) {
      const t = num(row[timeCol])
      if (!Number.isFinite(t)) continue
      if (table === poseTable) {
        const name = row.frame
        if (typeof name !== 'string' || !name) continue
        // The sink takes wxyz (the descriptor's order); the table carries the pose contract's
        // xyzw, so the reordering is here and nowhere else.
        const vals = POSE_COLUMNS.map((c) => num(row[c]))
        if (vals.some((v) => !Number.isFinite(v))) continue
        track('pose', name).insert(t, vals)
      } else {
        const name = row.joint
        const value = num(row.position)
        if (typeof name !== 'string' || !name || !Number.isFinite(value)) continue
        track('joint', name).insert(t, [value])
      }
      ticks.add(t)
      if (t < lo) lo = t
      if (t > hi) hi = t
    }
    return lo <= hi ? [lo, hi] : null
  }

  const extend = (span: [number, number] | null) => {
    if (!span) return
    loaded = loaded
      ? [Math.min(loaded[0], span[0]), Math.max(loaded[1], span[1])]
      : span
  }

  /** Read every row of `table` in [t0, t1], a page at a time. The next page starts at the last
   *  time of the previous one -- inclusive, since a page may end mid-tick -- and `insert` makes
   *  the overlap exact by replacing the sample at an equal time. */
  const loadTable = async (table: string, t0: number, t1: number) => {
    let cursor = t0
    for (;;) {
      const page = await reader.page(table, cursor, t1, pageRows)
      if (disposed) return
      const span = insert(table, page.rows)
      extend(span)
      if (!page.truncated || !span || span[1] <= cursor) return
      cursor = span[1]
    }
  }

  const loadWindow = async (t0: number, t1: number) => {
    // Both tables, whichever the run has: a table the run does not carry fails on its own, and
    // the other still drives what it names. The first failure is what the caller hears.
    const results = await Promise.allSettled([
      loadTable(poseTable, t0, t1),
      loadTable(jointTable, t0, t1),
    ])
    if (disposed) return
    const failed = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected')
    if (failed.length === results.length) throw failed[0].reason
  }

  const trimTo = (lo: number, hi: number) => {
    for (const t of tracks.values()) t.trim(lo, hi)
    ticks.trim(lo, hi)
    if (loaded) loaded = [Math.max(loaded[0], lo), Math.min(loaded[1], hi)]
  }

  const clear = () => {
    tracks.clear()
    trackList.length = 0
    ticks.clear()
    loaded = null
  }

  /** Make [t0, t1] loaded, reading only what is not: an extension on either side of what is
   *  there, or everything afresh when the ask is disjoint from it. Then let go of what lies
   *  farther than `keepS` from the ask. */
  const ensure = async (t0: number, t1: number, reload = false) => {
    if (reload || !loaded || t1 < loaded[0] || t0 > loaded[1]) {
      clear()
      await loadWindow(t0, t1)
    } else {
      const parts: Promise<void>[] = []
      if (t0 < loaded[0]) parts.push(loadWindow(t0, loaded[0]))
      if (t1 > loaded[1]) parts.push(loadWindow(loaded[1], t1))
      await Promise.all(parts)
    }
    if (disposed) return
    // The ask is loaded whether or not it held rows: a window of a table that has nothing yet
    // is not asked again until the viewer moves on.
    extend([t0, t1])
    trimTo(t0 - keepS, t1 + keepS)
    notify()
  }

  const enqueue = (work: () => Promise<void>): Promise<void> => {
    const next = queue.then(work)
    // The queue itself never rejects: a failed load is the asker's to hear, not the next one's.
    queue = next.catch(() => undefined)
    return next
  }

  const onEvent = (table: string) => (event: RowEvent) => {
    if (disposed) return
    switch (event.kind) {
      case 'batch': {
        // Appended only while the tail is loaded: a viewer looking at an earlier window would
        // otherwise accumulate every row of the run behind its back.
        if (!loaded || !asked || asked[1] + keepS < loaded[1]) return
        const span = insert(table, event.rows)
        if (!span) return
        extend(span)
        notify()
        return
      }
      case 'gap':
        if (asked) void enqueue(() => ensure(asked![0], asked![1], true)).catch(() => undefined)
        return
      case 'eof':
        complete = true
        if (asked) void enqueue(() => ensure(asked![0], asked![1], true)).catch(() => undefined)
        else notify()
        return
      case 'error':
        // `eof` follows, and the re-read there says what the tables hold.
        return
    }
  }

  const unfollow = reader.follow
    ? [reader.follow(poseTable, onEvent(poseTable)), reader.follow(jointTable, onEvent(jointTable))]
    : []

  return {
    range(): MotionRange {
      const n = ticks.times.length
      return { t0: n ? ticks.times[0] : 0, t1: n ? ticks.times[n - 1] : 0, complete }
    },

    tracks: () => trackList,
    meta: () => meta,

    indexAt(t: number): number {
      return disposed ? -1 : nearest(ticks.times, t)
    },

    apply(index: number, sink: MotionSink): void {
      if (disposed || index < 0 || index >= ticks.times.length) return
      const at = ticks.times[index]
      for (const track of tracks.values()) {
        const i = nearest(track.times, at)
        if (i < 0) continue
        if (track.kind === 'joint') {
          sink.joint(track.name, track.values[i])
        } else {
          const o = i * 7
          const v = track.values
          sink.pose(track.name, [v[o], v[o + 1], v[o + 2]], [v[o + 3], v[o + 4], v[o + 5], v[o + 6]])
        }
      }
    },

    fetch(t0: number, t1: number): Promise<void> {
      if (disposed) return Promise.resolve()
      asked = [t0, t1]
      return enqueue(() => ensure(t0, t1))
    },

    subscribe(listener: () => void): () => void {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },

    dispose(): void {
      disposed = true
      for (const u of unfollow) u()
      listeners.clear()
      clear()
    },
  }
}
