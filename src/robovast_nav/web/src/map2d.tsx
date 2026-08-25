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
//
// Bindings (vast visualization.config.panels), both optional and both declared by this panel's
// CONFIG_CLASS (robovast_nav.panels:Map2DBindings) -- so a misspelled one is a validation error that
// names the fields, rather than an empty panel:
//   map: files/depot.yaml           # a literal path...
//   map: {param: map_file}          # ...or the parameter holding it, or {internal: _map_file},
//                                   # or {role: map} for what a variation contributed
//   markers:
//     - {kind: pose, pos: [0, 0], yaw: 0, label: start}
//     - {kind: pose, param: goal_pose, label: goal}
//     - {kind: path, internal: _path, label: planned path}
//
// Every field reads through the kit's one resolver, so where a value comes from is not this panel's
// concern. The markers are drawn in the MAP frame -- this panel *is* the map -- so a map-frame
// parameter needs no `offset:` here, where the world-frame 3D scene does.

import { useEffect, useMemo, useRef, useState } from 'react'
import { declaredMarkers, resolveStringBinding, type ConfigPanelProps, type SceneMarker }
  from '@robovast/panel-kit'
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

/** Candidate scale-bar lengths in metres: a 1/2/5 sequence, so the bar is always a round number. */
const SCALE_STEPS = [0.5, 1, 2, 5, 10, 20, 50, 100, 200]

/** The panel's own ink and a halo that contrasts with it.
 *
 *  The canvas is transparent — what surrounds the map is the panel, so the annotations have to be
 *  legible against whatever the host's theme paints there. Taken from the element's computed `color`
 *  rather than a constant: a remote panel shares only react with the host and cannot read its theme,
 *  but the inherited text colour is that theme, already resolved. A fixed slate-on-white pair read
 *  as invisible the moment the host went dark. */
function inkFor(el: HTMLElement): { ink: string; halo: string } {
  const ink = getComputedStyle(el).color || '#334155'
  const [r = 0, g = 0, b = 0] = (ink.match(/[\d.]+/g) ?? []).map(Number)
  // Rec. 601 luma: light ink means a dark surface behind it, so the halo goes the other way.
  const light = (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5
  return { ink, halo: light ? 'rgba(0, 0, 0, 0.75)' : 'rgba(255, 255, 255, 0.85)' }
}

const note: React.CSSProperties = { margin: 8, color: '#b26a00', fontSize: 12, lineHeight: 1.4 }

interface View {
  /** Pixels per metre. */
  scale: number
  /** Map-frame point at the panel's centre. */
  cx: number
  cy: number
}

export default function Map2DPanel({ spec, config, fileUrl }: ConfigPanelProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [grid, setGrid] = useState<OccupancyGrid | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<View | null>(null)
  const drag = useRef<{ x: number; y: number; cx: number; cy: number } | null>(null)

  // A declared `map:` wins over a contributed one: it is the campaign author naming a file, where the
  // contribution is what a variation happened to generate. A campaign whose only factor is a plain
  // parameter list has no variation to contribute anything and would otherwise have no map at all,
  // though its map is sitting right there in the project.
  //
  // Resolved through the kit rather than read as a string, so the binding grammar is the same one
  // every panel field uses: `map: files/depot.yaml` and `map: {param: map_file}` both arrive here
  // without this panel knowing the difference.
  const declaredMap = resolveStringBinding(spec.config?.map, config)
  const mapPath = declaredMap || config.contribution?.files?.[MAP_FILE_ROLE]

  // Contributed plus declared, concatenated rather than one overriding the other -- the same rule
  // (and the same resolver) the 3D scene panel uses.
  const markers = useMemo<SceneMarker[]>(
    () => [...(config.contribution?.markers ?? []), ...declaredMarkers(spec.config, config)],
    [config, spec.config],
  )

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

    const { ink, halo } = inkFor(canvas)
    drawMarkers(ctx, markers, toPx, current.scale, halo)
    drawScaleBar(ctx, rect.width, rect.height, current.scale, ink, halo)
    if (!view) setView(current)
  }, [grid, bitmap, markers, view])

  if (!mapPath) {
    return (
      <div style={note}>
        No map for this configuration. A map reaches this panel one of two ways: as the{' '}
        <code>map</code> entry of a variation&apos;s contribution — a campaign whose factors place
        nothing has none — or declared on the panel itself, which is how a campaign points at a map
        that is checked in:
        <pre style={{ margin: '6px 0 0' }}>
          {'- map2d:\n    map: files/depot.yaml'}
        </pre>
      </div>
    )
  }
  if (error) return <div style={note}>Could not read the map: {error}</div>

  return (
    <canvas
      ref={canvasRef}
      // No background: the map's unknown cells are transparent (mapColor) and so is everything
      // around the grid, so the panel's own surface shows through instead of a light slab of ours
      // sitting in a dark theme.
      style={{ width: '100%', height: '100%', display: 'block', cursor: 'grab' }}
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
      // Back to fit. Zooming into a corner is easy to do and, without this, only a remount undoes.
      onDoubleClick={() => setView(null)}
      title="drag to pan, wheel to zoom, double-click to fit"
    />
  )
}

