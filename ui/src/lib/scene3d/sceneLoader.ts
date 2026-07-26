// Shared-candidate code: this file imports only 'three' -- keep it free of robovast imports so it
// can be extracted into a package other projects consume too (see README.md in this directory).
//
// Loads a scene descriptor (scene.json + scene.bin, produced by rst's `rst-export-web`
// or the MujocoSim adapter's SIM_SUITE_SCENE_EXPORT_DIR hook) into a three.js Group, and returns a
// jointMap that animates hinge/slide joints -- covering the whole scene (robot + environment), not
// just the articulated robot.
//
// Coordinate frame: the descriptor is Z-up (like ROS). The viewport wraps this Group in a group
// rotated -PI/2 about X to bring it into three's Y-up.

import {
  BoxGeometry,
  BufferAttribute,
  BufferGeometry,
  CapsuleGeometry,
  CylinderGeometry,
  DataTexture,
  DoubleSide,
  Group,
  Matrix4,
  Mesh,
  MeshStandardMaterial,
  Object3D,
  PlaneGeometry,
  Quaternion,
  RepeatWrapping,
  RGBAFormat,
  Skeleton,
  SkinnedMesh,
  SphereGeometry,
  SRGBColorSpace,
  TextureLoader,
  Uint16BufferAttribute,
  Vector3,
  type Bone,
  type Texture,
} from 'three'

interface BinRef {
  off: number
  count: number
}

interface SceneBody {
  name: string
  parent: number
  pos: [number, number, number]
  quat: [number, number, number, number] // wxyz order
}

interface SceneJoint {
  name: string
  body: number
  type: 'hinge' | 'slide' | 'free' | 'ball' | 'unknown'
  axis: [number, number, number]
  pos: [number, number, number]
  qposadr: number
}

interface SceneGeom {
  body: number
  type: 'plane' | 'sphere' | 'capsule' | 'ellipsoid' | 'cylinder' | 'box' | 'mesh'
  pos: [number, number, number]
  quat: [number, number, number, number] // wxyz
  size: [number, number, number]
  matid: number
  rgba: [number, number, number, number]
  mesh: number | null
  skin?: number | null // index into scene.skins -> render as a deformable THREE.SkinnedMesh
}

interface SceneMesh {
  vert: BinRef
  index: BinRef
  uv?: BinRef
}

// Skin bind data for a SkinnedMesh: which body nodes are the bones, their bind-pose (descriptor-frame)
// transforms (-> boneInverses), and the per-vertex bone indices/weights (4 slots each, index into
// `bones`). The mesh's live deformation follows the bone body nodes, driven by basePose over /tf.
interface SceneSkin {
  bones: string[] // body names; must exist as scene body nodes
  bindpos: [number, number, number][]
  bindquat: [number, number, number, number][] // wxyz per bone
  skinIndex: BinRef // uint16, 4 per vertex
  skinWeight: BinRef // float32, 4 per vertex
}

interface SceneMaterial {
  rgba: [number, number, number, number]
  texture: number // -1 = none
  texrepeat: [number, number]
  texuniform: boolean
}

type SceneTexture =
  | { file: string }
  | { raw: BinRef; width: number; height: number; channels: number }

interface SceneDescriptor {
  up: string
  bodies: SceneBody[]
  joints: SceneJoint[]
  initialJoints: Record<string, number>
  geoms: SceneGeom[]
  meshes: SceneMesh[]
  skins?: SceneSkin[]
  materials: SceneMaterial[]
  textures: SceneTexture[]
  // The world's authored initial camera (a MuJoCo free camera, Z-up world frame), baked by the
  // exporter so any viewer frames the scene the way the world author intended.
  view?: { lookat?: number[]; distance?: number; azimuth?: number; elevation?: number }
}

export interface SceneModel {
  root: Group
  jointMap: Record<string, (value: number) => void>
  initialJoints: Record<string, number>
  /** The descriptor's baked initial camera, passed through verbatim (absent when unauthored). */
  view?: SceneDescriptor['view']
  /**
   * Drive a body's world pose from live data (e.g. a mobile robot base from /odom). The base body's
   * parent is the world body, so its local matrix is its world pose -- set in the same descriptor
   * frame as the rest transforms. No-op if the named body does not exist.
   */
  basePose: (
    bodyName: string,
    pos: readonly number[],
    quat: [number, number, number, number],
  ) => void
}

