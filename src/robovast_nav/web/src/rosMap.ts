// Reading a ROS occupancy map — the `map.yaml` + image pair a nav campaign plans on — out of a
// workspace, in the browser.
//
// Not the same thing as the costmap panel's grids: those arrive from a service endpoint already
// decoded into an OccupancyGrid, while this is the *authored* map a campaign points `map_file` at.
// So the fetching and decoding are here; everything downstream (the planar transforms, the
// grayscale ramp) is occupancyGrid.ts's, because a map is a map once it is cells and an origin.

import type { OccupancyGrid } from './occupancyGrid'

/** What a nav `map.yaml` declares, of the keys that decide geometry. */
interface MapYaml {
  image: string
  resolution: number
  origin: number[]
  negate?: number
  occupied_thresh?: number
  free_thresh?: number
}

/** A deliberately small YAML reader: a map.yaml is a flat mapping of scalars and one inline list.
 *
 *  A remote panel bundles what it imports, and pulling a full YAML parser into it to read six keys
 *  would cost more than the panel. Anything this cannot read is reported rather than guessed —
 *  a map drawn at the wrong origin is worse than no map. */
export function parseMapYaml(text: string): MapYaml {
  const out: Record<string, unknown> = {}
  for (const rawLine of text.split('\n')) {
    const line = rawLine.split('#')[0].trim()
    if (!line || !line.includes(':')) continue
    const [key, ...rest] = line.split(':')
    const value = rest.join(':').trim()
    if (!value) continue
    if (value.startsWith('[')) {
      out[key.trim()] = value
        .replace(/[[\]]/g, '')
        .split(',')
        .map((v) => Number(v.trim()))
    } else {
      const n = Number(value)
      out[key.trim()] = Number.isNaN(n) ? value.replace(/^["']|["']$/g, '') : n
    }
  }
  const image = out.image
  const resolution = out.resolution
  if (typeof image !== 'string' || typeof resolution !== 'number') {
    throw new Error('map.yaml declares no usable `image` and `resolution`')
  }
  const origin = Array.isArray(out.origin) ? (out.origin as number[]) : [0, 0, 0]
  return {
    image,
    resolution,
    origin,
    negate: typeof out.negate === 'number' ? out.negate : 0,
    occupied_thresh: typeof out.occupied_thresh === 'number' ? out.occupied_thresh : 0.65,
    free_thresh: typeof out.free_thresh === 'number' ? out.free_thresh : 0.196,
  }
}

/** Decode a binary PGM (P5). Browsers decode PNG and JPEG natively but not this, and a nav map is
 *  almost always a .pgm — so the alternative to ~30 lines here is the map not rendering at all. */
export function decodePgm(buffer: ArrayBuffer): { width: number; height: number; data: Uint8Array } {
  const bytes = new Uint8Array(buffer)
  let offset = 0

  const token = (): string => {
    // Whitespace and full-line comments may appear between any two header fields.
    while (offset < bytes.length) {
      const c = bytes[offset]
      if (c === 0x23) {
        while (offset < bytes.length && bytes[offset] !== 0x0a) offset += 1
      } else if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) {
        offset += 1
      } else break
    }
    const start = offset
    while (offset < bytes.length) {
      const c = bytes[offset]
      if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) break
      offset += 1
    }
    return String.fromCharCode(...bytes.subarray(start, offset))
  }

  const magic = token()
  if (magic !== 'P5') {
    throw new Error(`expected a binary PGM (P5), got ${magic || 'no magic number'}`)
  }
  const width = Number(token())
  const height = Number(token())
  const maxval = Number(token())
  offset += 1 // exactly one whitespace byte separates the header from the raster
  if (!width || !height || !maxval) throw new Error('PGM header is incomplete')
  if (maxval > 255) throw new Error('16-bit PGM maps are not supported')
  return { width, height, data: bytes.subarray(offset, offset + width * height) }
}

/**
 * Fetch a `map.yaml` and its image, and return them as the OccupancyGrid the drawing code takes.
 *
 * The occupancy convention is nav's own: a pixel is scored `(255 - p) / 255` (or `p / 255` when
 * `negate`), then thresholded — above `occupied_thresh` is 100, below `free_thresh` is 0, and the
 * band between is unknown (-1) rather than a made-up cost. The image's first row is the map's
 * TOP, so it is flipped: getting that wrong mirrors the map about its x-axis, which looks like a
 * plausible map and is the wrong one.
 */
export async function loadRosMap(yamlUrl: string): Promise<OccupancyGrid> {
  const yamlText = await (await fetch(yamlUrl)).text()
  const meta = parseMapYaml(yamlText)

  // The image is named relative to the yaml, exactly as a nav map package lays it out.
  const imageUrl = new URL(meta.image, new URL(yamlUrl, window.location.href)).toString()
  const response = await fetch(imageUrl)
  if (!response.ok) {
    throw new Error(`could not read the map image ${meta.image} (${response.status})`)
  }
  const { width, height, data } = decodePgm(await response.arrayBuffer())

  const cells = new Int8Array(width * height)
  for (let row = 0; row < height; row += 1) {
    for (let col = 0; col < width; col += 1) {
      const pixel = data[row * width + col]
      const occupancy = meta.negate ? pixel / 255 : (255 - pixel) / 255
      const value =
        occupancy > (meta.occupied_thresh ?? 0.65)
          ? 100
          : occupancy < (meta.free_thresh ?? 0.196)
            ? 0
            : -1
      cells[(height - 1 - row) * width + col] = value
    }
  }

  return {
    header: { frame_id: 'map' },
    info: {
      resolution: meta.resolution,
      width,
      height,
      origin: {
        position: { x: meta.origin[0] ?? 0, y: meta.origin[1] ?? 0, z: 0 },
        // A map.yaml's third origin element is a yaw in radians, not a quaternion.
        orientation: yawQuat(meta.origin[2] ?? 0),
      },
    },
    data: cells,
  }
}

function yawQuat(yaw: number) {
  return { x: 0, y: 0, z: Math.sin(yaw / 2), w: Math.cos(yaw / 2) }
}
