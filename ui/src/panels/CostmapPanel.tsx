// CostmapPanel: an rviz-style top-down view of what nav2 saw during a run -- the static map, the global
// and local costmaps, the actual path the robot drove, and the robot marker -- all at the current
// playback time. It reuses the ported, dependency-free grid/geometry helpers from lib/nav and the
// canvas-draw approach from robosito's nav-map, but sources data from the postprocessed run instead of
// live rosbridge:
//   * costmap grids   -> data.costmapFrame(topic, t) (nearest frame; zlib payload inflated in-browser)
//   * frame -> map TF  -> the `poses` table (each recorded frame's pose is already in the map frame)
//   * driven path      -> the base_link trail from `poses` up to the current time
//
// The high-rate clock drives an imperative redraw (no React re-render); grid frames are re-fetched on a
// throttle as time moves. Report-if-missing: if the costmaps endpoint has no data the panel says so.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Alert from '@mui/material/Alert'
import {
  costmapColor,
  decodeGrid,
  gridExtentInMap,
  gridOrigin,
  IDENTITY_PLANAR,
  mapColor,
  type OccupancyGrid,
  type Planar,
} from '@/lib/nav/occupancyGrid'
import { registerPanel } from '@/lib/dashboard/registry'
import type { PanelProps } from '@/lib/dashboard/types'
import type { DataProvider } from '@/lib/dashboard/dataProvider'
import type { CostmapFrame } from '@/lib/robovastClient'

// The global costmap covers the whole map, so keep it faint enough that the static map shows
// through; the local costmap is a small window drawn on top and stays clearly visible/opaque.
const GLOBAL_ALPHA = 110
const LOCAL_ALPHA = 210
const FETCH_INTERVAL_MS = 120 // min wall gap between costmap-frame fetch rounds

interface View {
  cx: number
  cy: number
  ppm: number
}
interface Extent {
  minX: number
  minY: number
  maxX: number
  maxY: number
}
interface Pose extends Planar {
  t: number
}
interface LayerCfg {
  name: string
  topic: string
  color: (v: number) => [number, number, number, number]
}
interface LayerRuntime {
  cfg: LayerCfg
  grid?: OccupancyGrid
  canvas?: HTMLCanvasElement
  frameId: string
}

function yawQuat(yaw: number) {
  return { x: 0, y: 0, z: Math.sin(yaw / 2), w: Math.cos(yaw / 2) }
}

// Inflate a base64 zlib payload (Python zlib.compress) into the grid's int8 cells.
async function inflateCells(b64: string): Promise<Int8Array> {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'))
  return new Int8Array(await new Response(stream).arrayBuffer())
}

function frameToGrid(frame: CostmapFrame): OccupancyGrid {
  return {
    header: { frame_id: frame.frame_id },
    info: {
      resolution: frame.resolution,
      width: frame.width,
      height: frame.height,
      origin: {
        position: { x: frame.origin_x, y: frame.origin_y, z: 0 },
        orientation: yawQuat(frame.origin_yaw),
      },
    },
    data: [] as unknown as number[], // filled by caller with the inflated Int8Array
  }
}

// Parse the vast layer bindings: costmap layers carry a `topic`; the poses table binding is separate.
function parseLayers(config: Record<string, unknown>): { layers: LayerCfg[]; posesTable: string } {
  const raw = (config.layers ?? {}) as Record<string, { topic?: string; table?: string }>
  const layers: LayerCfg[] = []
  let posesTable = 'poses'
  for (const [name, binding] of Object.entries(raw)) {
    if (name === 'poses') {
      posesTable = binding?.table ?? 'poses'
      continue
    }
    if (!binding?.topic) continue
    const color =
      name === 'map'
        ? mapColor
        : (v: number) => costmapColor(v, name === 'global' ? GLOBAL_ALPHA : LOCAL_ALPHA)
    layers.push({ name, topic: binding.topic, color })
  }
  return { layers, posesTable }
}