// The descriptor quaternion is (w, x, y, z); three's Quaternion constructor takes (x, y, z, w).
function descQuat(q: [number, number, number, number]): Quaternion {
  return new Quaternion(q[1], q[2], q[3], q[0])
}

function restMatrix(pos: readonly number[], quat: [number, number, number, number]): Matrix4 {
  return new Matrix4().compose(
    new Vector3(pos[0], pos[1], pos[2]),
    descQuat(quat),
    new Vector3(1, 1, 1),
  )
}

// A primitive whose descriptor axis differs from three's needs its geometry pre-rotated. In the
// descriptor, capsules and cylinders extend along +Z; three's Capsule/CylinderGeometry extend along +Y.
function rotateZup(geometry: BufferGeometry): BufferGeometry {
  geometry.rotateX(Math.PI / 2)
  return geometry
}

function buildTexture(
  tex: SceneTexture | undefined,
  bin: ArrayBuffer,
  loader: TextureLoader,
  baseUrl: string,
): Texture | null {
  if (!tex) return null
  let texture: Texture
  if ('file' in tex) {
    texture = loader.load(new URL(tex.file, baseUrl).href)
  } else {
    // Raw procedural texture packed in scene.bin. Expand to RGBA (three DataTexture wants 4 channels).
    const src = new Uint8Array(bin, tex.raw.off, tex.raw.count)
    const px = tex.width * tex.height
    const rgba = new Uint8Array(px * 4)
    const ch = tex.channels
    for (let i = 0; i < px; i++) {
      rgba[i * 4] = src[i * ch]
      rgba[i * 4 + 1] = src[i * ch + 1]
      rgba[i * 4 + 2] = src[i * ch + 2]
      rgba[i * 4 + 3] = ch >= 4 ? src[i * ch + 3] : 255
    }
    const data = new DataTexture(rgba, tex.width, tex.height, RGBAFormat)
    data.needsUpdate = true
    texture = data
  }
  texture.colorSpace = SRGBColorSpace
  texture.wrapS = RepeatWrapping
  texture.wrapT = RepeatWrapping
  return texture
}

function buildMaterial(
  geom: SceneGeom,
  scene: SceneDescriptor,
  textures: (Texture | null)[],
): MeshStandardMaterial {
  const mat = geom.matid >= 0 ? scene.materials[geom.matid] : null
  const rgba = mat ? mat.rgba : geom.rgba
  const material = new MeshStandardMaterial({
    metalness: 0.1,
    roughness: 0.8,
    transparent: rgba[3] < 1,
    opacity: rgba[3],
    side: geom.type === 'plane' ? DoubleSide : undefined,
  })
  material.color.setRGB(rgba[0], rgba[1], rgba[2], SRGBColorSpace)

  if (mat && mat.texture >= 0) {
    const texture = textures[mat.texture]
    if (texture) {
      // Mesh geoms carry the tiling in their (triplanar) UVs, so leave texture.repeat at 1. For
      // primitives (plane/box) three's built-in UVs run 0..1 over the geom, so scale the repeat by
      // the geom's planar span (the descriptor size is a half-extent; texuniform => texrepeat is per-metre).
      if (geom.type !== 'mesh') {
        const spanU = mat.texuniform ? 2 * geom.size[0] : 1
        const spanV = mat.texuniform ? 2 * geom.size[1] : 1
        texture.repeat.set(mat.texrepeat[0] * spanU, mat.texrepeat[1] * spanV)
      }
      material.map = texture
      material.color.setRGB(1, 1, 1) // don't tint the texture
    }
  }
  return material
}

