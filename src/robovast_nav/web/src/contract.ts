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
  /** Thin the result to one row per 1/`hz` second (per distinct `key` value when given). Requires
   *  `columns`: the SQL names them because the time column becomes an aggregate. A viewer needs far
   *  less resolution than a bag records, and the service caps a query at 5000 rows -- so for any
   *  table that is a *recording* rather than a summary, this is what keeps the tail of a run from
   *  being cut off. */
  decimate?: { hz: number; key?: string }
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

/** Built-in panels the host offers a package panel, so one can be *derived* rather than
 *  reimplemented -- this package's nav2 behavior tree is the scenario tree with a different
 *  table, title and empty-state hint. They arrive as props because a remote shares only
 *  react/react-dom with the host and cannot import its modules. Optional: a host predating
 *  this must leave the panel degrading, not crashing. */
export interface PanelBuiltins {
  ScenarioTree?: (props: PanelProps) => JSX.Element | null
}

export interface PanelProps {
  spec: PanelSpec
  clock: PlaybackClock
  data: DataProvider
  builtins?: PanelBuiltins
}
