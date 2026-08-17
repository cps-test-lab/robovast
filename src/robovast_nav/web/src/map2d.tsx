// Map2DPanel (config-view type `map2d`): the occupancy map a nav campaign plans on, with what the
// selected configuration's variations placed drawn on it.
//
// The direct replacement for the desktop editor's map view — the one custom visualization that
// existed — and it is here rather than in the core UI for the reason the costmap panel is: only a
// nav campaign has a `map.yaml`, and only this package knows how to read one.
//
// It is the *planning* view, and that is why it exists beside the 3D scene rather than being
// replaced by it. A nav variation reasons on this grid: a path is A*-searched over these cells and
// an obstacle is placed relative to that path, so "why did the path go there" is a question about
// this picture and not about the mesh.
//
// Everything below the fetch is occupancyGrid.ts's: the same decode-to-canvas, the same grayscale
// ramp and the same planar transforms the run view's costmap panel draws with.

import { useEffect, useMemo, useRef, useState } from 'react'
import type { ConfigPanelProps, SceneMarker } from '@robovast/panel-kit'
import {
  applyPlanar,
  decodeGrid,
  gridExtentInMap,
  gridOrigin,
  IDENTITY_PLANAR,
  mapColor,
  type OccupancyGrid,
} from './occupancyGrid'
import { loadRosMap } from './rosMap'

/** Which `contribution.files` entry holds the map. Named, so a variation that has none is simply
 *  missing the key rather than having to supply a placeholder. */
const MAP_FILE_ROLE = 'map'

/** Fraction of the panel left as margin when fitting the map. */
const FIT_MARGIN = 0.06

const note: React.CSSProperties = { margin: 8, color: '#b26a00', fontSize: 12, lineHeight: 1.4 }

interface View {
  /** Pixels per metre. */
  scale: number
  /** Map-frame point at the panel's centre. */
  cx: number
  cy: number
}

export default function Map2DPanel({ config, fileUrl }: ConfigPanelProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [grid, setGrid] = useState<OccupancyGrid | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<View | null>(null)
  const drag = useRef<{ x: number; y: number; cx: number; cy: number } | null>(null)

  const mapPath = config.contribution?.files?.[MAP_FILE_ROLE]
  const markers = config.contribution?.markers ?? []

  useEffect(() => {
    if (!mapPath) return
    let cancelled = false
    setError(null)
    loadRosMap(fileUrl(mapPath))
      .then((loaded) => {
        if (cancelled) return
        setGrid(loaded)
        setView(null) // refit: a different map is a different extent
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [mapPath, fileUrl])

  // The map bitmap, decoded once per grid rather than per frame.
  const bitmap = useMemo(() => (grid ? decodeGrid(grid, mapColor) : null), [grid])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !grid || !bitmap) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * devicePixelRatio
    canvas.height = rect.height * devicePixelRatio
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0)
    ctx.clearRect(0, 0, rect.width, rect.height)

    const extent = gridExtentInMap(grid, IDENTITY_PLANAR)
    const current =
      view ??
      (() => {
        const spanX = Math.max(extent.maxX - extent.minX, 1e-6)
        const spanY = Math.max(extent.maxY - extent.minY, 1e-6)
        return {
          scale: Math.min(rect.width / spanX, rect.height / spanY) * (1 - FIT_MARGIN * 2),
          cx: (extent.minX + extent.maxX) / 2,
          cy: (extent.minY + extent.maxY) / 2,
        }
      })()

    // Map metres -> canvas pixels. y is negated: the map frame counts up, the canvas counts down.
    const toPx = (x: number, y: number) => ({
      px: rect.width / 2 + (x - current.cx) * current.scale,
      py: rect.height / 2 - (y - current.cy) * current.scale,
    })

    const o = gridOrigin(grid)
    const corner = applyPlanar(o, 0, o.height * o.res) // the grid's top-left in the map frame
    const at = toPx(corner.x, corner.y)
    ctx.save()
    ctx.translate(at.px, at.py)
    ctx.rotate(-o.yaw)
    ctx.imageSmoothingEnabled = false
    ctx.drawImage(bitmap, 0, 0, o.width * o.res * current.scale, o.height * o.res * current.scale)
    ctx.restore()

    drawMarkers(ctx, markers, toPx, current.scale)
    if (!view) setView(current)
  }, [grid, bitmap, markers, view])

  if (!mapPath) {
    return (
      <div style={note}>
        No map for this configuration. This panel draws the occupancy map a nav variation planned
        on, which reaches it as the <code>map</code> entry of the variation&apos;s contribution —
        a campaign whose factors place nothing has none.
      </div>
    )
  }
  if (error) return <div style={note}>Could not read the map: {error}</div>

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: '100%', display: 'block', cursor: 'grab', background: '#eef1f5' }}
      onWheel={(e) => {
        e.preventDefault()
        setView((v) => (v ? { ...v, scale: v.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15) } : v))
      }}
      onMouseDown={(e) => {
        if (!view) return
        drag.current = { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy }
      }}
      onMouseMove={(e) => {
        const d = drag.current
        if (!d || !view) return
        setView({
          ...view,
          cx: d.cx - (e.clientX - d.x) / view.scale,
          cy: d.cy + (e.clientY - d.y) / view.scale,
        })
      }}
      onMouseUp={() => {
        drag.current = null
      }}
      onMouseLeave={() => {
        drag.current = null
      }}
    />
  )
}

