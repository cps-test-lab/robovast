// The run-view panel contract, as consumed by an external (Module-Federation) panel. These are the
// same shapes the host defines in ui/src/lib/dashboard (PanelProps, PlaybackClock, DataProvider) --
// duplicated here as a minimal type-only surface so this remote can typecheck without depending on
// the host UI package. The real objects are injected by the host at runtime via props, so only the
// signatures a panel uses need to match. Keep in sync with the host's dashboard types.

export interface PlaybackClock {
  /** Current playback time (seconds on the run's timeline). */
  readonly t: number
  /** Subscribe to time changes; returns an unsubscribe fn. */
  subscribe(fn: () => void): () => void
}

export type DataRow = Record<string, unknown>

export interface SeriesOptions {
  timeCol?: string
  t0?: number
  t1?: number
  columns?: string[]
  maxRows?: number
  match?: Record<string, string | number>
}

export interface DataProvider {
  has(table: string, columns?: string[]): Promise<boolean>
  timeRange(table: string, timeCol?: string): Promise<[number, number] | null>
  series(table: string, opts?: SeriesOptions): Promise<DataRow[]>
  nearest(table: string, t: number, timeCol?: string): Promise<DataRow | null>
  /** GET a run-scoped JSON endpoint under the campaign (config_name + run_id applied). The costmap
   *  panel reaches its grids via `fetchRun('costmap', {topic, t})` -- the generic host seam knows
   *  nothing costmap-specific. */
  fetchRun<T>(endpoint: string, params?: Record<string, string | number>): Promise<T>
  runFileUrl(path: string): string
}

export interface PanelSpec {
  type: string
  title?: string
  config: Record<string, unknown>
}

export interface PanelProps {
  spec: PanelSpec
  clock: PlaybackClock
  data: DataProvider
}
