// TimeSeriesPanel (type `timeseries`): the diagram rendering of a time series -- the chosen columns
// plotted over the whole run, with a cursor line at the current playback time and a live value-at-t
// readout. It is one of several renderings of the same state-at-time source (the scene and state
// panels are others); it is blind to how the data was captured.
//
// Bindings (vast visualization.panels):
//   source: { table, time_column }
//   series: [ { column, label? }, ... ]

import { useEffect, useMemo, useRef } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { CANVAS, CHART_LABEL, SERIES } from '@/colors'
import { registerPanel } from '@/lib/panels/registry'
import { useTimeSeries, type TimeSeriesBinding, type TimeSeriesSource } from '@/lib/panels/timeSeries'
import { useCanvasClock, useClock, type PanelProps } from '@robovast/panel-kit'

// The shared categorical scale, so a series keeps its colour between here and the eval charts.
const PALETTE = SERIES

interface SeriesCfg {
  column: string
  label: string
  color: string
}

interface Plot {
  t0: number
  t1: number
  yMin: number
  yMax: number
}

function parseSeries(config: Record<string, unknown>): SeriesCfg[] {
  const raw = (config.series ?? []) as { column?: string; label?: string }[]
  return raw
    .filter((s) => s?.column)
    .map((s, i) => ({ column: String(s.column), label: s.label ?? String(s.column), color: PALETTE[i % PALETTE.length] }))
}

function plotBounds(source: TimeSeriesSource, series: SeriesCfg[]): Plot | null {
  const range = source.range()
  if (!range) return null
  let yMin = Infinity
  let yMax = -Infinity
  for (const r of source.all()) {
    for (const s of series) {
      const v = Number(r[s.column])
      if (!Number.isFinite(v)) continue
      if (v < yMin) yMin = v
      if (v > yMax) yMax = v
    }
  }
  if (!Number.isFinite(yMin)) return null
  if (yMin === yMax) {
    yMin -= 1
    yMax += 1
  }
  return { t0: range[0], t1: range[1], yMin, yMax }
}

function TimeSeriesPanel({ spec, clock, data }: PanelProps) {
  const source = (spec.config.source ?? {}) as TimeSeriesBinding
  const series = useMemo(() => parseSeries(spec.config), [spec.config])
  const { t } = useClock(clock) // drives the small readout row; the canvas draws imperatively

  const query = useTimeSeries(source, data, series.map((s) => s.column))
  const seriesRef = useRef<TimeSeriesSource | null>(null)
  const plotRef = useRef<Plot | null>(null)

  const MARGIN = 6 // device px, kept tiny -- the chart fills the panel

  const { containerRef, canvasRef, requestDraw } = useCanvasClock(clock, (ctx, w, h, now) => {
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.fillStyle = CANVAS
    ctx.fillRect(0, 0, w, h)
    const src = seriesRef.current
    const p = plotRef.current
    if (!src || !p) return
    const m = MARGIN * (window.devicePixelRatio || 1)
    const sx = (time: number) => m + ((time - p.t0) / Math.max(p.t1 - p.t0, 1e-6)) * (w - 2 * m)
    const sy = (v: number) => h - m - ((v - p.yMin) / Math.max(p.yMax - p.yMin, 1e-6)) * (h - 2 * m)

    const rows = src.all()
    for (const s of series) {
      ctx.beginPath()
      let started = false
      for (const r of rows) {
        const v = Number(r[s.column])
        if (!Number.isFinite(v)) continue
        const px = sx(src.timeOf(r))
        const py = sy(v)
        if (!started) {
          ctx.moveTo(px, py)
          started = true
        } else ctx.lineTo(px, py)
      }
      ctx.strokeStyle = s.color
      ctx.lineWidth = 1.5 * (window.devicePixelRatio || 1)
      ctx.stroke()
    }

    // Cursor at the current time.
    const cx = sx(now)
    ctx.beginPath()
    ctx.moveTo(cx, m)
    ctx.lineTo(cx, h - m)
    ctx.strokeStyle = 'rgba(255,255,255,0.55)'
    ctx.lineWidth = 1 * (window.devicePixelRatio || 1)
    ctx.stroke()
  })

  useEffect(() => {
    if (!query.data) return
    seriesRef.current = query.data
    plotRef.current = plotBounds(query.data, series)
    requestDraw()
  }, [query.data, series, requestDraw])

  if (query.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (query.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(query.error as Error).message}
      </Alert>
    )
  if (!series.length)
    return (
      <Alert severity="info" sx={{ m: 1 }}>
        No <code>series</code> configured. Add <code>series: [{'{'} column: ... {'}'}]</code> to this panel.
      </Alert>
    )
  if (!query.data?.all().length)
    return (
      <Alert severity="warning" sx={{ m: 1 }}>
        No rows in <code>{source.table}</code> for this run.
      </Alert>
    )

  const at = query.data.at(t)
  const fmt = (v: unknown) => {
    const n = Number(v)
    return Number.isFinite(n) ? n.toFixed(2) : '—'
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', width: '100%', height: '100%', bgcolor: CANVAS }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.5, px: 1, py: 0.5, fontSize: 12 }}>
        {series.map((s) => (
          <Box key={s.column} sx={{ display: 'flex', alignItems: 'center', gap: 0.5, color: CHART_LABEL }}>
            <Box sx={{ width: 10, height: 3, bgcolor: s.color, borderRadius: 1 }} />
            <span>{s.label}</span>
            <b style={{ color: s.color }}>{fmt(at?.[s.column])}</b>
          </Box>
        ))}
      </Box>
      <Box ref={containerRef} sx={{ position: 'relative', flexGrow: 1, minHeight: 0 }}>
        <canvas ref={canvasRef} style={{ display: 'block' }} />
      </Box>
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'timeseries',
    label: 'Time series',
    defaultPosition: { anchor: 'bottom-right', width: 460, height: 240 },
    resizable: true,
    minimizable: true,
  },
  component: TimeSeriesPanel,
})

export default TimeSeriesPanel