function buildGeometry(geom: SceneGeom, scene: SceneDescriptor, bin: ArrayBuffer): BufferGeometry {
  const [sx, sy, sz] = geom.size
  switch (geom.type) {
    case 'box':
      return new BoxGeometry(2 * sx, 2 * sy, 2 * sz)
    case 'plane': {
      // size 0 means an unbounded plane; give it a large finite quad.
      const w = sx > 0 ? 2 * sx : 50
      const h = sy > 0 ? 2 * sy : 50
      return new PlaneGeometry(w, h)
    }
    case 'sphere':
      return new SphereGeometry(sx, 24, 16)
    case 'ellipsoid': {
      const g = new SphereGeometry(1, 24, 16)
      g.scale(sx, sy, sz)
      return g
    }
    case 'capsule':
      return rotateZup(new CapsuleGeometry(sx, 2 * sy, 8, 16))
    case 'cylinder':
      return rotateZup(new CylinderGeometry(sx, sx, 2 * sy, 24))
    case 'mesh': {
      const m = scene.meshes[geom.mesh as number]
      const verts = new Float32Array(bin, m.vert.off, m.vert.count)
      const index = new Uint32Array(bin, m.index.off, m.index.count)
      const mat = geom.matid >= 0 ? scene.materials[geom.matid] : null
      const geometry = new BufferGeometry()

      if (!m.uv && mat && mat.texture >= 0) {
        // Textured mesh with no UVs (e.g. a floorplan mesh): synthesize texcoords by a per-face
        // triplanar projection -- pick the axis the triangle faces most and project onto the other
        // two -- so vertical walls tile correctly instead of smearing. Tiling is baked into the UVs
        // (texrepeat is per-metre under texuniform), so buildMaterial leaves texture.repeat at 1.
        // Needs a non-indexed geometry (UVs per face).
        const [ru, rv] = mat.texrepeat
        const nTri = index.length / 3
        const pos = new Float32Array(nTri * 9)
        const uv = new Float32Array(nTri * 6)
        const a = new Vector3(), b = new Vector3(), c = new Vector3(), n = new Vector3()
        for (let t = 0; t < nTri; t++) {
          const ia = index[3 * t], ib = index[3 * t + 1], ic = index[3 * t + 2]
          a.set(verts[3 * ia], verts[3 * ia + 1], verts[3 * ia + 2])
          b.set(verts[3 * ib], verts[3 * ib + 1], verts[3 * ib + 2])
          c.set(verts[3 * ic], verts[3 * ic + 1], verts[3 * ic + 2])
          n.copy(b).sub(a).cross(c.clone().sub(a))
          const ax = Math.abs(n.x), ay = Math.abs(n.y), az = Math.abs(n.z)
          // Project onto the plane perpendicular to the dominant normal axis.
          const uvOf = (v: Vector3): [number, number] =>
            az >= ax && az >= ay ? [v.x, v.y] : ax >= ay ? [v.y, v.z] : [v.x, v.z]
          const tri = [a, b, c]
          for (let k = 0; k < 3; k++) {
            pos[t * 9 + k * 3] = tri[k].x
            pos[t * 9 + k * 3 + 1] = tri[k].y
            pos[t * 9 + k * 3 + 2] = tri[k].z
            const [pu, pv] = uvOf(tri[k])
            uv[t * 6 + k * 2] = pu * ru
            uv[t * 6 + k * 2 + 1] = pv * rv
          }
        }
        geometry.setAttribute('position', new BufferAttribute(pos, 3))
        geometry.setAttribute('uv', new BufferAttribute(uv, 2))
      } else {
        geometry.setAttribute('position', new BufferAttribute(verts, 3))
        geometry.setIndex(new BufferAttribute(index, 1))
        if (m.uv) {
          geometry.setAttribute('uv', new BufferAttribute(new Float32Array(bin, m.uv.off, m.uv.count), 2))
        }
      }
      geometry.computeVertexNormals()
      return geometry
    }
    default:
      return new BoxGeometry(2 * sx, 2 * sy, 2 * sz)
  }
}

