// Shared-candidate code: this file imports only 'three' and 'three/addons' -- keep it free of
// robovast imports so it can be extracted into a package other projects consume too (see README.md
// in this directory).
//
// Loads a scene -- one binary glTF file (`.glb`) a simulator backend's exporter writes, see
// docs/simulators.rst -- into a three.js Group, and returns the imperative API a motion source
// drives: `basePose` seats any body at a world pose, `jointMap` turns a hinge or slides a slide.
//
// What the file carries beyond core glTF is in `extras` (docs/simulators.rst, "The scene"):
// each body is a node whose extras name the body and its joints, sensor cameras are camera nodes
// whose extras name the sensor, and the scene's extras state the contract (`format`/`version`),
// the joint values the nodes are posed at and the world's authored view.
//
// Coordinate frame: glTF is Y-up and the world Z-up. The file's `world` node carries the turn
// between them; this loader takes it off, so `root` is Z-up like the world the poses are recorded
// in, and the viewport's wrapper group turns the whole scene into three's Y-up once.

import {
  Group,
  Matrix4,
  Mesh,
  Object3D,
  Quaternion,
  SkinnedMesh,
  Vector3,
  type Material,
  type Texture,
} from 'three'
import type { GLTF } from 'three/addons/loaders/GLTFLoader.js'
import { buildSceneCamera, parseSceneCameras, type SceneCamera } from './sceneCameras'

/** The contract a scene's `extras` state in their `format` field. */
export const SCENE_FORMAT = 'roqsim.scene'
/** The highest contract version this loader reads. */
export const SCENE_VERSION = 1

/**
 * Refuse a scene this loader cannot read, naming what it states and what is read. A newer version
 * would otherwise be drawn with whatever keys overlap, and a glTF file that states no contract has
 * no bodies or joints a motion source could address.
 */
export function checkSceneFormat(extras: { format?: unknown; version?: unknown }, url: string): void {
  if (extras.format !== SCENE_FORMAT) {
    throw new Error(
      `${url} states ${extras.format === undefined ? 'no scene contract' : `the contract ${JSON.stringify(extras.format)}`}` +
        ` in its scene extras, not ${JSON.stringify(SCENE_FORMAT)}.`,
    )
  }
  const version = extras.version
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) {
    throw new Error(`${url}: scene contract version ${JSON.stringify(version)} is not a positive integer.`)
  }
  if (version > SCENE_VERSION) {
    throw new Error(
      `${url} is scene contract version ${version}; this viewer reads up to ${SCENE_VERSION}. ` +
        'Update the viewer to one that reads it.',
    )
  }
}

/** A joint as a body node's `extras.joints` states it. */
interface SceneJoint {
  name: string
  type: 'hinge' | 'slide' | 'free' | 'ball' | 'unknown'
  axis: [number, number, number]
  pos: [number, number, number]
  /** The value at which the body sits at its reference pose; a value q moves it by q - ref. */
  ref?: number
}

/** The scene's own `extras`. */
interface SceneExtras {
  format?: string
  version?: number
  /** The value each named hinge and slide is posed at in the file. */
  initialJoints?: Record<string, number>
  /** The world's authored initial camera (a MuJoCo free camera, Z-up world frame). */
  view?: { lookat?: number[]; distance?: number; azimuth?: number; elevation?: number }
}

interface GltfNode {
  name?: string
  children?: number[]
  camera?: number
  translation?: [number, number, number]
  rotation?: [number, number, number, number] // xyzw
  extras?: Record<string, unknown>
}

interface GltfJson {
  scene?: number
  scenes: { nodes?: number[]; extras?: SceneExtras }[]
  nodes?: GltfNode[]
  cameras?: { type: string; perspective?: { yfov: number; aspectRatio?: number; znear: number; zfar?: number } }[]
}

