// Shared-candidate code: this file imports only 'three' -- keep it free of robovast imports (see
// README.md in this directory).
//
// The cameras a scene places on its bodies: a sensor's mount and projection, so a viewer
// renders the scene from where the sensor looked instead of shipping the sensor's images.
//
// Each is a glTF perspective camera on a node of a body (docs/simulators.rst, "Camera sensors"),
// read by the loader into:
//
//   { name, body, pos: [x, y, z], quat: [w, x, y, z],
//     perspective: { yfov, aspectRatio?, znear, zfar? }, kind?, width?, height? }
//
// The camera looks down its local -Z with +Y up, which is three's camera convention too, so the entry
// becomes a PerspectiveCamera parented to its body's node with nothing converted. As a child of that
// node it follows the body as the motion source moves it, and the renderer keeps its view matrix
// current with the rest of the graph.
//
// A free view is the same thing placed by the viewer rather than by the world: a position, a point to
// look at and a field of view, in the scene's world frame or in a body's frame
// (`parseFreeView`, `buildFreeCamera`).

import { MathUtils, Matrix4, PerspectiveCamera, Quaternion, Vector3 } from 'three'

/** One sensor camera, as the scene places it. */
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
  /** The published image's size, when the scene states it. */
  width?: number
  height?: number
  /** Width over height of the picture: the image's when stated, else the projection's. `undefined`
   *  when the scene states neither, and the picture then takes the canvas's shape. */
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
 * The scene's sensor cameras, checked. An absent list is no cameras; an entry that cannot be
 * placed is refused naming it, rather than being drawn from somewhere it is not.
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

/** A camera the viewer places: `position` and `look_at` in metres, in the scene's frame (Z-up
 *  world), or in the frame of body `attach`, which it then follows. */
export interface FreeViewSpec {
  position: [number, number, number]
  look_at: [number, number, number]
  /** Vertical field of view in degrees. */
  fov: number
  attach?: string
  up: [number, number, number]
  near: number
  far: number
  /** Width over height of the picture; `undefined` takes the panel's shape. */
  aspect?: number
}

/** What a free view takes when it does not say: a 60 degree field of view, the world's +z as up,
 *  and clip planes at 5 cm and 1 km. */
export const FREE_VIEW_DEFAULTS = {
  fov: 60,
  up: [0, 0, 1] as [number, number, number],
  near: 0.05,
  far: 1000,
}

/** The keys a free view takes. */
export const FREE_VIEW_KEYS: readonly string[] = [
  'position', 'look_at', 'fov', 'attach', 'up', 'near', 'far', 'width', 'height', 'aspect',
]

/** A free view from a panel's `view`, checked; throws naming what is wrong. */
export function parseFreeView(raw: unknown): FreeViewSpec {
  const where = 'camera panel view'
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw))
    throw new Error(`${where}: expected a mapping with position and look_at.`)
  const v = raw as Record<string, unknown>
  const unknown = Object.keys(v).filter((k) => !FREE_VIEW_KEYS.includes(k))
  if (unknown.length)
    throw new Error(
      `${where}: unknown ${unknown.join(', ')} (it takes ${FREE_VIEW_KEYS.join(', ')}; ` +
        'azimuth/elevation/distance/lookat are the server render\'s view, which needs kind: render).',
    )
  const vec = (key: string): [number, number, number] => {
    if (!isVec(v[key], 3)) throw new Error(`${where}: ${key} must be [x, y, z] in metres.`)
    return v[key] as [number, number, number]
  }
  const pos = (key: string, fallback: number): number => {
    if (v[key] === undefined) return fallback
    if (!isNum(v[key]) || (v[key] as number) <= 0) throw new Error(`${where}: ${key} must be positive.`)
    return v[key] as number
  }
  const position = vec('position')
  const lookAt = vec('look_at')
  const up = v.up === undefined ? FREE_VIEW_DEFAULTS.up : vec('up')
  const fov = pos('fov', FREE_VIEW_DEFAULTS.fov)
  if (fov >= 180) throw new Error(`${where}: fov must be below 180 degrees.`)
  const near = pos('near', FREE_VIEW_DEFAULTS.near)
  const far = pos('far', FREE_VIEW_DEFAULTS.far)
  if (far <= near) throw new Error(`${where}: far must exceed near.`)
  if (v.attach !== undefined && (typeof v.attach !== 'string' || !v.attach))
    throw new Error(`${where}: attach must name a body.`)
  if ((v.width === undefined) !== (v.height === undefined))
    throw new Error(`${where}: width and height go together.`)
  if (v.aspect !== undefined && v.width !== undefined)
    throw new Error(`${where}: give aspect or width/height, not both.`)
  let aspect: number | undefined
  if (v.width !== undefined) aspect = pos('width', 0) / pos('height', 0)
  else if (v.aspect !== undefined) aspect = pos('aspect', 0)
  const dir = new Vector3(...lookAt).sub(new Vector3(...position))
  if (dir.lengthSq() === 0) throw new Error(`${where}: position and look_at are the same point.`)
  if (new Vector3(...up).cross(dir).lengthSq() < 1e-12 * dir.lengthSq() * new Vector3(...up).lengthSq())
    throw new Error(`${where}: up is parallel to the view direction; give another up.`)
  return {
    position, look_at: lookAt, fov, up, near, far,
    ...(v.attach !== undefined ? { attach: v.attach as string } : {}),
    ...(aspect !== undefined ? { aspect } : {}),
  }
}

/** The camera a free view describes, posed in the frame it is given in (the caller parents it to the
 *  scene's world node, or to the `attach` body's node). It looks from `position` at `look_at`, with
 *  `up` the image's up as far as the view direction allows. */
export function buildFreeCamera(spec: FreeViewSpec, name = 'view'): SceneCamera {
  const camera = new PerspectiveCamera(spec.fov, spec.aspect ?? 1, spec.near, spec.far)
  camera.name = name
  const eye = new Vector3(...spec.position)
  const rotation = new Matrix4().lookAt(eye, new Vector3(...spec.look_at), new Vector3(...spec.up))
  camera.position.copy(eye)
  camera.quaternion.setFromRotationMatrix(rotation)
  return { name, kind: 'rgb', aspect: spec.aspect, camera }
}
