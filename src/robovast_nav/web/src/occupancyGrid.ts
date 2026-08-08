// Rendering helpers for the costmap panel: decode a nav_msgs/OccupancyGrid into an offscreen canvas
// (rviz "map" grayscale or "costmap" gradient color schemes) and the planar geometry needed to place a
// grid or a robot marker into the shared `map` frame. Pure functions only -- the canvas orchestration
// (view transform, compositing, interaction) lives in the CostmapPanel. No `three` dependency:
// everything is 2D (ground-plane) so a small planar transform (x, y, yaw) suffices.
//
// These are generic OccupancyGrid rendering helpers: whether the grid arrives live over rosbridge or,
// as here, is rebuilt into the same OccupancyGrid shape from a data.db row by the CostmapPanel, the
// functions are identical.

// --- ROS message shapes (only the fields we read) ---

export interface Point {
  x: number
  y: number
  z: number
}

export interface Quaternion {
  x: number
  y: number
  z: number
  w: number
}

export interface OccupancyGrid {
  header: { frame_id: string }
  info: {
    resolution: number // meters per cell
    width: number // cells
    height: number // cells
    origin: { position: Point; orientation: Quaternion } // pose of cell (0,0)'s corner in header.frame_id
  }
  data: number[] // row-major, data[y*width + x]; y=0 is the bottom row
}

// --- planar geometry ---

/** A planar (2D) rigid transform: translation + yaw (radians). */
export interface Planar {
  x: number
  y: number
  yaw: number
}

/** Identity transform (a frame that coincides with its reference, e.g. a grid already in `map`). */
export const IDENTITY_PLANAR: Planar = { x: 0, y: 0, yaw: 0 }

/** Yaw (rotation about z) of a quaternion, in radians. */
export function quatYaw(q: Quaternion): number {
  const siny = 2 * (q.w * q.z + q.x * q.y)
  const cosy = 1 - 2 * (q.y * q.y + q.z * q.z)
  return Math.atan2(siny, cosy)
}

/** Transform a point through a planar transform: `t` applied to local point (x, y). */
export function applyPlanar(t: Planar, x: number, y: number): { x: number; y: number } {
  const c = Math.cos(t.yaw)
  const s = Math.sin(t.yaw)
  return { x: t.x + c * x - s * y, y: t.y + s * x + c * y }
}

/** Compose two planar transforms: the result applies `inner` first, then `outer` (outer ∘ inner). */
export function composePlanar(outer: Planar, inner: Planar): Planar {
  const p = applyPlanar(outer, inner.x, inner.y)
  return { x: p.x, y: p.y, yaw: outer.yaw + inner.yaw }
}

/** The grid origin pose reduced to planar form, plus its cell size and dimensions in cells. */
export function gridOrigin(
  grid: OccupancyGrid,
): Planar & { res: number; width: number; height: number } {
  const o = grid.info.origin
  return {
    x: o.position.x,
    y: o.position.y,
    yaw: quatYaw(o.orientation),
    res: grid.info.resolution,
    width: grid.info.width,
    height: grid.info.height,
  }
}

// --- color schemes (return [r, g, b, a], 0..255) ---

/** rviz "map" grayscale: unknown (-1) transparent, free (0) light, occupied (100) dark. */
export function mapColor(v: number): [number, number, number, number] {
  if (v < 0) return [0, 0, 0, 0] // unknown -> show the background through
  const shade = Math.round(255 * (1 - Math.min(v, 100) / 100))
  return [shade, shade, shade, 255]
}

/** rviz "costmap" gradient over nav2's translated 0..100 (+ -1) values, semi-transparent:
 *  free (0) / unknown (-1) transparent; 1..98 blue->red; 99 inscribed (cyan); 100 lethal (purple).
 *  `alpha` (0..255) sets the overlay opacity (global costmaps use a higher value than local). */
export function costmapColor(v: number, alpha = 150): [number, number, number, number] {
  if (v <= 0) return [0, 0, 0, 0] // free (0) or unknown (-1)
  const c = Math.min(v, 100)
  if (c >= 100) return [255, 0, 255, alpha] // lethal
  if (c >= 99) return [0, 255, 255, alpha] // inscribed
  const r = Math.round((c * 255) / 100)
  return [r, 0, 255 - r, alpha]
}

/** Every colour a scheme can produce, as a flat RGBA lookup indexed by the cell's raw int8 byte.
 *
 *  A cell value is one byte, so a scheme has at most 256 possible outputs however it is written —
 *  calling it per cell instead means allocating a fresh 4-element array for every cell, which on a
 *  full-map grid (600x300 is ordinary) is ~185k short-lived arrays per decode and dominates the cost
 *  of showing a frame. Built once per decode; the scheme stays an ordinary function. */
function palette(color: (v: number) => [number, number, number, number]): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 4)
  for (let i = 0; i < 256; i++) {
    const [r, g, b, a] = color(i < 128 ? i : i - 256) // int8: 0..127 then -128..-1
    lut[i * 4] = r
    lut[i * 4 + 1] = g
    lut[i * 4 + 2] = b
    lut[i * 4 + 3] = a
  }
  return lut
}

/** Decode a grid to an offscreen canvas sized width×height cells. Pixel row 0 is the grid's TOP row
 *  (grid y counts up from the bottom), so the caller places it with a matching y-flip. Returns null if
 *  a 2D context can't be obtained. */
export function decodeGrid(
  grid: OccupancyGrid,
  color: (v: number) => [number, number, number, number],
): HTMLCanvasElement | null {
  const { width, height } = grid.info
  if (!width || !height) return null
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  const img = ctx.createImageData(width, height)
  const out = img.data
  const lut = palette(color)
  const data = grid.data
  for (let row = 0; row < height; row++) {
    const srcRow = height - 1 - row // image row 0 == grid's top row
    for (let col = 0; col < width; col++) {
      const v = data[srcRow * width + col]
      // `& 0xff` is the int8 -> LUT index the palette was built against. A short/absent payload keeps
      // the old meaning: treat a missing cell as unknown (-1), i.e. index 255.
      const li = (v === undefined ? 255 : v & 0xff) * 4
      const di = (row * width + col) * 4
      out[di] = lut[li]
      out[di + 1] = lut[li + 1]
      out[di + 2] = lut[li + 2]
      out[di + 3] = lut[li + 3]
    }
  }
  ctx.putImageData(img, 0, 0)
  return canvas
}

/** Axis-aligned extent (in the map frame) covered by a grid, given its frame->map planar transform.
 *  Used to auto-fit the view to the occupancy map on first load. */
export function gridExtentInMap(
  grid: OccupancyGrid,
  frameToMap: Planar,
): { minX: number; minY: number; maxX: number; maxY: number } {
  const o = gridOrigin(grid)
  const w = o.width * o.res
  const h = o.height * o.res
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const [lx, ly] of [
    [0, 0],
    [w, 0],
    [0, h],
    [w, h],
  ]) {
    const inFrame = applyPlanar(o, lx, ly) // grid-local -> grid frame
    const inMap = applyPlanar(frameToMap, inFrame.x, inFrame.y) // grid frame -> map
    minX = Math.min(minX, inMap.x)
    minY = Math.min(minY, inMap.y)
    maxX = Math.max(maxX, inMap.x)
    maxY = Math.max(maxY, inMap.y)
  }
  return { minX, minY, maxX, maxY }
}
