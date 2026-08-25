// The nav-map meaning laid over a flat sidecar: which keys an occupancy grid's metadata declares, and
// what a reader may assume when one is missing.
//
// Split from flatYaml.ts so the two stay reusable apart. The reading is generic — any map format whose
// metadata is a flat mapping goes through `parseFlatYaml` — and only this file knows that `resolution`
// is metres per cell and that `occupied_thresh` has a default. A consumer whose maps declare different
// keys writes its own dozen lines here rather than a second parser.
//
// Kept in the kit rather than beside the panel that draws nav maps because it is pure text -> object
// with no DOM in it, and this is where the host UI's vitest reaches it (frontend/ui/vite.config.ts
// names this directory in `test.include`).

import { numberSequence, parseFlatYaml } from './flatYaml'

/** What a nav `map.yaml` declares, of the keys that decide geometry. */
export interface MapYaml {
  image: string
  resolution: number
  origin: number[]
  negate?: number
  occupied_thresh?: number
  free_thresh?: number
}

/** Read a nav `map.yaml` — the `image` + `resolution` + `origin` triple, and the occupancy thresholds.
 *
 *  `origin` is [x, y] or [x, y, yaw]. Absent means the ROS default of [0, 0, 0]; present but
 *  unreadable throws, which is the whole reason `numberSequence` draws that line. This function used
 *  to accept only the inline spelling and quietly default everything else, so every map a generator
 *  wrote — a block sequence — was drawn at the world origin while its markers stayed in the map frame.
 *  Each pose came out displaced by exactly the origin that had been discarded, and on a symmetric
 *  building that reads as a rotation, which is how it survived. */
export function parseMapYaml(text: string): MapYaml {
  const out = parseFlatYaml(text)
  const image = out.image
  const resolution = out.resolution
  if (typeof image !== 'string' || typeof resolution !== 'number') {
    throw new Error('map.yaml declares no usable `image` and `resolution`')
  }
  return {
    image,
    resolution,
    origin: numberSequence(out.origin, 'origin', [2, 3], [0, 0, 0]),
    negate: typeof out.negate === 'number' ? out.negate : 0,
    occupied_thresh: typeof out.occupied_thresh === 'number' ? out.occupied_thresh : 0.65,
    free_thresh: typeof out.free_thresh === 'number' ? out.free_thresh : 0.196,
  }
}