export interface SceneModel {
  root: Group
  jointMap: Record<string, (value: number) => void>
  initialJoints: Record<string, number>
  /** Every body name in the scene, i.e. every name `basePose` accepts. A consumer reports the
   *  names it could not resolve against this, so a motion source aimed at the wrong world is visible
   *  rather than rendering a plausible-looking lie. */
  bodies: string[]
  /** Every joint name `jointMap` accepts. The other half of the same check. */
  joints: string[]
  /** The scene's authored initial camera, passed through verbatim (absent when unauthored). */
  view?: SceneExtras['view']
  /** The sensor cameras the scene places, each a child of its body's node (sceneCameras.ts). */
  cameras: SceneCamera[]
  /** The node of the body named *body*, or of the world body without one -- what a viewer-placed
   *  camera is parented to so it is posed in that frame. `undefined` for a name the scene lacks. */
  frameNode: (body?: string) => Object3D | undefined
  /**
   * Seat a body at a **world** pose, Z-up, from a run's pose track. Works for any body: the pose is
   * composed with the inverse of the parent's root-relative transform, so a link deep in a
   * kinematic chain lands where it belongs rather than at that offset from its parent. Apply parents
   * before children within a frame (the recording's body order is that order) so a child composes
   * against its parent's new transform.
   *
   * No-op if the named body does not exist -- a consumer checks `bodies` and reports, rather than
   * having every call throw.
   */
  basePose: (
    bodyName: string,
    pos: ArrayLike<number>,
    quat: readonly [number, number, number, number],
  ) => void
  /**
   * Put every body back at the pose the file gives it -- the state `loadScene` returns. What lets
   * one model serve a second run of the same world: a body the previous run's motion moved and the
   * next run's does not drive would otherwise stay where the previous run left it.
   */
  reset: () => void
  /**
   * Release this scene's GPU resources (geometries, materials, textures).
   *
   * Needed because switching campaign switches *world*: the viewport frees whatever it is showing when
   * it is torn down, but replacing one scene with another inside a live viewport would otherwise leave
   * the previous world's buffers resident for as long as the tab is open.
   */
  dispose: () => void
}

/** Free every geometry, material and texture under `root`.
 *
 *  One implementation, used both when a viewport goes away and when a scene is replaced inside a live
 *  one -- the second case is what switching campaign does, and it has no other disposal path.
 */
export function disposeSceneGraph(root: Object3D): void {
  root.traverse((obj) => {
    if (!(obj instanceof Mesh)) return
    obj.geometry.dispose()
    const materials: Material[] = Array.isArray(obj.material) ? obj.material : [obj.material]
    for (const m of materials) {
      for (const value of Object.values(m)) {
        if (value && (value as Texture).isTexture) (value as Texture).dispose()
      }
      m.dispose()
    }
  })
}

/** A pose (position, wxyz quaternion) composed into `out`, allocating nothing: basePose runs for
 *  every driven body on every animation frame. */
const composeScratch = { pos: new Vector3(), quat: new Quaternion(), scale: new Vector3(1, 1, 1) }
function composePose(
  out: Matrix4,
  pos: ArrayLike<number>,
  quat: readonly [number, number, number, number],
): Matrix4 {
  composeScratch.pos.set(pos[0], pos[1], pos[2])
  composeScratch.quat.set(quat[1], quat[2], quat[3], quat[0])
  return out.compose(composeScratch.pos, composeScratch.quat, composeScratch.scale)
}

/** How a hinge or a slide moves its body at `value`: about or along its axis, through its anchor. */
function jointMotion(joint: SceneJoint, value: number, out: Matrix4): Matrix4 {
  const axis = new Vector3(joint.axis[0], joint.axis[1], joint.axis[2]).normalize()
  const q = value - (joint.ref ?? 0)
  if (joint.type === 'hinge') {
    const [x, y, z] = joint.pos
    return out
      .makeTranslation(x, y, z)
      .multiply(new Matrix4().makeRotationAxis(axis, q))
      .multiply(new Matrix4().makeTranslation(-x, -y, -z))
  }
  const t = axis.multiplyScalar(q)
  return out.makeTranslation(t.x, t.y, t.z)
}

/** The decoders a scene file may need, loaded with the loader the first time a scene is. */
async function gltfLoader() {
  const [{ GLTFLoader }, { MeshoptDecoder }] = await Promise.all([
    import('three/addons/loaders/GLTFLoader.js'),
    import('three/addons/libs/meshopt_decoder.module.js'),
  ])
  return new GLTFLoader().setMeshoptDecoder(MeshoptDecoder)
}

