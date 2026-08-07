// CostmapPanel: an rviz-style top-down view of what nav2 saw during a run -- the static map, the global
// and local costmaps, the actual path the robot drove, and the robot marker -- all at the current
// playback time. Sources data from the postprocessed run via the injected DataProvider:
//   * costmap grids   -> data.fetchRun('costmap', {topic, t}) (nearest frame; zlib payload inflated in-browser)
//   * frame -> map TF  -> the `poses` table (each recorded frame's pose is already in the map frame)
//   * driven path      -> the base_link trail from `poses` up to the current time
//
// This is the first package-provided run-view panel: it ships with robovast_nav (not the core UI) as a
// Module-Federation remote, because it is the only panel that needs nav2 costmap grids + the occupancy-
// grid helpers. It implements the host's PanelProps contract, so it is time-synced and queries the run's
// data.db exactly like a built-in panel. Relocated verbatim from ui/src/panels/CostmapPanel.tsx, with
// `data.costmapFrame(...)` replaced by the generic `data.fetchRun('costmap', ...)` seam.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  costmapColor,
  decodeGrid,
  gridExtentInMap,
  gridOrigin,
  IDENTITY_PLANAR,
  mapColor,
  type OccupancyGrid,
  type Planar,
} from './occupancyGrid'
import type { DataProvider, PanelProps } from './contract'

// One nav2 OccupancyGrid frame (nearest a requested time), from the service's /costmap endpoint.
// `data` is zlib-compressed, base64-encoded int8 cells (row-major, -1..100).
interface CostmapFrame {
  t: number
  frame_id: string
  resolution: number
  width: number
  height: number
  origin_x: number
  origin_y: number
  origin_yaw: number
  data: string
}

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

// The service clamps any data.db query at 5000 rows and cuts by TIME, so a query that asks for more
// than that silently loses the end of the run rather than failing. `poses` is a recording, not a
// summary: with `rosbags_tf_to_csv: {frames: all}` it holds every TF frame that resolves against map
// -- a TB4 nav run carries base_link, odom, both wheels and the ground-truth frame at ~50 Hz, which
// is 10.7k rows over 100 s. Reading the whole table in one query therefore returned poses up to t=53 s
// and nothing after: the driven path and the robot marker froze half way while the costmap layers,
// fetched per-time from their own endpoint, kept following the robot to the end.
//
// Two things keep that from recurring, and both are needed. One query PER FRAME, issued only for the
// frames this panel actually draws (the robot frame, plus whatever frame a costmap grid turns out to
// be in), so the budget no longer divides by however many frames the world happens to publish -- that
// count is unbounded, since `all` grows with every walker bone and movable prop a scene gains. And
// decimation, so one frame's own length cannot reach the cap either: 20 Hz is well past what scrubbing
// a trail can show and buys 250 s of run before the cap is anywhere near.
const POSE_COLUMNS = ['timestamp', 'position.x', 'position.y', 'orientation.yaw']
const POSE_HZ = 20
const POSE_MAX_ROWS = 5000 // the service's hard cap; asking for more does not raise it

async function loadFramePoses(data: DataProvider, table: string, frame: string): Promise<Pose[]> {
  const rows = await data.series(table, {
    match: { frame },
    columns: POSE_COLUMNS,
    decimate: { hz: POSE_HZ },
    maxRows: POSE_MAX_ROWS,
  })
  const poses = rows.map((r) => ({
    t: Number(r.timestamp),
    x: Number(r['position.x']),
    y: Number(r['position.y']),
    yaw: Number(r['orientation.yaw']),
  }))
  poses.sort((a, b) => a.t - b.t)
  return poses
}

/** Nearest pose (by time) for a frame, or null. */
function poseAt(poses: Pose[] | undefined, t: number): Pose | null {
  if (!poses?.length) return null
  let best = poses[0]
  for (const p of poses) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p
  return best
}

