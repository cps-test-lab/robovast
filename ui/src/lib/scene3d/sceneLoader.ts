// Shared-candidate code: this file imports only 'three' -- keep it free of robovast imports so it
// can be extracted into a package other projects consume too (see README.md in this directory).
//
// Loads a scene descriptor (scene.json + scene.bin, produced by rst's `rst-export-web`
// or the MujocoSim adapter's ROBOSITO_SCENE_EXPORT_DIR hook) into a three.js Group, and returns a
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
  // Baked per-vertex texcoords, in MuJoCo's convention (v from the image's top row). Present only
  // when the source mesh carried UVs; when it is, it wins over the material's texrepeat/texuniform,
  // and buildGeometry's triplanar projection is not used.
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
  /** Every body name in the descriptor, i.e. every name `basePose` accepts. A consumer reports the
   *  names it could not resolve against this, so a motion source aimed at the wrong world is visible
   *  rather than rendering a plausible-looking lie. */
  bodies: string[]
  /** Every joint name `jointMap` accepts. The other half of the same check. */
  joints: string[]
  /** The descriptor's baked initial camera, passed through verbatim (absent when unauthored). */
  view?: SceneDescriptor['view']
  /**
   * Seat a body at a **world** pose, in the descriptor frame -- from a run capture's pose track or a
   * /tf transform. Works for any body: the pose is composed with the inverse of the parent's
   * root-relative transform, so a link deep in a kinematic chain lands where it belongs rather than at
   * that offset from its parent. Apply parents before children within a frame (the capture format
   * requires that ordering) so a child composes against its parent's new transform.
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
   * Release this scene's GPU resources (geometries, materials, textures).
   *
   * Needed because switching campaign switches *world*: the viewport frees whatever it is showing when
   * it is torn down, but replacing one scene with another inside a live viewport would otherwise leave
   * the previous world's buffers resident for as long as the tab is open.
   */
  dispose: () => void
}

// The descriptor quaternion is (w, x, y, z); three's Quaternion constructor takes (x, y, z, w).
function descQuat(q: readonly [number, number, number, number]): Quaternion {
  return new Quaternion(q[1], q[2], q[3], q[0])
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
    const materials = Array.isArray(obj.material) ? obj.material : [obj.material]
    for (const m of materials) {
      m.map?.dispose?.()
      m.dispose()
    }
  })
}

/** Compose a descriptor-frame pose (wxyz quat) into `out`, allocating nothing.
 *
 *  The hot path: basePose runs for every driven body on every animation frame, so the convenient
 *  `restMatrix` -- three fresh objects per call -- is the wrong tool there.
 */
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
  onLoad: () => void,
): Texture | null {
  if (!tex) return null
  let texture: Texture
  if ('file' in tex) {
    texture = loader.load(new URL(tex.file, baseUrl).href, onLoad)
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
  // Descriptor UVs are in MuJoCo's convention: v is measured from the image's TOP row. three's
  // default flipY=true would sample from the bottom. Setting it false also makes the two texture
  // paths agree -- DataTexture above already defaults to flipY=false.
  texture.flipY = false
  return texture
}

/** Resolve a descriptor texture at a given repeat, sharing one Texture per distinct repeat.
 *
 *  `texture.repeat` lives on the Texture, but the repeat a geom needs is a property of the *geom*,
 *  so a material shared across geoms of different size (rst_assets de-duplicates materials across
 *  prop copies, and a shelf puts one material on its boards and its legs) cannot share one Texture:
 *  each geom would overwrite the last one's repeat. Clones share the underlying Source, so the image
 *  is uploaded once regardless of how many variants exist.
 */
type TextureBank = (index: number, repeatU: number, repeatV: number) => Texture | null

/** The extent, in metres, that a primitive's built-in three UVs span.
 *
 *  Under `texuniform` MuJoCo's texrepeat is a per-metre period; three's primitives carry 0..1 UVs
 *  over the whole geom, so the repeat count is texrepeat x this span. Types whose tiling cannot be
 *  expressed by a single repeat (box, mesh) bake it into their UVs in buildGeometry and return 1.
 */
function planarSpan(geom: SceneGeom): [number, number] {
  const [sx, sy, sz] = geom.size
  switch (geom.type) {
    case 'plane':
      // Mirrors buildGeometry's substitution for an unbounded (size 0) plane.
      return [sx > 0 ? 2 * sx : 50, sy > 0 ? 2 * sy : 50]
    case 'sphere':
      // SphereGeometry is equirectangular: u wraps the circumference, v runs pole to pole.
      return [2 * Math.PI * sx, Math.PI * sx]
    case 'ellipsoid':
      return [Math.PI * (sx + sy), Math.PI * sz]
    case 'cylinder':
    case 'capsule':
      return [2 * Math.PI * sx, 2 * sy]
    default:
      return [1, 1]
  }
}

function buildMaterial(
  geom: SceneGeom,
  scene: SceneDescriptor,
  textureFor: TextureBank,
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
    // MuJoCo's mapping rule (spelled out in rst's textures.py): a mesh with baked UVs is mapped by
    // those UVs and *ignores* texrepeat/texuniform; anything else is projected, and texrepeat counts
    // tiles across the object (texuniform=false) or metres per tile (texuniform=true).
    //
    // buildGeometry bakes the tiling into the UVs wherever one repeat cannot express it -- every
    // mesh, and a texuniform box, whose six faces have different extents -- and those ask for 1 here.
    const baked = geom.type === 'mesh' || (geom.type === 'box' && mat.texuniform)
    const [spanU, spanV] = mat.texuniform ? planarSpan(geom) : [1, 1]
    const texture = baked
      ? textureFor(mat.texture, 1, 1)
      : textureFor(mat.texture, mat.texrepeat[0] * spanU, mat.texrepeat[1] * spanV)
    if (texture) {
      material.map = texture
      material.color.setRGB(1, 1, 1) // don't tint the texture
    }
  }
  return material
}

