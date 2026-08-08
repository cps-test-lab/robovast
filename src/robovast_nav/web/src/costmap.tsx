// CostmapPanel: an rviz-style top-down view of what nav2 saw during a run -- the static map, the global
// and local costmaps, the actual path the robot drove, and the robot marker -- all at the current
// playback time. Sources data from the postprocessed run via the injected DataProvider:
//   * costmap grids   -> data.fetchRun('costmap', {topic, t}) (nearest frame; zlib payload inflated in-browser)
//   * frame -> map TF  -> the `poses` table (each recorded frame's pose is already in the map frame)
//   * driven path      -> the base_link trail from `poses` up to the current time
//
// This is the first package-provided run-view panel: it ships with robovast_nav (not the core UI) as a
// Module-Federation remote, because it is the only panel that needs nav2 costmap grids + the occupancy-
// grid helpers. It implements the PanelProps contract from @robovast/panel-kit, so it is time-synced and
// queries the run's data.db exactly like a built-in panel.
//
// It is also the only panel that fetches per clock position rather than preloading its whole series --
// grids are far too large for that, which is why they are served one frame at a time. Everything about
// *when* to fetch and *whether the frame in hand still answers for the cursor* therefore lives in the
// kit's `keyframes` module, shared with the planned live view; what is left here is costmap-specific:
// which topics are layers, how a grid is coloured, and how it is composed into the map frame.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DEFAULT_MIN_INTERVAL_MS,
  frameValidity,
  nearestIndex,
  useCanvasClock,
  useKeyframePump,
  type DataProvider,
  type FrameValidity,
  type PanelProps,
} from '@robovast/panel-kit'
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