/** Draw the markers top-down. The same marker list the 3D scene draws, projected: a box becomes its
 *  footprint, a pose a dot with a heading tick, a path a polyline. A kind with no 2D meaning is
 *  skipped rather than approximated.
 *
 *  Labels are drawn here and nowhere else so far: `label` is part of the marker contract, and two
 *  pose dots distinguished only by colour do not say which is the start. */
function drawMarkers(
  ctx: CanvasRenderingContext2D,
  markers: SceneMarker[],
  toPx: (x: number, y: number) => { px: number; py: number },
  scale: number,
  halo: string,
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

    // `point` is left unlabelled on purpose: a rasterized path contributes hundreds of them, all
    // named the same, and the labels would bury the map they are drawn on.
    if (marker.label && marker.kind !== 'point') {
      drawLabel(ctx, marker.label, px + 7, py - 6, color, halo)
    }
  }
}

/** Small text with a halo, so it stays readable over free cells (near-white), occupied ones
 *  (near-black) and the bare panel alike -- a marker sits on all three, and picking one text colour
 *  loses the others. */
function drawLabel(ctx: CanvasRenderingContext2D, text: string, px: number, py: number,
                   color: string, halo: string) {
  ctx.save()
  ctx.font = '11px system-ui, -apple-system, sans-serif'
  ctx.textBaseline = 'middle'
  ctx.lineWidth = 3
  ctx.strokeStyle = halo
  ctx.strokeText(text, px, py)
  ctx.fillStyle = color
  ctx.fillText(text, px, py)
  ctx.restore()
}

/** A round-number scale bar, bottom-left. This is a metric top-down view of a place a robot drives
 *  through, so "how far is that" is the question it is asked most; the alternative is a reader
 *  counting grid cells they cannot see. */
function drawScaleBar(ctx: CanvasRenderingContext2D, width: number, height: number, scale: number,
                      ink: string, halo: string) {
  // The largest round length that fits a quarter of the panel, so the bar stays a comparable size
  // as the view zooms rather than growing off the edge.
  const budget = width * 0.25
  const metres = [...SCALE_STEPS].reverse().find((m) => m * scale <= budget) ?? SCALE_STEPS[0]
  const length = metres * scale
  const x = 10
  const y = height - 12

  ctx.save()
  // The halo first, as a slightly fatter line under the bar: the bar crosses both the map and the
  // bare panel, so it needs the same two-tone treatment the labels get.
  ctx.lineWidth = 4
  ctx.strokeStyle = halo
  ctx.beginPath()
  ctx.moveTo(x, y)
  ctx.lineTo(x + length, y)
  ctx.stroke()
  ctx.lineWidth = 1.5
  ctx.strokeStyle = ink
  ctx.beginPath()
  ctx.moveTo(x, y - 4)
  ctx.lineTo(x, y)
  ctx.lineTo(x + length, y)
  ctx.lineTo(x + length, y - 4)
  ctx.stroke()
  drawLabel(ctx, `${metres} m · map frame`, x + length + 6, y - 1, ink, halo)
  ctx.restore()
}