async function loadPoses(data: DataProvider, table: string): Promise<Map<string, Pose[]>> {
  const byFrame = new Map<string, Pose[]>()
  if (!(await data.has(table))) return byFrame
  const rows = await data.series(table)
  for (const r of rows) {
    const frame = String(r.frame ?? '')
    if (!frame) continue
    const arr = byFrame.get(frame) ?? []
    arr.push({
      t: Number(r.timestamp),
      x: Number(r['position.x']),
      y: Number(r['position.y']),
      yaw: Number(r['orientation.yaw']),
    })
    byFrame.set(frame, arr)
  }
  for (const arr of byFrame.values()) arr.sort((a, b) => a.t - b.t)
  return byFrame
}

/** Nearest pose (by time) for a frame, or null. */
function poseAt(poses: Pose[] | undefined, t: number): Pose | null {
  if (!poses?.length) return null
  let best = poses[0]
  for (const p of poses) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p
  return best
}

function CostmapPanel({ spec, clock, data }: PanelProps) {
  const { layers, posesTable } = useMemo(() => parseLayers(spec.config), [spec.config])
  const robotFrame = (spec.config.robot_frame as string) ?? 'base_link'

  const containerRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const sizeRef = useRef({ w: 0, h: 0 })
  const viewRef = useRef<View | null>(null)
  const extentRef = useRef<Extent | null>(null)
  const layersRef = useRef<LayerRuntime[]>(layers.map((cfg) => ({ cfg, frameId: 'map' })))
  const posesRef = useRef<Map<string, Pose[]>>(new Map())
  const tRef = useRef(clock.t)
  const rafRef = useRef<number | null>(null)
  const fetchingRef = useRef(false)
  const lastFetchWallRef = useRef(0)

  const [error, setError] = useState<string | null>(null)

  const frameToMap = useCallback((frameId: string, t: number): Planar | null => {
    if (!frameId || frameId === 'map') return IDENTITY_PLANAR
    return poseAt(posesRef.current.get(frameId), t)
  }, [])

  const draw = useCallback(() => {
    rafRef.current = null
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const { w, h } = sizeRef.current
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.fillStyle = '#12171f'
    ctx.fillRect(0, 0, w, h)
    const view = viewRef.current
    if (!view) return
    const t = tRef.current
    const w2s = (x: number, y: number): [number, number] => [
      w / 2 + (x - view.cx) * view.ppm,
      h / 2 - (y - view.cy) * view.ppm,
    ]

    const drawGrid = (rt: LayerRuntime) => {
      if (!rt.canvas) return
      const f = frameToMap(rt.frameId, t)
      if (!f || !rt.grid) return
      const o = gridOrigin(rt.grid)
      ctx.save()
      ctx.translate(w / 2, h / 2)
      ctx.scale(view.ppm, -view.ppm)
      ctx.translate(-view.cx, -view.cy)
      ctx.translate(f.x, f.y)
      ctx.rotate(f.yaw)
      ctx.translate(o.x, o.y)
      ctx.rotate(o.yaw)
      ctx.translate(0, o.height * o.res)
      ctx.scale(o.res, -o.res)
      ctx.imageSmoothingEnabled = false
      ctx.drawImage(rt.canvas, 0, 0)
      ctx.restore()
    }

    // map first, then global, then local (local sits on top).
    for (const rt of layersRef.current) if (rt.cfg.name === 'map') drawGrid(rt)
    for (const rt of layersRef.current) if (rt.cfg.name === 'global') drawGrid(rt)
    for (const rt of layersRef.current)
      if (rt.cfg.name !== 'map' && rt.cfg.name !== 'global') drawGrid(rt)

    // Driven path: the base_link trail up to the current time (already in the map frame).
    const trail = posesRef.current.get(robotFrame)
    if (trail?.length) {
      ctx.beginPath()
      let started = false
      for (const p of trail) {
        if (p.t > t) break
        const [sx, sy] = w2s(p.x, p.y)
        if (!started) {
          ctx.moveTo(sx, sy)
          started = true
        } else ctx.lineTo(sx, sy)
      }
      if (started) {
        ctx.strokeStyle = '#2dd4bf'
        ctx.lineWidth = 2
        ctx.stroke()
      }
    }

    // Robot marker at the current pose.
    const robot = poseAt(trail, t)
    if (robot) {
      const [sx, sy] = w2s(robot.x, robot.y)
      const dx = Math.cos(robot.yaw)
      const dy = -Math.sin(robot.yaw)
      const nx = -dy
      const ny = dx
      const L = 15
      const Wd = 8
      ctx.beginPath()
      ctx.moveTo(sx + dx * L, sy + dy * L)
      ctx.lineTo(sx - dx * L * 0.6 + nx * Wd, sy - dy * L * 0.6 + ny * Wd)
      ctx.lineTo(sx - dx * L * 0.6 - nx * Wd, sy - dy * L * 0.6 - ny * Wd)
      ctx.closePath()
      ctx.fillStyle = '#f0b429'
      ctx.fill()
      ctx.strokeStyle = 'rgba(0,0,0,0.6)'
      ctx.lineWidth = 1.5
      ctx.stroke()
    }
  }, [frameToMap, robotFrame])

  const requestDraw = useCallback(() => {
    if (rafRef.current != null) return
    rafRef.current = requestAnimationFrame(draw)
  }, [draw])

  const fitToExtent = useCallback((ext: Extent) => {
    const { w, h } = sizeRef.current
    const ew = Math.max(ext.maxX - ext.minX, 0.1)
    const eh = Math.max(ext.maxY - ext.minY, 0.1)
    const ppm = Math.max(2, Math.min((w || 600) / (ew * 1.1), (h || 400) / (eh * 1.1)))
    viewRef.current = { cx: (ext.minX + ext.maxX) / 2, cy: (ext.minY + ext.maxY) / 2, ppm }
  }, [])

  // Fetch the nearest grid frame for every costmap layer at time `t` (throttled by the caller).
  const fetchFrames = useCallback(
    async (t: number) => {
      fetchingRef.current = true
      try {
        for (const rt of layersRef.current) {
          const frame = await data.costmapFrame(rt.cfg.topic, t)
          if (!frame) continue
          const grid = frameToGrid(frame)
          const cells = await inflateCells(frame.data)
          grid.data = cells as unknown as number[]
          const canvas = decodeGrid(grid, rt.cfg.color)
          if (!canvas) continue
          rt.grid = grid
          rt.canvas = canvas
          rt.frameId = frame.frame_id
          // Auto-fit the view to the first map-frame grid we see.
          if (viewRef.current == null && (frame.frame_id === 'map' || !frame.frame_id)) {
            const ext = gridExtentInMap(grid, IDENTITY_PLANAR)
            extentRef.current = ext
            fitToExtent(ext)
          }
        }
        requestDraw()
      } catch (e) {
        setError((e as Error).message)
      } finally {
        fetchingRef.current = false
      }
    },
    [data, fitToExtent, requestDraw],
  )

  // Load poses once for the run (frame->map transforms + the driven path).
  useEffect(() => {
    let alive = true
    loadPoses(data, posesTable)
      .then((p) => {
        if (!alive) return
        posesRef.current = p
        // If no costmap ever arrives, still fit to the driven path so the panel isn't blank.
        const trail = p.get(robotFrame)
        if (viewRef.current == null && trail?.length) {
          const xs = trail.map((q) => q.x)
          const ys = trail.map((q) => q.y)
          fitToExtent({
            minX: Math.min(...xs),
            maxX: Math.max(...xs),
            minY: Math.min(...ys),
            maxY: Math.max(...ys),
          })
        }
        requestDraw()
      })
      .catch((e) => setError((e as Error).message))
    return () => {
      alive = false
    }
  }, [data, posesTable, robotFrame, fitToExtent, requestDraw])

  // Follow the clock: redraw every change; re-fetch grid frames on a wall-clock throttle.
  useEffect(() => {
    const onClock = () => {
      tRef.current = clock.t
      requestDraw()
      const now = performance.now()
      if (!fetchingRef.current && now - lastFetchWallRef.current > FETCH_INTERVAL_MS) {
        lastFetchWallRef.current = now
        void fetchFrames(clock.t)
      }
    }
    onClock()
    return clock.subscribe(onClock)
  }, [clock, fetchFrames, requestDraw])

  // Canvas sizing (device pixels for crisp HiDPI).
  useEffect(() => {
    const container = containerRef.current
    const canvas = canvasRef.current
    if (!container || !canvas) return
    const ro = new ResizeObserver(() => {
      const dpr = window.devicePixelRatio || 1
      const cw = container.clientWidth
      const ch = container.clientHeight
      sizeRef.current = { w: Math.round(cw * dpr), h: Math.round(ch * dpr) }
      canvas.width = sizeRef.current.w
      canvas.height = sizeRef.current.h
      canvas.style.width = `${cw}px`
      canvas.style.height = `${ch}px`
      requestDraw()
    })
    ro.observe(container)
    return () => ro.disconnect()
  }, [requestDraw])

  // Pan (drag) + zoom (wheel about the cursor). Native listeners so wheel can preventDefault.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const view = viewRef.current
      if (!view) return
      const dpr = window.devicePixelRatio || 1
      const rect = canvas.getBoundingClientRect()
      const mx = (e.clientX - rect.left) * dpr
      const my = (e.clientY - rect.top) * dpr
      const { w, h } = sizeRef.current
      const wx = view.cx + (mx - w / 2) / view.ppm
      const wy = view.cy - (my - h / 2) / view.ppm
      const ppm = Math.min(5000, Math.max(2, view.ppm * Math.exp(-e.deltaY * 0.0015)))
      viewRef.current = { ppm, cx: wx - (mx - w / 2) / ppm, cy: wy + (my - h / 2) / ppm }
      requestDraw()
    }
    let dragging = false
    let lastX = 0
    let lastY = 0
    const onDown = (e: MouseEvent) => {
      dragging = true
      lastX = e.clientX
      lastY = e.clientY
    }
    const onMove = (e: MouseEvent) => {
      if (!dragging) return
      const view = viewRef.current
      if (!view) return
      const dpr = window.devicePixelRatio || 1
      const dx = (e.clientX - lastX) * dpr
      const dy = (e.clientY - lastY) * dpr
      lastX = e.clientX
      lastY = e.clientY
      viewRef.current = { ...view, cx: view.cx - dx / view.ppm, cy: view.cy + dy / view.ppm }
      requestDraw()
    }
    const onUp = () => {
      dragging = false
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    canvas.addEventListener('mousedown', onDown)
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      canvas.removeEventListener('wheel', onWheel)
      canvas.removeEventListener('mousedown', onDown)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [requestDraw])

  if (layers.length === 0)
    return (
      <Alert severity="info" sx={{ m: 1 }}>
        No costmap layers configured. Add a <code>layers</code> map (each with a <code>topic</code>) to
        this panel in the vast <code>visualization.panels</code>.
      </Alert>
    )

  return (
    <Box ref={containerRef} sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: '#12171f' }}>
      <canvas ref={canvasRef} style={{ display: 'block', cursor: 'grab' }} />
      {error ? (
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, right: 8, py: 0 }}>
          {error}
        </Alert>
      ) : null}
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'costmap',
    label: 'Costmaps',
    defaultPosition: { anchor: 'top-right', width: 420, height: 420 },
    resizable: true,
    minimizable: true,
  },
  component: CostmapPanel,
})

export default CostmapPanel