/** Draw the contributed markers top-down. The same marker list the 3D scene draws, projected: a
 *  box becomes its footprint, a pose a dot with a heading tick, a path a polyline. A kind with no
 *  2D meaning is skipped rather than approximated. */
function drawMarkers(
  ctx: CanvasRenderingContext2D,
  markers: SceneMarker[],
  toPx: (x: number, y: number) => { px: number; py: number },
  scale: number,
) {
  for (const marker of markers) {
    const color = marker.color || '#38bdf8'
    ctx.strokeStyle = color
    ctx.fillStyle = color
    ctx.lineWidth = 1.5

    if (marker.kind === 'path' && marker.points?.length) {
      ctx.beginPath()
      marker.points.forEach((p, i) => {
        const { px, py } = toPx(p[0] ?? 0, p[1] ?? 0)
        if (i === 0) ctx.moveTo(px, py)
        else ctx.lineTo(px, py)
      })
      ctx.stroke()
      continue
    }
    if (!marker.pos) continue
    const { px, py } = toPx(marker.pos[0] ?? 0, marker.pos[1] ?? 0)

    switch (marker.kind) {
      case 'box': {
        const [sx = 0.5, sy = 0.5] = marker.size ?? []
        ctx.save()
        ctx.translate(px, py)
        ctx.rotate(-(marker.yaw ?? 0))
        ctx.globalAlpha = 0.45
        ctx.fillRect((-sx / 2) * scale, (-sy / 2) * scale, sx * scale, sy * scale)
        ctx.globalAlpha = 1
        ctx.strokeRect((-sx / 2) * scale, (-sy / 2) * scale, sx * scale, sy * scale)
        ctx.restore()
        break
      }
      case 'cylinder':
      case 'sphere': {
        ctx.beginPath()
        ctx.arc(px, py, Math.max((marker.radius ?? 0.25) * scale, 2), 0, Math.PI * 2)
        ctx.globalAlpha = 0.45
        ctx.fill()
        ctx.globalAlpha = 1
        ctx.stroke()
        break
      }
      case 'pose': {
        ctx.beginPath()
        ctx.arc(px, py, 4, 0, Math.PI * 2)
        ctx.fill()
        if (marker.yaw != null) {
          const len = 14
          ctx.beginPath()
          ctx.moveTo(px, py)
          ctx.lineTo(px + Math.cos(marker.yaw) * len, py - Math.sin(marker.yaw) * len)
          ctx.stroke()
        }
        break
      }
      case 'point': {
        ctx.globalAlpha = 0.5
        ctx.beginPath()
        ctx.arc(px, py, 1.5, 0, Math.PI * 2)
        ctx.fill()
        ctx.globalAlpha = 1
        break
      }
      default:
        break
    }
  }
}