export default function CostmapPanel({ spec, clock, data }: PanelProps) {
  const { layers, posesTable } = useMemo(() => parseLayers(spec.config), [spec.config])
  const robotFrame = (spec.config.robot_frame as string) ?? 'base_link'

  const containerRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const sizeRef = useRef({ w: 0, h: 0 })
  const viewRef = useRef<View | null>(null)
  const extentRef = useRef<Extent | null>(null)
  const layersRef = useRef<LayerRuntime[]>(layers.map((cfg) => ({ cfg, frameId: 'map' })))
  const posesRef = useRef<Map<string, Pose[]>>(new Map())
  const poseLoadsRef = useRef<Set<string>>(new Set())
  const posesTableOkRef = useRef<Promise<boolean> | null>(null)
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

  // Load one frame's poses, once, on first use. Fire-and-forget: the next redraw picks the result up,
  // and until then the layer needing that frame simply isn't drawn.
  const ensureFrame = useCallback(
    (frameId: string) => {
      if (!frameId || frameId === 'map') return // the map frame is the reference, not a lookup
      if (poseLoadsRef.current.has(frameId)) return
      poseLoadsRef.current.add(frameId)
      posesTableOkRef.current ??= data.has(posesTable)
      void posesTableOkRef.current
        .then((ok) => (ok ? loadFramePoses(data, posesTable, frameId) : []))
        .then((poses) => {
          posesRef.current.set(frameId, poses)
          // Hitting the cap even decimated means the run outgrew POSE_HZ: say so, because the symptom
          // is a path that just stops, which reads as the robot having stopped.
          if (poses.length >= POSE_MAX_ROWS)
            setError(
              `${posesTable}['${frameId}'] hit the ${POSE_MAX_ROWS}-row query cap at ${POSE_HZ} Hz, ` +
                `so the trail and robot marker end before the run does. Lower POSE_HZ.`,
            )
          // If no costmap ever arrives, still fit to the driven path so the panel isn't blank.
          if (viewRef.current == null && frameId === robotFrame && poses.length) {
            const xs = poses.map((q) => q.x)
            const ys = poses.map((q) => q.y)
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
    },
    [data, posesTable, robotFrame, fitToExtent, requestDraw],
  )

  // The robot frame is needed unconditionally (driven path + marker); every other frame is pulled in
  // by fetchFrames when a grid arrives declaring it. Rebinding to another run/table drops what the
  // previous one loaded -- these are refs, so nothing else clears them.
  useEffect(() => {
    posesRef.current = new Map()
    poseLoadsRef.current = new Set()
    posesTableOkRef.current = null
    ensureFrame(robotFrame)
  }, [ensureFrame, robotFrame])

  // Fetch the nearest grid frame for every costmap layer at time `t` (throttled by the caller).
  const fetchFrames = useCallback(
    async (t: number) => {
      fetchingRef.current = true
      try {
        for (const rt of layersRef.current) {
          const frame = await data.fetchRun<CostmapFrame | null>('costmap', { topic: rt.cfg.topic, t })
          if (!frame) continue
          const grid = frameToGrid(frame)
          const cells = await inflateCells(frame.data)
          grid.data = cells as unknown as number[]
          const canvas = decodeGrid(grid, rt.cfg.color)
          if (!canvas) continue
          rt.grid = grid
          rt.canvas = canvas
          rt.frameId = frame.frame_id
          // The grid states which frame it is in (the local costmap is in `odom`), and that is the
          // only place that frame is named -- nothing in the .vast declares it -- so this is where
          // its poses get pulled in.
          ensureFrame(frame.frame_id)
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
    [data, ensureFrame, fitToExtent, requestDraw],
  )

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
      <div style={{ padding: 12, fontSize: 13, color: '#9aa4b2' }}>
        No costmap layers configured. Add a <code>layers</code> map (each with a <code>topic</code>) to
        this panel in the vast <code>visualization.panels</code>.
      </div>
    )

  return (
    <div
      ref={containerRef}
      style={{ position: 'relative', width: '100%', height: '100%', background: '#12171f' }}
    >
      <canvas ref={canvasRef} style={{ display: 'block', cursor: 'grab' }} />
      {error ? (
        <div
          style={{
            position: 'absolute',
            bottom: 8,
            left: 8,
            right: 8,
            padding: '4px 8px',
            fontSize: 12,
            color: '#fff',
            background: 'rgba(180,83,9,0.9)',
            borderRadius: 4,
          }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}
