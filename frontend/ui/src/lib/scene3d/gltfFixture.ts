// A small glTF scene for the tests: bodies, their joints and sensor cameras, written to the contract
// the exporter writes (docs/simulators.rst, "The scene") and served by a stubbed fetch.

/** The world node's turn from the world's Z-up onto glTF's Y-up, as an exporter writes it. */
export const WORLD_TURN = [-Math.SQRT1_2, 0, 0, Math.SQRT1_2]

export interface FixtureBody {
  name: string
  parent: number
  translation?: [number, number, number]
  rotation?: [number, number, number, number] // xyzw
  joints?: { name: string; type: string; axis: number[]; pos: number[]; ref?: number }[]
}

export interface FixtureCamera {
  sensor: string
  body: number
  translation: [number, number, number]
  rotation: [number, number, number, number] // xyzw
  perspective: { yfov: number; aspectRatio?: number; znear: number; zfar?: number }
  kind?: string
  width?: number
  height?: number
}

/** The glTF JSON of a scene of `bodies` (body 0 the world) with `cameras` and scene `extras`. */
export function gltfScene(
  bodies: FixtureBody[],
  extras: Record<string, unknown> = { format: 'roqsim.scene', version: 1, initialJoints: {} },
  cameras: FixtureCamera[] = [],
): Record<string, unknown> {
  const nodes: Record<string, unknown>[] = bodies.map((b, i) => ({
    ...(b.name ? { name: b.name } : {}),
    ...(i === 0 ? { rotation: WORLD_TURN } : {}),
    ...(b.translation ? { translation: b.translation } : {}),
    ...(b.rotation ? { rotation: b.rotation } : {}),
    extras: { body: b.name, body_id: i, ...(b.joints ? { joints: b.joints } : {}) },
  }))
  const child = (parent: number, node: number) => {
    const p = nodes[parent] as { children?: number[] }
    p.children = [...(p.children ?? []), node]
  }
  bodies.forEach((b, i) => {
    if (i) child(b.parent, i)
  })
  const gltfCameras = cameras.map((c) => ({ name: c.sensor, type: 'perspective', perspective: c.perspective }))
  cameras.forEach((c, i) => {
    nodes.push({
      name: c.sensor,
      translation: c.translation,
      rotation: c.rotation,
      camera: i,
      extras: { sensor: c.sensor, kind: c.kind, width: c.width, height: c.height },
    })
    child(c.body, nodes.length - 1)
  })
  return {
    asset: { version: '2.0' },
    scene: 0,
    scenes: [{ nodes: [0], extras }],
    nodes,
    ...(gltfCameras.length ? { cameras: gltfCameras } : {}),
  }
}

/** A fetch that answers every request with `json` as the response body. */
export function fetchOf(json: unknown) {
  const bytes = new TextEncoder().encode(JSON.stringify(json))
  return async () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  })
}