function buildGeometry(geom: SceneGeom, scene: SceneDescriptor, bin: ArrayBuffer): BufferGeometry {
  const [sx, sy, sz] = geom.size
  switch (geom.type) {
    case 'box': {
      const geometry = new BoxGeometry(2 * sx, 2 * sy, 2 * sz)
      const mat = geom.matid >= 0 ? scene.materials[geom.matid] : null
      if (mat && mat.texture >= 0 && mat.texuniform) {
        // texuniform means a per-metre period, but three gives all six faces the same 0..1 UV while
        // their extents differ -- one texture.repeat would tile a 0.05 x 0.05 x 2 m shelf leg's tall
        // sides like its tiny top. So scale each face's UVs by its own extent instead.
        // BoxGeometry emits faces in a fixed order, 4 vertices each; u runs along the first axis of
        // each pair below and v along the second.
        const faceSpans: [number, number][] = [
          [2 * sz, 2 * sy], [2 * sz, 2 * sy], // +x, -x
          [2 * sx, 2 * sz], [2 * sx, 2 * sz], // +y, -y
          [2 * sx, 2 * sy], [2 * sx, 2 * sy], // +z, -z
        ]
        const [ru, rv] = mat.texrepeat
        const uv = geometry.getAttribute('uv')
        for (let f = 0; f < 6; f++) {
          const [su, sv] = faceSpans[f]
          for (let k = 0; k < 4; k++) {
            const i = f * 4 + k
            uv.setXY(i, uv.getX(i) * su * ru, uv.getY(i) * sv * rv)
          }
        }
        uv.needsUpdate = true
      }
      return geometry
    }
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
        // Textured mesh with no baked UVs (e.g. a floorplan mesh): synthesize texcoords by a
        // per-face triplanar projection -- pick the axis the triangle faces most and project onto
        // the other two -- so vertical walls tile correctly instead of smearing. This reproduces
        // MuJoCo's auto-projection, which is what it applies to a UV-less mesh. Tiling is baked into
        // the UVs, so buildMaterial leaves texture.repeat at 1. Needs a non-indexed geometry.
        //
        // texuniform=true: texrepeat is metres per tile, so project the raw coordinate. Otherwise
        // MuJoCo scales the projection to the object, so normalise to 0..1 across the mesh first and
        // texrepeat counts tiles across it.
        const [ru, rv] = mat.texrepeat
        const uniform = mat.texuniform
        const lo = [Infinity, Infinity, Infinity]
        const hi = [-Infinity, -Infinity, -Infinity]
        if (!uniform) {
          for (let i = 0; i < verts.length; i++) {
            const axis = i % 3
            if (verts[i] < lo[axis]) lo[axis] = verts[i]
            if (verts[i] > hi[axis]) hi[axis] = verts[i]
          }
        }
        // A flat mesh has zero extent along one axis; guard the divide rather than emit NaN UVs.
        const span = [0, 1, 2].map((i) => Math.max(hi[i] - lo[i], 1e-9))
        const norm = (value: number, axis: number) =>
          uniform ? value : (value - lo[axis]) / span[axis]

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
            az >= ax && az >= ay
              ? [norm(v.x, 0), norm(v.y, 1)]
              : ax >= ay
                ? [norm(v.y, 1), norm(v.z, 2)]
                : [norm(v.x, 0), norm(v.z, 2)]
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
  // Clones made before the image arrives share the Source but carry their own upload state, so the
  // load callback has to re-mark them; a DataTexture has its pixels up front and needs none of this.
  const variants: Texture[][] = scene.textures.map(() => [])
  const baseTextures = scene.textures.map((t, i) =>
    buildTexture(t, bin, loader, baseUrl, () => {
      for (const v of variants[i]) v.needsUpdate = true
    }),
  )
  const byRepeat = new Map<string, Texture>()
  const textureFor: TextureBank = (index, repeatU, repeatV) => {
    const base = baseTextures[index]
    if (!base) return null
    if (repeatU === 1 && repeatV === 1) return base
    const key = `${index}:${repeatU}:${repeatV}`
    let texture = byRepeat.get(key)
    if (!texture) {
      texture = base.clone()
      texture.repeat.set(repeatU, repeatV)
      if (base.image) texture.needsUpdate = true
      variants[index].push(texture)
      byRepeat.set(key, texture)
    }
    return texture
  }

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
    const material = buildMaterial(geom, scene, textureFor)
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

  // basePose: seat a body at a **world** pose (a run capture's pose track, a /tf transform), in the
  // descriptor frame.
  //
  // A node's `matrix` is its transform relative to its parent, so a world pose has to be composed
  // with the inverse of the parent's world transform. Writing it straight into `matrix` -- as this did
  // -- is correct only while the parent *is* the world body, and silently wrong otherwise: a link fed
  // its own world pose renders at that offset from its parent instead of at it. That limited the whole
  // mechanism to world-parented bodies (a robot base, a free prop, a walker's mocap bones) without
  // saying so anywhere, and it is why an articulated robot could not be driven this way at all.
  //
  // Composing the parent instead removes the restriction: any body can be driven, and a ball-jointed
  // body is just another pose track rather than an unsupported case. The parent's world transform must
  // be current, which is why callers apply pose tracks parents-first (the capture format requires that
  // order, and MuJoCo's body order already satisfies it).
  // Scratch matrices: basePose runs per driven body per frame, so it allocates nothing.
  const relScratch = new Matrix4()
  const mulScratch = new Matrix4()
  const localScratch = new Matrix4()

  /** Transform of `node` relative to the scene root, from local matrices only.
   *
   *  Deliberately not `node.matrixWorld`: the viewport mounts this scene under a group rotated -PI/2
   *  about X to reach three's Y-up, so a world matrix carries that frame conversion and inverting it
   *  would cancel it -- placing every driven body in the wrong frame. Poses arrive in the *descriptor*
   *  frame, so the composition has to stay inside the descriptor's own subtree. Reading local matrices
   *  also removes any ordering dependency on three's world-matrix bookkeeping: a child composed later
   *  in the same frame sees its parent's freshly written `matrix`, not last frame's `matrixWorld`.
   */
  const relativeToRoot = (node: Object3D): Matrix4 => {
    relScratch.identity()
    for (let n: Object3D | null = node; n && n !== root; n = n.parent) {
      mulScratch.multiplyMatrices(n.matrix, relScratch)
      relScratch.copy(mulScratch)
    }
    return relScratch
  }

  const basePose = (
    bodyName: string,
    pos: ArrayLike<number>,
    quat: readonly [number, number, number, number],
  ) => {
    const i = bodyIndexByName.get(bodyName)
    if (i == null) return
    const node = nodes[i]
    composePose(localScratch, pos, quat)
    const parent = node.parent
    if (parent && parent !== root) {
      // local = (parent relative to root)^-1 * (desired, relative to root)
      node.matrix.multiplyMatrices(relativeToRoot(parent).invert(), localScratch)
    } else {
      // Directly under the root: the local matrix *is* the pose. The common case, and the only one
      // the previous implementation handled correctly.
      node.matrix.copy(localScratch)
    }
    node.matrixWorldNeedsUpdate = true
  }

  return {
    root,
    jointMap,
    initialJoints: scene.initialJoints,
    bodies: scene.bodies.map((b) => b.name),
    joints: Object.keys(jointMap),
    basePose,
    view: scene.view,
    dispose: () => {
      disposeSceneGraph(root)
      // A base texture only ever used through repeat-variant clones is on no material, so the graph
      // walk never reaches it.
      for (const t of baseTextures) t?.dispose()
    },
  }
}
