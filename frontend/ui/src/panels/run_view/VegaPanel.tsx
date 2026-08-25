// VegaPanel (type `vega`): the general diagram rendering of a run -- an author-supplied Vega-Lite
// spec over one of the run's data.db tables, with a rule marking the current playback time.
//
// Where `timeseries` draws a fixed canvas line chart of columns that already exist, a Vega-Lite spec
// can *derive* what the run never recorded (speed from a pose trail, error between two frames) and
// use any mark, so the two are complements rather than duplicates: `timeseries` stays the cheap path
// for high-rate numeric columns, this one for everything else.
//
// Bindings (vast visualization.panels):
//   source:    { table, time_column?, filter?,   -- the rows; same binding as `timeseries`, so the
//                decimate_hz?, key? }               run scope, the frame filter and the thinning all
//                                                   happen in SQL
//   vega_lite: { ... }                           -- the spec, with no `data` block of its own
//   max_rows:  int                               -- row cap (default 5000, and the service clamps
//                                                   there, so this cannot buy a longer run)
//
// A run that outruns the cap is cut at the HEAD, not sampled: the query is `ORDER BY time LIMIT n`.
// `source.decimate_hz` is therefore the only way to chart a whole long run -- and since the cap is
// the ceiling too, hitting it is worth saying out loud rather than plotting the first minute of a
// ten-minute run as if it were the run.
//
// The spec is bound to two named datasets: `table` (the rows) and `cursor` (a single row `{t}` at the
// playback time). Only the `cursor` dataset changes as the clock runs, so ticking the cursor updates
// that mark instead of re-parsing the spec.