// Build a deformable THREE.SkinnedMesh for a skin geom. The skeleton bones are the *existing* body
// nodes named by the skin (already driven by basePose over /tf), so no new pose plumbing is needed --
// Skeleton.update() reads their matrixWorld each frame.
//
// Frame handling: skin verts and the bind poses are descriptor-frame (Z-up); the whole scene is
// wrapped in the viewport's up-rotation group (`upMatrix`). three's skinning cancels a common transform
// applied to both the mesh's bindMatrix and the bones, so binding with bindMatrix = upMatrix (which is
// exactly this mesh's matrixWorld -- it sits on the identity world body inside that group) and
// boneInverses = inverse(upMatrix * bindDescriptor) makes the mesh deform in the descriptor frame and
// render Y-up like every other geom. See the geom's body 0 placement in the exporter.
function buildSkinnedMesh(
  geom: SceneGeom,
  scene: SceneDescriptor,
  bin: ArrayBuffer,
  material: MeshStandardMaterial,
  nodes: Object3D[],
  bodyIndexByName: Map<string, number>,
  upMatrix: Matrix4,
): SkinnedMesh | null {
  const m = scene.meshes[geom.mesh as number]
  const skin = scene.skins?.[geom.skin as number]
  if (!skin) return null

  const geometry = new BufferGeometry()
  geometry.setAttribute('position', new BufferAttribute(new Float32Array(bin, m.vert.off, m.vert.count), 3))
  geometry.setIndex(new BufferAttribute(new Uint32Array(bin, m.index.off, m.index.count), 1))
  if (m.uv) {
    geometry.setAttribute('uv', new BufferAttribute(new Float32Array(bin, m.uv.off, m.uv.count), 2))
  }
  geometry.setAttribute(
    'skinIndex',
    new Uint16BufferAttribute(new Uint16Array(bin, skin.skinIndex.off, skin.skinIndex.count), 4),
  )
  geometry.setAttribute(
    'skinWeight',
    new BufferAttribute(new Float32Array(bin, skin.skinWeight.off, skin.skinWeight.count), 4),
  )
  geometry.computeVertexNormals()

  const bones: Object3D[] = []
  const boneInverses: Matrix4[] = []
  for (let i = 0; i < skin.bones.length; i += 1) {
    const bi = bodyIndexByName.get(skin.bones[i])
    if (bi == null) return null // a bone body missing from the tree -> cannot rig this skin
    bones.push(nodes[bi])
    const bind = restMatrix(skin.bindpos[i], skin.bindquat[i])
    boneInverses.push(new Matrix4().multiplyMatrices(upMatrix, bind).invert())
  }

  const mesh = new SkinnedMesh(geometry, material)
  mesh.castShadow = true
  mesh.receiveShadow = true
  mesh.frustumCulled = false // bones move well past the bind bbox; don't let three cull the walker
  // three's Skeleton is typed for Bone[]; at runtime it only reads each bone's matrixWorld, so the
  // existing body-node Object3Ds serve as bones directly (no separate Bone hierarchy to keep in sync).
  mesh.bind(new Skeleton(bones as Bone[], boneInverses), upMatrix)
  return mesh
}

