// ScenePanel (type `scene`): a top-down/side 2D view of "where the thing is right now". It plots one
// column against another (e.g. a quadrotor's x vs altitude z), tracing the path up to the current
// playback time and marking the sample at that time. It is the source-agnostic analog of the costmap's
// robot marker + driven path, for any results table with a time column.
//
// It renders whatever a TimeSeriesSource yields, so it is blind to how the data was captured. Bindings
// (vast visualization.panels):
//   source: { table, time_column }   the time series to read
//   x: <column>  y: <column>         the two columns to plot against each other
//   trail: true|false                draw the path up to t (default true)

import { useEffect, useRef } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { CANVAS, MARK, SERIES } from '@/colors'
import { registerPanel } from '@/lib/panels/registry'
import { useTimeSeries, type TimeSeriesBinding, type TimeSeriesSource } from '@/lib/panels/timeSeries'
import { useCanvasClock, type PanelProps } from '@robovast/panel-kit'

interface Extent {
  minX: number
  maxX: number
  minY: number
  maxY: number
}

function extentOf(source: TimeSeriesSource, xCol: string, yCol: string): Extent | null {
  const rows = source.all()
  if (!rows.length) return null
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (const r of rows) {
    const x = Number(r[xCol])
    const y = Number(r[yCol])
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (y < minY) minY = y
    if (y > maxY) maxY = y
  }
  return Number.isFinite(minX) ? { minX, maxX, minY, maxY } : null
}

function ScenePanel({ spec, clock, data }: PanelProps) {
  const source = (spec.config.source ?? {}) as TimeSeriesBinding
  const xCol = String(spec.config.x ?? 'x')
  const yCol = String(spec.config.y ?? 'y')
  const trail = spec.config.trail !== false

  const query = useTimeSeries(source, data, [xCol, yCol])
  const seriesRef = useRef<TimeSeriesSource | null>(null)
  const extentRef = useRef<Extent | null>(null)

  const { containerRef, canvasRef, requestDraw } = useCanvasClock(clock, (ctx, w, h, t) => {
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.fillStyle = CANVAS
    ctx.fillRect(0, 0, w, h)
    const src = seriesRef.current
    const ext = extentRef.current
    if (!src || !ext) return

    // World->screen: fit the extent with a small margin, y up.
    const pad = 0.08
    const ew = Math.max(ext.maxX - ext.minX, 1e-6)
    const eh = Math.max(ext.maxY - ext.minY, 1e-6)
    const scale = Math.min((w * (1 - 2 * pad)) / ew, (h * (1 - 2 * pad)) / eh)
    const cx = (ext.minX + ext.maxX) / 2
    const cy = (ext.minY + ext.maxY) / 2
    const s = (x: number, y: number): [number, number] => [
      w / 2 + (x - cx) * scale,
      h / 2 - (y - cy) * scale,
    ]

    // Trail up to the current time.
    if (trail) {
      const rows = src.upTo(t)
      ctx.beginPath()
      let started = false
      for (const r of rows) {
        const x = Number(r[xCol])
        const y = Number(r[yCol])
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue
        const [sx, sy] = s(x, y)
        if (!started) {
          ctx.moveTo(sx, sy)
          started = true
        } else ctx.lineTo(sx, sy)
      }
      if (started) {
        ctx.strokeStyle = SERIES[0]
        ctx.lineWidth = 2 * (window.devicePixelRatio || 1)
        ctx.stroke()
      }
    }

    // Marker at the current sample.
    const now = src.at(t)
    if (now) {
      const x = Number(now[xCol])
      const y = Number(now[yCol])
      if (Number.isFinite(x) && Number.isFinite(y)) {
        const [sx, sy] = s(x, y)
        ctx.beginPath()
        ctx.arc(sx, sy, 6 * (window.devicePixelRatio || 1), 0, Math.PI * 2)
        ctx.fillStyle = MARK
        ctx.fill()
        ctx.strokeStyle = 'rgba(0,0,0,0.6)'
        ctx.lineWidth = 1.5 * (window.devicePixelRatio || 1)
        ctx.stroke()
      }
    }
  })

  // Index the loaded series + its extent, then draw.
  useEffect(() => {
    if (!query.data) return
    seriesRef.current = query.data
    extentRef.current = extentOf(query.data, xCol, yCol)
    requestDraw()
  }, [query.data, xCol, yCol, requestDraw])

  if (query.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (query.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(query.error as Error).message}
      </Alert>
    )
  if (!query.data?.all().length)
    return (
      <Alert severity="warning" sx={{ m: 1 }}>
        No rows in <code>{source.table}</code> for this run.
      </Alert>
    )

  return (
    <Box ref={containerRef} sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: CANVAS }}>
      <canvas ref={canvasRef} style={{ display: 'block' }} />
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'scene',
    label: 'Scene',
    defaultPosition: { anchor: 'center', width: 480, height: 400 },
    resizable: true,
    minimizable: true,
  },
  component: ScenePanel,
})

export default ScenePanel