// One nav2 OccupancyGrid frame (nearest a requested time), from the service's /costmap endpoint.
// `data` is zlib-compressed, base64-encoded int8 cells (row-major, -1..100).
//
// `t_prev`/`t_next` are the timestamps recorded either side of this frame for the same topic, or null at
// the ends of the topic's span. They are what make the answer interpretable: the panel turns them into
// the interval over which this frame stays the nearest one (so it knows when re-asking could change
// anything) and into a staleness threshold from the local publish period. Non-optional on purpose --
// a read path that forgets them should fail to compile.
interface CostmapFrame {
  t: number
  t_prev: number | null
  t_next: number | null
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
/** One decoded costmap frame on screen. Deliberately one object rather than loose fields on the layer:
 *  the timestamp, the pixels and the validity interval are only ever meaningful together, so there is
 *  no representable state where a grid is drawn without knowing what time it is for. */
interface DrawnFrame {
  /** The frame's own timestamp. The grid is *composed* at this time (see drawGrid), and its distance
   *  from the cursor is what decides whether it is still an honest answer. */
  t: number
  grid: OccupancyGrid
  canvas: HTMLCanvasElement
  /** The TF frame the grid's origin is expressed in, as declared by the message (`odom` for the local
   *  costmap, `map` for the others). Nothing in the .vast states it. */
  frameId: string
  validity: FrameValidity
}

interface LayerRuntime {
  cfg: LayerCfg
  frame?: DrawnFrame
  /** Why this layer can never produce a drawable frame, if so -- reported to the viewer, and it stops
   *  the layer being requested again.
   *
   *  Latched, because a postprocessed run is a closed recording: a topic with no rows cannot acquire
   *  them, and a zero-size grid will not decode on a retry either. Both cases previously left the layer
   *  perpetually out of date, so it was re-requested ~8×/s for the whole session with nothing shown and
   *  nothing said. A live provider must not latch this -- there, "no rows yet" is not "no rows". */
  unavailable?: string
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

/** A frame's poses plus their extracted times, so lookups can binary-search instead of rescanning. */
interface PoseTrack {
  poses: Pose[]
  times: number[]
}

async function loadFramePoses(data: DataProvider, table: string, frame: string): Promise<PoseTrack> {
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
  return { poses, times: poses.map((p) => p.t) }
}

/** Whether the recording holds a frame nearer the cursor than the one this layer is showing — i.e. a
 *  fetch would return something different.
 *
 *  The single predicate behind both "refetch this layer" and "keep showing what we have": outside the
 *  validity window a better frame exists and is being fetched, so the current one is merely *behind*;
 *  inside it, this frame is the best the recording has, so if it is also far from the cursor that is a
 *  real gap and must be reported rather than drawn. Deriving both from one function is what keeps a
 *  layer from being blanked and refetched on contradictory criteria. */
function hasNearerFrame(fr: DrawnFrame, t: number): boolean {
  return t < fr.validity.validFrom || t > fr.validity.validTo
}

/** Nearest pose (by time) for a frame, or null. Called once per layer plus once for the marker on every
 *  animation frame, and each layer now asks at a different time, so this is a binary search rather than
 *  the scan it used to be. */
function poseAt(track: PoseTrack | undefined, t: number): Pose | null {
  if (!track?.poses.length) return null
  const i = nearestIndex(track.times, t)
  return i < 0 ? null : track.poses[i]
}

export default function CostmapPanel({ spec, clock, data }: PanelProps) {
  const { layers, posesTable } = useMemo(() => parseLayers(spec.config), [spec.config])
  const robotFrame = (spec.config.robot_frame as string) ?? 'base_link'

  const viewRef = useRef<View | null>(null)
  const layersRef = useRef<LayerRuntime[]>(layers.map((cfg) => ({ cfg })))
  const posesRef = useRef<Map<string, PoseTrack>>(new Map())
  const poseLoadsRef = useRef<Set<string>>(new Set())
  const posesTableOkRef = useRef<Promise<boolean> | null>(null)

  const [error, setError] = useState<string | null>(null)

  const frameToMap = useCallback((frameId: string, t: number): Planar | null => {
    if (!frameId || frameId === 'map') return IDENTITY_PLANAR
    return poseAt(posesRef.current.get(frameId), t)
  }, [])

  const { containerRef, canvasRef, requestDraw } = useCanvasClock(clock, (ctx, w, h, t) => {
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.fillStyle = '#12171f'
    ctx.fillRect(0, 0, w, h)
    const view = viewRef.current
    if (!view) return
    const w2s = (x: number, y: number): [number, number] => [
      w / 2 + (x - view.cx) * view.ppm,
      h / 2 - (y - view.cy) * view.ppm,
    ]

    // Why a layer is not being drawn, collected while drawing and reported at the bottom of the canvas.
    // Silently skipping a layer reads as "nav2 saw nothing here", which is a different claim.
    const withheld: string[] = []

    const drawGrid = (rt: LayerRuntime) => {
      if (rt.unavailable) {
        withheld.push(`${rt.cfg.name}: ${rt.unavailable}`)
        return
      }
      const fr = rt.frame
      if (!fr) return // nothing fetched yet
      // The nearest recorded frame can be arbitrarily far from the cursor -- before nav2 started
      // publishing, after it stopped, or across a mid-run gap the endpoint still returns the closest
      // row it has. Drawing that as though it were current is what put the local window somewhere the
      // robot no longer was.
      //
      // But only when the recording has nothing nearer (see hasNearerFrame). While scrubbing, a frame
      // leaves its validity window on essentially every cursor change and a replacement is already on
      // its way; blanking during that catch-up strobes the layer instead of reporting anything, and
      // strobes the heaviest layer worst, since the gap is its own round trip. A genuine gap or an
      // off-the-end clamp lands *inside* the validity window -- there the frame never stops being the
      // nearest one, so nothing better is coming and withholding is the honest answer.
      const age = Math.abs(t - fr.t)
      if (age > fr.validity.staleAfter && !hasNearerFrame(fr, t)) {
        withheld.push(`${rt.cfg.name}: nearest frame ${age.toFixed(1)} s away`)
        return
      }
      // Compose the grid at the frame's OWN time, not the cursor's: a local costmap's origin is
      // expressed in `odom` as of when it was published, so resolving odom->map at the cursor would
      // place every obstacle cell at a slightly wrong map coordinate. The cost is that the residual
      // arrow-vs-window offset stays visible, bounded by half the topic's publish period -- that offset
      // is the data's real resolution, and hiding it would be the more expensive lie.
      const f = frameToMap(fr.frameId, fr.t)
      if (!f) return
      const o = gridOrigin(fr.grid)
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
      ctx.drawImage(fr.canvas, 0, 0)
      ctx.restore()
    }

    // map first, then global, then local (local sits on top).
    for (const rt of layersRef.current) if (rt.cfg.name === 'map') drawGrid(rt)
    for (const rt of layersRef.current) if (rt.cfg.name === 'global') drawGrid(rt)
    for (const rt of layersRef.current)
      if (rt.cfg.name !== 'map' && rt.cfg.name !== 'global') drawGrid(rt)

    // Driven path: the base_link trail up to the current time (already in the map frame).
    const track = posesRef.current.get(robotFrame)
    const trail = track?.poses
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
    const robot = poseAt(track, t)
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

    // Say which layers were withheld and why. Drawn on the canvas rather than held in React state
    // because this runs once per animation frame while playing, and a setState here would re-render the
    // component at display rate. Top-left, because the error banner owns the bottom edge -- and it is
    // deliberately not that banner: a layer with no frame near the cursor is a fact about the
    // recording, not a failure, and conflating the two would train the reader to ignore the banner.
    if (withheld.length) {
      const dpr = window.devicePixelRatio || 1 // this canvas is sized in device pixels
      ctx.font = `${12 * dpr}px system-ui, sans-serif`
      ctx.fillStyle = '#9aa4b2'
      ctx.textBaseline = 'top'
      let y = 8 * dpr
      for (const line of withheld) {
        ctx.fillText(line, 8 * dpr, y)
        y += 15 * dpr
      }
    }
  })

  const fitToExtent = useCallback(
    (ext: Extent) => {
      // Device-pixel canvas size, owned by useCanvasClock; fall back to a sane default before the first
      // resize observation so an early fit still produces a usable view.
      const w = canvasRef.current?.width || 600
      const h = canvasRef.current?.height || 400
      const ew = Math.max(ext.maxX - ext.minX, 0.1)
      const eh = Math.max(ext.maxY - ext.minY, 0.1)
      const ppm = Math.max(2, Math.min(w / (ew * 1.1), h / (eh * 1.1)))
      viewRef.current = { cx: (ext.minX + ext.maxX) / 2, cy: (ext.minY + ext.maxY) / 2, ppm }
    },
    [canvasRef],
  )

  // Load one frame's poses, once, on first use. Fire-and-forget: the next redraw picks the result up,
  // and until then the layer needing that frame simply isn't drawn.
  const ensureFrame = useCallback(
    (frameId: string) => {
      if (!frameId || frameId === 'map') return // the map frame is the reference, not a lookup
      if (poseLoadsRef.current.has(frameId)) return
      poseLoadsRef.current.add(frameId)
      posesTableOkRef.current ??= data.has(posesTable)
      void posesTableOkRef.current
        .then((ok) =>
          ok ? loadFramePoses(data, posesTable, frameId) : { poses: [], times: [] as number[] },
        )
        .then((track) => {
          posesRef.current.set(frameId, track)
          const poses = track.poses
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
  // by a fetch round when a grid arrives declaring it. Rebinding to another run/table drops what the
  // previous one loaded -- these are refs, so nothing else clears them.
  useEffect(() => {
    posesRef.current = new Map()
    poseLoadsRef.current = new Set()
    posesTableOkRef.current = null
    ensureFrame(robotFrame)
  }, [ensureFrame, robotFrame])

  // Fetch one layer's nearest frame at `t` and decode it. Mutates `rt` on success.
  const fetchLayer = useCallback(
    async (rt: LayerRuntime, t: number) => {
      const frame = await data.fetchRun<CostmapFrame | null>('costmap', { topic: rt.cfg.topic, t })
      if (!frame) {
        rt.unavailable = `no frames recorded for ${rt.cfg.topic}`
        return
      }
      // `spanOpen: false` -- a postprocessed run is a finished recording, so a frame with no later
      // neighbour really is the last one there will ever be. A live provider over rosbridge would
      // pass true here, and that single flag is the whole difference: with it, the newest frame is
      // re-requested as the cursor advances past it instead of being trusted forever.
      //
      // The refresh floor is this panel's own fetch cadence: nav2 publishes a local costmap far
      // faster than any viewer fetches one (50 Hz is ordinary), and without it every frame would be
      // called stale within 40 ms -- long before it could be replaced.
      const validity = frameValidity(
        { t: frame.t, tPrev: frame.t_prev, tNext: frame.t_next },
        { spanOpen: false, refreshFloorSec: DEFAULT_MIN_INTERVAL_MS / 1000 },
      )
      // Same frame as the one already decoded -- refresh the interval and skip the expensive part.
      // decodeGrid is a per-cell loop over the whole grid, and the static map would otherwise be
      // re-decoded on every round for a picture that cannot have changed.
      if (rt.frame && frame.t === rt.frame.t) {
        rt.frame = { ...rt.frame, validity }
        return
      }
      const grid = frameToGrid(frame)
      const cells = await inflateCells(frame.data)
      grid.data = cells as unknown as number[]
      const canvas = decodeGrid(grid, rt.cfg.color)
      if (!canvas) {
        rt.unavailable = `frame at t=${frame.t.toFixed(2)} s has no extent (${frame.width}x${frame.height})`
        return
      }
      rt.frame = { t: frame.t, grid, canvas, frameId: frame.frame_id, validity }
      // The grid states which frame it is in (the local costmap is in `odom`), and that is the
      // only place that frame is named -- nothing in the .vast declares it -- so this is where
      // its poses get pulled in.
      ensureFrame(frame.frame_id)
      // Auto-fit the view to the first map-frame grid we see.
      if (viewRef.current == null && (frame.frame_id === 'map' || !frame.frame_id))
        fitToExtent(gridExtentInMap(grid, IDENTITY_PLANAR))
    },
    [data, ensureFrame, fitToExtent],
  )

  // One fetch round: bring every layer whose frame no longer answers for `t` up to date.
  //
  // Layers run concurrently AND redraw independently, which matters more than it looks. They differ by
  // more than an order of magnitude in cost -- a local costmap is ~100x100 cells in a sub-KB payload,
  // a full-map global costmap ~600x300 in ~15 KB -- so making the round a barrier would hold the cheap
  // layer the user is actually watching behind the expensive one's decode. The old serial loop did
  // exactly that, and put `local` last. The kit's pump keeps at most one round in flight, so fanning
  // out here cannot reintroduce out-of-order results.
  const fetchAt = useCallback(
    async (t: number) => {
      const due = layersRef.current.filter(
        (rt) => !rt.unavailable && (!rt.frame || hasNearerFrame(rt.frame, t)),
      )
      if (!due.length) return false
      // allSettled, not all: one failing layer must not discard the layers that did arrive, and each
      // draws as soon as it lands rather than at the end of the round.
      const settled = await Promise.allSettled(
        due.map((rt) => fetchLayer(rt, t).then(() => requestDraw())),
      )
      const failed = settled.find((r) => r.status === 'rejected')
      if (failed) setError((failed.reason as Error).message)
      // A failed layer keeps its previous validity, so it is still due and retries on the next clock
      // movement; reporting the round as real work keeps the throttle honest rather than letting a
      // broken endpoint be retried as fast as the clock ticks.
      return true
    },
    [fetchLayer, requestDraw],
  )

  useKeyframePump(clock, { fetchAt })

  // Pan (drag) + zoom (wheel about the cursor). Native listeners so wheel can preventDefault.
  // Canvas sizing, rAF coalescing and the clock subscription are useCanvasClock's; only the
  // costmap-specific interaction is here.
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
      const w = canvas.width
      const h = canvas.height
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
  }, [canvasRef, requestDraw])

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