import { useEffect, useMemo, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { CANVAS } from '@/colors'
import { VegaLiteChart } from '@/components/VegaLiteChart'
import { registerPanel } from '@/lib/panels/registry'
import { useTimeSeries, type TimeSeriesBinding } from '@/lib/panels/timeSeries'
import type { DataRow, PanelProps, PlaybackClock } from '@robovast/panel-kit'

const TABLE = 'table'
const CURSOR = 'cursor'
const DEFAULT_TIME_COLUMN = 'timestamp'
// Matches DataProvider.series' own default. Also the service's hard clamp, which is why the
// truncation warning points at `decimate_hz` rather than at a bigger number here.
const DEFAULT_MAX_ROWS = 5000
// The clock ticks at display rate (~60 Hz). A cursor rule does not need that, and each update is a
// Vega dataset change rather than a canvas line, so it is throttled well below it.
const CURSOR_HZ = 18

/** Every data.db column is TEXT, so a `type: quantitative` field would sort lexicographically unless
 *  it is coerced first. Vega-Lite's own `format: {parse: 'auto'}` cannot do it here: that runs in the
 *  *load* step, and these rows are injected into the view as a named dataset instead of being loaded.
 *
 *  So apply the same rule ourselves, per column: if every non-empty value parses as a finite number,
 *  the column is numeric. Deliberately not driven by the spec's declared field types -- a `transform`
 *  can compute over a column the encoding never names (`fold`, `calculate`, `window`), and those
 *  inputs need coercing just as much. A nominal column of numeric-looking strings becoming a number
 *  is harmless: Vega-Lite honours the declared `type: nominal` regardless. */
function coerceNumericColumns(rows: DataRow[]): DataRow[] {
  if (!rows.length) return rows
  const isBlank = (v: unknown) => v == null || v === ''
  const numeric = Object.keys(rows[0]).filter(
    (c) =>
      rows.some((r) => !isBlank(r[c])) &&
      rows.every((r) => isBlank(r[c]) || Number.isFinite(Number(r[c]))),
  )
  if (!numeric.length) return rows
  return rows.map((row) => {
    const out = { ...row }
    for (const c of numeric) if (!isBlank(out[c])) out[c] = Number(out[c])
    return out
  })
}

type Spec = Record<string, unknown>

const CONCAT_KEYS = ['vconcat', 'hconcat', 'concat'] as const

/** Which positional channel a spec binds to the time column, if either does. */
function timeChannel(spec: Spec, timeField: string): 'x' | 'y' | null {
  const enc = spec.encoding as Record<string, { field?: unknown }> | undefined
  if (!enc) return null
  for (const ch of ['x', 'y'] as const) {
    if (enc[ch] && enc[ch].field === timeField) return ch
  }
  return null
}

/** Layer a playback cursor onto one spec, when a time cursor means anything there.
 *
 *  Skipped unless the spec is layerable (`mark`/`layer`) *and* binds the time column to x or y -- a
 *  boxplot by frame has no time axis to mark, and a facet/repeat spec cannot be layered at all (its
 *  child is templated per facet, and the one-row cursor dataset carries no facet field).
 *
 *  Injects by WRAPPING rather than appending to an existing `layer`: a layer child inherits its
 *  parent's shared `encoding`, so an appended rule would pick up that spec's `y` field -- which the
 *  cursor row does not have -- and drop out. Wrapping inherits nothing. `width`/`height` move to the
 *  wrapper with it, since that is the sizing spec once it exists. */
function withCursor(spec: Spec, timeField: string): Spec {
  if (!('mark' in spec) && !('layer' in spec)) return spec
  const ch = timeChannel(spec, timeField)
  if (!ch) return spec
  const enc = spec.encoding as Record<string, { type?: unknown }>
  const { width, height, ...inner } = spec
  const rule = {
    data: { name: CURSOR },
    mark: { type: 'rule', color: 'rgba(255,255,255,0.55)', strokeWidth: 1 },
    encoding: { [ch]: { field: 't', type: enc[ch].type ?? 'quantitative' } },
  }
  return {
    ...(width !== undefined ? { width } : {}),
    ...(height !== undefined ? { height } : {}),
    layer: [inner, rule],
  }
}

/** Apply `withCursor` where it belongs: to each child of a concat spec (so stacked charts sharing a
 *  time axis each get their own rule), else to the spec itself. */
function injectCursor(spec: Spec, timeField: string): Spec {
  for (const key of CONCAT_KEYS) {
    const children = spec[key]
    if (Array.isArray(children)) {
      return { ...spec, [key]: children.map((c) => withCursor(c as Spec, timeField)) }
    }
  }
  return withCursor(spec, timeField)
}

/** The clock's `t`, sampled at CURSOR_HZ instead of at display rate. The trailing flush matters: it
 *  is what leaves the cursor at the exact position a seek or pause ended on. */
function useThrottledTime(clock: PlaybackClock): number {
  const [t, setT] = useState(() => clock.t)
  useEffect(() => {
    const minGap = 1000 / CURSOR_HZ
    let last = 0
    let pending: ReturnType<typeof setTimeout> | null = null
    const flush = () => {
      pending = null
      last = performance.now()
      setT(clock.t)
    }
    const unsubscribe = clock.subscribe(() => {
      const now = performance.now()
      if (now - last >= minGap) flush()
      else if (pending == null) pending = setTimeout(flush, minGap - (now - last))
    })
    return () => {
      unsubscribe()
      if (pending != null) clearTimeout(pending)
    }
  }, [clock])
  return t
}

function VegaPanel({ spec, clock, data }: PanelProps) {
  const source = (spec.config.source ?? {}) as TimeSeriesBinding
  const vegaLite = spec.config.vega_lite as Spec | undefined
  const maxRows = Number(spec.config.max_rows ?? DEFAULT_MAX_ROWS)
  const timeField = source.time_column ?? DEFAULT_TIME_COLUMN
  const t = useThrottledTime(clock)

  const query = useTimeSeries(source, data, undefined, maxRows)
  const rows = useMemo(
    () => (query.data ? coerceNumericColumns(query.data.all()) : []),
    [query.data],
  )
  const chartSpec = useMemo(
    () => (vegaLite ? injectCursor({ data: { name: TABLE }, ...vegaLite }, timeField) : null),
    [vegaLite, timeField],
  )

  if (!vegaLite || !chartSpec)
    return (
      <Alert severity="info" sx={{ m: 1 }}>
        No <code>vega_lite</code> spec configured for this panel.
      </Alert>
    )
  if (query.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (query.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(query.error as Error).message}
      </Alert>
    )
  if (!rows.length)
    return (
      <Alert severity="warning" sx={{ m: 1 }}>
        No rows in <code>{source.table}</code> for this run.
      </Alert>
    )

  return (
    <Box sx={{ width: '100%', height: '100%', overflow: 'auto', bgcolor: CANVAS, p: 0.5 }}>
      {/* A clipped chart read as a complete one is worse than no chart, so say so rather than
          quietly plotting the run's first `maxRows` samples. Read from the query rather than from
          `rows.length >= maxRows`: the row count is a guess that misses exactly this case, because
          the source drops rows whose time does not parse, and it also cries truncation over a table
          that happens to hold exactly `maxRows` rows. */}
      {query.data?.truncated ? (
        <Alert severity="warning" variant="outlined" sx={{ py: 0, mb: 0.5 }}>
          Showing only the first {rows.length} rows of <code>{source.table}</code>
          {/* The service says so when it stopped for a reason other than the row cap, and only it
              knows which -- so quote it rather than name a fix that belongs to the other cause. */}
          {query.data.truncationNote ? (
            ` — ${query.data.truncationNote}`
          ) : source.decimate_hz ? (
            <>
              , even at {source.decimate_hz} Hz — the run ends after them. Lower{' '}
              <code>source.decimate_hz</code> further.
            </>
          ) : (
            <>
              {' '}
              — the rest of the run is not plotted. Set <code>source.decimate_hz</code> to thin the
              whole run instead; the service caps max_rows at 5000, so raising it cannot help.
            </>
          )}
        </Alert>
      ) : null}
      <VegaLiteChart spec={chartSpec} datasets={{ [TABLE]: rows, [CURSOR]: [{ t }] }} />
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'vega',
    label: 'Chart',
    defaultPosition: { anchor: 'bottom-right', width: 480, height: 260 },
    resizable: true,
    minimizable: true,
  },
  component: VegaPanel,
})

export default VegaPanel
