// Shared-candidate code: this file imports only 'three' -- keep it free of robovast imports (see
// README.md in this directory).
//
// The cameras a scene descriptor places on its bodies: a sensor's mount and projection, so a viewer
// renders the scene from where the sensor looked instead of shipping the sensor's images.
//
// Each entry is a glTF perspective camera on a body (docs/simulators.rst, the scene descriptor):
//
//   { name, body, pos: [x, y, z], quat: [w, x, y, z],
//     perspective: { yfov, aspectRatio?, znear, zfar? }, kind?, width?, height? }
//
// The camera looks down its local -Z with +Y up, which is three's camera convention too, so the entry
// becomes a PerspectiveCamera parented to its body's node with nothing converted. As a child of that
// node it follows the body as the motion source moves it, and the renderer keeps its view matrix
// current with the rest of the graph.

import { MathUtils, PerspectiveCamera, Quaternion } from 'three'

/** One `cameras` entry as the descriptor states it. */
export interface SceneCameraEntry {
  name: string
  body: number
  pos: [number, number, number]
  quat: [number, number, number, number] // wxyz
  perspective: { yfov: number; aspectRatio?: number; znear: number; zfar?: number }
  /** What the sensor's image holds: `rgb` or `depth`. Absent means a plain view, read as `rgb`. */
  kind?: string
  width?: number
  height?: number
}

/** A camera of a loaded scene. */
export interface SceneCamera {
  name: string
  kind: string
  /** The published image's size, when the descriptor states it. */
  width?: number
  height?: number
  /** Width over height of the picture: the image's when stated, else the projection's. `undefined`
   *  when the descriptor states neither, and the picture then takes the canvas's shape. */
  aspect?: number
  /** The projection's own aspect ratio, when stated. */
  projectionAspect?: number
  camera: PerspectiveCamera
}

/** How far a camera with no `zfar` draws, as a multiple of its `znear`. glTF reads an absent `zfar`
 *  as an infinite projection, which a PerspectiveCamera cannot hold; this keeps the depth buffer's
 *  ratio where a finite one would put it. */
export const INFINITE_FAR_RATIO = 1e5

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const isVec = (v: unknown, n: number): boolean =>
  Array.isArray(v) && v.length === n && v.every(isNum)

/**
 * The descriptor's `cameras`, checked. An absent list is no cameras; an entry that cannot be placed
 * is refused naming it, rather than being drawn from somewhere it is not.
 */
export function parseSceneCameras(raw: unknown, bodyCount: number, url: string): SceneCameraEntry[] {
  if (raw === undefined) return []
  if (!Array.isArray(raw)) throw new Error(`${url}: \`cameras\` is not a list.`)
  return raw.map((entry: unknown, i) => {
    const e = (entry ?? {}) as Record<string, unknown>
    const where = `${url}: cameras[${i}]${typeof e.name === 'string' ? ` (${e.name})` : ''}`
    const p = (e.perspective ?? {}) as Record<string, unknown>
    if (typeof e.name !== 'string' || !e.name) throw new Error(`${where} has no name.`)
    if (!Number.isInteger(e.body) || (e.body as number) < 0 || (e.body as number) >= bodyCount)
      throw new Error(`${where}: body ${JSON.stringify(e.body)} is not one of the scene's bodies.`)
    if (!isVec(e.pos, 3) || !isVec(e.quat, 4))
      throw new Error(`${where} needs pos [x, y, z] and quat [w, x, y, z].`)
    if (!isNum(p.yfov) || p.yfov <= 0 || p.yfov >= Math.PI || !isNum(p.znear) || p.znear <= 0)
      throw new Error(`${where} needs perspective.yfov in (0, pi) and a positive perspective.znear.`)
    if (p.zfar !== undefined && !(isNum(p.zfar) && p.zfar > p.znear))
      throw new Error(`${where}: perspective.zfar must exceed znear.`)
    if (p.aspectRatio !== undefined && !(isNum(p.aspectRatio) && p.aspectRatio > 0))
      throw new Error(`${where}: perspective.aspectRatio must be positive.`)
    for (const key of ['width', 'height'] as const)
      if (e[key] !== undefined && !(Number.isInteger(e[key]) && (e[key] as number) > 0))
        throw new Error(`${where}: ${key} must be a positive integer.`)
    return {
      name: e.name,
      body: e.body as number,
      pos: e.pos as SceneCameraEntry['pos'],
      quat: e.quat as SceneCameraEntry['quat'],
      perspective: {
        yfov: p.yfov,
        znear: p.znear,
        ...(p.aspectRatio !== undefined ? { aspectRatio: p.aspectRatio as number } : {}),
        ...(p.zfar !== undefined ? { zfar: p.zfar as number } : {}),
      },
      ...(typeof e.kind === 'string' ? { kind: e.kind } : {}),
      ...(e.width !== undefined ? { width: e.width as number } : {}),
      ...(e.height !== undefined ? { height: e.height as number } : {}),
    }
  })
}

/** The camera an entry describes, at its mount pose in its body's frame. The caller parents it. */
export function buildSceneCamera(entry: SceneCameraEntry): SceneCamera {
  const { yfov, aspectRatio, znear, zfar } = entry.perspective
  const imageAspect =
    entry.width !== undefined && entry.height !== undefined ? entry.width / entry.height : undefined
  const aspect = imageAspect ?? aspectRatio
  const camera = new PerspectiveCamera(
    MathUtils.radToDeg(yfov),
    aspectRatio ?? aspect ?? 1,
    znear,
    zfar ?? znear * INFINITE_FAR_RATIO,
  )
  camera.name = entry.name
  camera.position.set(entry.pos[0], entry.pos[1], entry.pos[2])
  const [w, x, y, z] = entry.quat
  camera.quaternion.copy(new Quaternion(x, y, z, w).normalize())
  return {
    name: entry.name,
    kind: entry.kind ?? 'rgb',
    width: entry.width,
    height: entry.height,
    aspect,
    projectionAspect: aspectRatio,
    camera,
  }
}

/** The cameras a viewer offers to look through: those whose image is a picture of the scene. A
 *  depth sensor's image is a distance per pixel, which rendering the scene's colours would misstate. */
export function viewpoints(cameras: readonly SceneCamera[]): SceneCamera[] {
  return cameras.filter((c) => c.kind === 'rgb')
}

/** The largest box of `aspect` (width / height) centred in a `width` x `height` canvas. */
export function letterbox(
  width: number,
  height: number,
  aspect: number,
): { x: number; y: number; width: number; height: number } {
  if (width / height > aspect) {
    const w = height * aspect
    return { x: (width - w) / 2, y: 0, width: w, height }
  }
  const h = width / aspect
  return { x: 0, y: (height - h) / 2, width, height: h }
}