/** Fetch and build the scene at `sceneUrl`. */
export async function loadScene(sceneUrl: string): Promise<SceneModel> {
  const baseUrl = new URL(sceneUrl, window.location.href).href
  const [loader, bytes] = await Promise.all([
    gltfLoader(),
    fetch(sceneUrl).then((r) => {
      if (!r.ok) throw new Error(`Failed to fetch ${sceneUrl} (${r.status} ${r.statusText}).`)
      return r.arrayBuffer()
    }),
  ])
  const gltf = await loader.parseAsync(bytes, baseUrl.slice(0, baseUrl.lastIndexOf('/') + 1))
  return sceneModel(gltf, sceneUrl)
}

/** The scene model of a parsed glTF file. */
export function sceneModel(gltf: GLTF, url: string): SceneModel {
  const json = gltf.parser.json as GltfJson
  const sceneDef = json.scenes[json.scene ?? 0]
  const extras: SceneExtras = sceneDef.extras ?? {}
  checkSceneFormat(extras, url)
  const nodeDefs = json.nodes ?? []

  // glTF node index -> the object three built for it.
  const objects = new Map<number, Object3D>()
  gltf.scene.traverse((obj) => {
    const index = gltf.parser.associations.get(obj)?.nodes
    if (index !== undefined && !objects.has(index)) objects.set(index, obj)
  })
  const parentOf = new Map<number, number>()
  nodeDefs.forEach((n, i) => n.children?.forEach((c) => parentOf.set(c, i)))

  // Bodies, by body id. The node name is the body's exact name (three's loader rewrites some
  // characters in it), or `body_<id>` for an unnamed one.
  const bodyNode: Object3D[] = []
  const bodyNames: string[] = []
  const bodyJoints: SceneJoint[][] = []
  const bodyOfNode = new Map<number, number>()
  nodeDefs.forEach((n, i) => {
    const id = n.extras?.body_id
    if (typeof id !== 'number') return
    const obj = objects.get(i)
    if (!obj) throw new Error(`${url}: node ${i} (body ${id}) is not in the scene.`)
    const name = typeof n.extras?.body === 'string' ? n.extras.body : ''
    obj.name = name || `body_${id}`
    bodyNode[id] = obj
    bodyNames[id] = name
    bodyJoints[id] = Array.isArray(n.extras?.joints) ? (n.extras.joints as SceneJoint[]) : []
    bodyOfNode.set(i, id)
  })
  const world = bodyNode[0]
  if (!world) throw new Error(`${url} has no world body (a node with extras.body_id 0).`)
  for (let i = 0; i < bodyNode.length; i++) {
    if (!bodyNode[i]) throw new Error(`${url} has no node for body ${i}.`)
  }

  // The scene, Z-up: the world node without the file's turn onto Y-up.
  const root = new Group()
  root.name = 'scene'
  world.quaternion.identity()
  root.add(world)
  // A skinned mesh is a root of the glTF scene (glTF ignores its node's transform), and three
  // skins it from its bones' world matrices: put under the world node, it is drawn wherever its
  // bones are, and never culled -- its bones move far past the bind pose's bounds.
  for (const child of [...gltf.scene.children]) {
    let skinned = false
    child.traverse((obj) => {
      if ((obj as SkinnedMesh).isSkinnedMesh) {
        obj.frustumCulled = false
        skinned = true
      }
    })
    if (skinned) world.add(child)
  }

  // Sensor cameras: built from their nodes the same way a viewer-placed camera is, on the node of
  // the body each is mounted on; three's own camera objects are dropped.
  const entries = nodeDefs.flatMap((n, i) => {
    if (n.camera === undefined || typeof n.extras?.sensor !== 'string') return []
    const body = bodyOfNode.get(parentOf.get(i) ?? -1)
    const [x, y, z, w] = n.rotation ?? [0, 0, 0, 1]
    return [{
      name: n.extras.sensor,
      body: body ?? -1,
      pos: n.translation ?? [0, 0, 0],
      quat: [w, x, y, z],
      perspective: json.cameras?.[n.camera]?.perspective ?? {},
      kind: n.extras.kind,
      width: n.extras.width,
      height: n.extras.height,
    }]
  })
  for (const [i, obj] of objects) {
    if (nodeDefs[i].camera !== undefined) obj.removeFromParent()
  }
  const cameras = parseSceneCameras(entries, bodyNode.length, url).map((entry) => {
    const sceneCamera = buildSceneCamera(entry)
    bodyNode[entry.body].add(sceneCamera.camera)
    return sceneCamera
  })

  root.traverse((obj) => {
    if (obj instanceof Mesh) {
      obj.castShadow = true
      obj.receiveShadow = true
    }
  })

  // Body transforms are written by hand from here on (basePose, jointMap), never recomposed from
  // position/quaternion.
  const initialJoints = extras.initialJoints ?? {}
  const valueOf = (j: SceneJoint) => (j.name && j.name in initialJoints ? initialJoints[j.name] : j.ref ?? 0)
  const scratch = new Matrix4()
  const posed: Matrix4[] = [] // each body's local transform as the file gives it
  const rest: Matrix4[] = [] // ... with its hinges and slides at their reference values
  bodyNode.forEach((node, i) => {
    node.updateMatrix()
    node.matrixAutoUpdate = false
    posed[i] = node.matrix.clone()
    const motion = new Matrix4()
    for (const j of bodyJoints[i]) {
      if (j.type === 'hinge' || j.type === 'slide') motion.multiply(jointMotion(j, valueOf(j), scratch))
    }
    rest[i] = posed[i].clone().multiply(motion.invert())
  })

  // jointMap: a body's local transform is its rest transform times the motion of each of its
  // hinges and slides at its current value, composed in the order the body declares them.
  const jointValue = new Map<SceneJoint, number>()
  const recompute = (i: number) => {
    const m = rest[i].clone()
    for (const j of bodyJoints[i]) {
      if (j.type !== 'hinge' && j.type !== 'slide') continue
      m.multiply(jointMotion(j, jointValue.get(j) ?? valueOf(j), scratch))
    }
    bodyNode[i].matrix.copy(m)
    bodyNode[i].matrixWorldNeedsUpdate = true
  }
  const jointMap: Record<string, (value: number) => void> = {}
  bodyJoints.forEach((joints, i) => {
    for (const j of joints) {
      if ((j.type !== 'hinge' && j.type !== 'slide') || !j.name) continue
      jointMap[j.name] = (value: number) => {
        jointValue.set(j, value)
        recompute(i)
      }
    }
  })

  const reset = () => {
    jointValue.clear()
    bodyNode.forEach((node, i) => {
      node.matrix.copy(posed[i])
      node.matrixWorldNeedsUpdate = true
    })
  }

  // Scratch matrices: basePose runs per driven body per frame, so it allocates nothing.
  const relScratch = new Matrix4()
  const mulScratch = new Matrix4()
  const localScratch = new Matrix4()

  /** Transform of `node` relative to the scene root, from local matrices only.
   *
   *  Deliberately not `node.matrixWorld`: the viewport mounts this scene under a group rotated -PI/2
   *  about X to reach three's Y-up, so a world matrix carries that frame conversion and inverting it
   *  would cancel it -- placing every driven body in the wrong frame. Reading local matrices also
   *  removes any ordering dependency on three's world-matrix bookkeeping: a child composed later in
   *  the same frame sees its parent's freshly written `matrix`, not last frame's `matrixWorld`.
   */
  const relativeToRoot = (node: Object3D): Matrix4 => {
    relScratch.identity()
    for (let n: Object3D | null = node; n && n !== root; n = n.parent) {
      mulScratch.multiplyMatrices(n.matrix, relScratch)
      relScratch.copy(mulScratch)
    }
    return relScratch
  }

  const bodyIndexByName = new Map<string, number>()
  bodyNames.forEach((name, i) => {
    if (name) bodyIndexByName.set(name, i)
  })

  const basePose = (
    bodyName: string,
    pos: ArrayLike<number>,
    quat: readonly [number, number, number, number],
  ) => {
    const i = bodyIndexByName.get(bodyName)
    if (i == null) return
    const node = bodyNode[i]
    composePose(localScratch, pos, quat)
    const parent = node.parent
    if (parent && parent !== root) {
      // local = (parent relative to root)^-1 * (desired, relative to root)
      node.matrix.multiplyMatrices(relativeToRoot(parent).invert(), localScratch)
    } else {
      node.matrix.copy(localScratch)
    }
    node.matrixWorldNeedsUpdate = true
  }

  return {
    root,
    jointMap,
    initialJoints,
    bodies: bodyNames,
    joints: Object.keys(jointMap),
    basePose,
    view: extras.view,
    cameras,
    frameNode: (body?: string) => {
      if (body === undefined) return world
      const i = bodyIndexByName.get(body)
      return i == null ? undefined : bodyNode[i]
    },
    reset,
    dispose: () => disposeSceneGraph(root),
  }
}