export async function loadScene(sceneUrl: string): Promise<SceneModel> {
  const baseUrl = new URL(sceneUrl, window.location.href).href
  const [scene, bin] = await Promise.all([
    fetch(sceneUrl).then((r) => {
      if (!r.ok) throw new Error(`Failed to fetch ${sceneUrl} (${r.status} ${r.statusText}).`)
      return r.json() as Promise<SceneDescriptor>
    }),
    fetch(new URL('scene.bin', baseUrl).href).then((r) => {
      if (!r.ok) throw new Error(`Failed to fetch scene.bin (${r.status} ${r.statusText}).`)
      return r.arrayBuffer()
    }),
  ])

  const loader = new TextureLoader()
  const textures = scene.textures.map((t) => buildTexture(t, bin, loader, baseUrl))

  // One Object3D per scene body, parented per body.parent (body 0 = world = root).
  const root = new Group()
  root.name = 'scene'
  const nodes: Object3D[] = scene.bodies.map((b, i) => {
    const node = new Object3D()
    node.name = b.name || `body_${i}`
    node.matrixAutoUpdate = false
    return node
  })
  scene.bodies.forEach((b, i) => {
    const node = nodes[i]
    if (i === 0) {
      root.add(node) // world body
    } else {
      nodes[b.parent].add(node)
    }
  })

  // Rest transform per body; jointMap recomposes it with joint motion on each update.
  const restOf: Matrix4[] = scene.bodies.map((b) => restMatrix(b.pos, b.quat))
  const applyRest = (i: number) => {
    nodes[i].matrix.copy(restOf[i])
    nodes[i].matrixWorldNeedsUpdate = true
  }
  scene.bodies.forEach((_, i) => applyRest(i))

  // Body-name -> index, used both to bind skins to their bone nodes and to drive basePose by name.
  const bodyIndexByName = new Map<string, number>()
  scene.bodies.forEach((b, i) => {
    if (b.name) bodyIndexByName.set(b.name, i)
  })
  // The descriptor is Z-up; the viewport wraps the whole scene in a group rotated -PI/2 about X to
  // reach three's Y-up. Skin binding needs that same rotation (must stay in sync with the viewport).
  const upMatrix =
    scene.up === 'z' ? new Matrix4().makeRotationX(-Math.PI / 2) : new Matrix4()

  // Attach geom meshes to their body node (local geom transform). A geom carrying a `skin` block is a
  // deformable SkinnedMesh bound to its bone body nodes; everything else is a static Mesh.
  for (const geom of scene.geoms) {
    if (geom.type === 'mesh' && geom.mesh == null) continue
    const material = buildMaterial(geom, scene, textures)
    if (geom.skin != null) {
      const skinned = buildSkinnedMesh(geom, scene, bin, material, nodes, bodyIndexByName, upMatrix)
      if (skinned) nodes[geom.body].add(skinned)
      continue
    }
    const mesh = new Mesh(buildGeometry(geom, scene, bin), material)
    mesh.position.set(geom.pos[0], geom.pos[1], geom.pos[2])
    mesh.quaternion.copy(descQuat(geom.quat))
    mesh.castShadow = true
    mesh.receiveShadow = true
    nodes[geom.body].add(mesh)
  }

  // jointMap: recompute the joint's body local matrix = rest * jointMotion(value). Multiple joints
  // on one body compose in declaration order (only relevant for compound joints; the arm is 1/body).
  const bodyJoints = new Map<number, SceneJoint[]>()
  for (const j of scene.joints) {
    if (j.type !== 'hinge' && j.type !== 'slide') continue
    const list = bodyJoints.get(j.body) ?? []
    list.push(j)
    bodyJoints.set(j.body, list)
  }
  const jointValues = new Map<string, number>()
  for (const j of scene.joints) jointValues.set(j.name, scene.initialJoints[j.name] ?? 0)

  const recompute = (bodyId: number) => {
    const node = nodes[bodyId]
    const m = restOf[bodyId].clone()
    for (const j of bodyJoints.get(bodyId) ?? []) {
      const q = jointValues.get(j.name) ?? 0
      const axis = new Vector3(j.axis[0], j.axis[1], j.axis[2]).normalize()
      const anchor = new Vector3(j.pos[0], j.pos[1], j.pos[2])
      const motion = new Matrix4()
      if (j.type === 'hinge') {
        // translate(anchor) * rotate(axis, q) * translate(-anchor)
        motion
          .makeTranslation(anchor.x, anchor.y, anchor.z)
          .multiply(new Matrix4().makeRotationAxis(axis, q))
          .multiply(new Matrix4().makeTranslation(-anchor.x, -anchor.y, -anchor.z))
      } else {
        const t = axis.multiplyScalar(q)
        motion.makeTranslation(t.x, t.y, t.z)
      }
      m.multiply(motion)
    }
    node.matrix.copy(m)
    node.matrixWorldNeedsUpdate = true
  }

  const jointMap: Record<string, (value: number) => void> = {}
  for (const j of scene.joints) {
    if (j.type !== 'hinge' && j.type !== 'slide') continue
    jointMap[j.name] = (value: number) => {
      jointValues.set(j.name, value)
      recompute(j.body)
    }
  }
  // Seat the home pose so the arm shows its rest configuration before live data arrives.
  for (const bodyId of bodyJoints.keys()) recompute(bodyId)

  // basePose: drive a body's world pose from live data (mobile robot base from /odom, or a walker's
  // bones from /tf). Same matrix-direct pattern as the joints, in the descriptor frame.
  const basePose = (
    bodyName: string,
    pos: readonly number[],
    quat: [number, number, number, number],
  ) => {
    const i = bodyIndexByName.get(bodyName)
    if (i == null) return
    nodes[i].matrix.copy(restMatrix(pos, quat))
    nodes[i].matrixWorldNeedsUpdate = true
  }

  return { root, jointMap, initialJoints: scene.initialJoints, basePose, view: scene.view }
}
