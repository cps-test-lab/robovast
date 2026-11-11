// The binary fixtures and the pixel arithmetic the rig needs, with nothing but node's own zlib:
// a PNG encoder (the camera frame), a PNG decoder (comparing canvas screenshots), and the glTF
// scene the run view's `scene3d` panel loads.

import { deflateSync, inflateSync } from 'node:zlib'

// -- PNG ---------------------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()

function crc32(buf) {
  let c = 0xffffffff
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(td))
  return Buffer.concat([len, td, crc])
}

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

/** An 8-bit RGB PNG of `width`×`height`, each pixel `pixel(x, y)` → `[r, g, b]`. */
export function encodePng(width, height, pixel) {
  const raw = Buffer.alloc((width * 3 + 1) * height)
  let o = 0
  for (let y = 0; y < height; y++) {
    raw[o++] = 0 // filter: none
    for (let x = 0; x < width; x++) {
      const [r, g, b] = pixel(x, y)
      raw[o++] = r
      raw[o++] = g
      raw[o++] = b
    }
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 2 // colour type: RGB
  return Buffer.concat([
    PNG_SIGNATURE,
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

/** Decode an 8-bit, non-interlaced RGB or RGBA PNG (what Chromium's screenshots are) into RGBA. */
export function decodePng(buf) {
  if (!buf.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error('not a PNG')
  let o = 8
  let width = 0
  let height = 0
  let colourType = 0
  const idat = []
  while (o < buf.length) {
    const len = buf.readUInt32BE(o)
    const type = buf.toString('ascii', o + 4, o + 8)
    const data = buf.subarray(o + 8, o + 8 + len)
    if (type === 'IHDR') {
      width = data.readUInt32BE(0)
      height = data.readUInt32BE(4)
      if (data[8] !== 8 || data[12] !== 0) throw new Error('only 8-bit non-interlaced PNGs are read')
      colourType = data[9]
    } else if (type === 'IDAT') idat.push(data)
    else if (type === 'IEND') break
    o += 12 + len
  }
  const bpp = colourType === 6 ? 4 : colourType === 2 ? 3 : 0
  if (!bpp) throw new Error(`PNG colour type ${colourType} is not read here`)
  const raw = inflateSync(Buffer.concat(idat))
  const stride = width * bpp
  const cur = Buffer.alloc(stride)
  const prev = Buffer.alloc(stride)
  const out = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)]
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1))
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? cur[i - bpp] : 0
      const b = prev[i]
      const c = i >= bpp ? prev[i - bpp] : 0
      let v = line[i]
      if (filter === 1) v += a
      else if (filter === 2) v += b
      else if (filter === 3) v += (a + b) >> 1
      else if (filter === 4) {
        const p = a + b - c
        const pa = Math.abs(p - a)
        const pb = Math.abs(p - b)
        const pc = Math.abs(p - c)
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c
      }
      cur[i] = v & 0xff
    }
    for (let x = 0; x < width; x++) {
      const d = (y * width + x) * 4
      out[d] = cur[x * bpp]
      out[d + 1] = cur[x * bpp + 1]
      out[d + 2] = cur[x * bpp + 2]
      out[d + 3] = bpp === 4 ? cur[x * bpp + 3] : 255
    }
    cur.copy(prev)
  }
  return { width, height, data: out }
}

/** The fraction of pixels that differ by more than `threshold` in any channel. Two images of
 *  different sizes are entirely different. */
export function changedFraction(a, b, threshold = 24) {
  if (a.width !== b.width || a.height !== b.height) return 1
  let changed = 0
  for (let i = 0; i < a.data.length; i += 4) {
    if (
      Math.abs(a.data[i] - b.data[i]) > threshold ||
      Math.abs(a.data[i + 1] - b.data[i + 1]) > threshold ||
      Math.abs(a.data[i + 2] - b.data[i + 2]) > threshold
    ) changed++
  }
  return changed / (a.width * a.height)
}

/** The centroid of the pixels `match(r, g, b, x, y)` accepts, or null when fewer than `min` do. */
export function centroidOf(img, match, min = 30) {
  let n = 0
  let sx = 0
  let sy = 0
  for (let y = 0; y < img.height; y++) {
    for (let x = 0; x < img.width; x++) {
      const i = (y * img.width + x) * 4
      if (match(img.data[i], img.data[i + 1], img.data[i + 2], x, y)) {
        n++
        sx += x
        sy += y
      }
    }
  }
  return n >= min ? { x: sx / n, y: sy / n, pixels: n } : null
}

// -- the camera frame --------------------------------------------------------------------------

/** A 320×240 picture of the boxes as a forward camera might see them: sky, floor, three blocks. */
export function cameraFramePng() {
  const W = 320
  const H = 240
  const horizon = 110
  const blocks = [
    { x0: 40, x1: 110, y0: 70, y1: 150, c: [200, 60, 50] },
    { x0: 150, x1: 190, y0: 90, y1: 135, c: [50, 110, 210] },
    { x0: 225, x1: 300, y0: 60, y1: 160, c: [60, 170, 90] },
  ]
  return encodePng(W, H, (x, y) => {
    for (const b of blocks) {
      if (x >= b.x0 && x < b.x1 && y >= b.y0 && y < b.y1) {
        const shade = x < b.x0 + (b.x1 - b.x0) * 0.3 ? 0.8 : 1
        return b.c.map((v) => Math.round(v * shade))
      }
    }
    if (y < horizon) {
      const t = y / horizon
      return [Math.round(150 + 60 * t), Math.round(190 + 40 * t), 235]
    }
    const checker = (Math.floor(x / 20) + Math.floor((y - horizon) / 12)) % 2
    return checker ? [120, 120, 125] : [140, 140, 145]
  })
}

// -- the scene -------------------------------------------------------------------------------

/** The world node's turn from Z-up onto glTF's Y-up, as the exporter writes it
 *  (src/lib/scene3d/gltfFixture.ts). */
const WORLD_TURN = [-Math.SQRT1_2, 0, 0, Math.SQRT1_2]

/** A camera on the robot looking along the body's +x with +z up: glTF cameras look down local -z
 *  with +y up, so local -z → body +x and local +y → body +z. */
const LOOK_FORWARD = [0.5, -0.5, -0.5, 0.5]

/** The bodies of the scene, in the world frame (Z-up, metres): a floor, four boxes of distinct
 *  colours at increasing depth along +x, and the robot that `sim_poses` moves. */
export const SCENE_BODIES = [
  { name: 'demo_crate_red', at: [2.5, 1.2, 0.5], size: [1, 1, 1], colour: [0.85, 0.15, 0.12] },
  { name: 'demo_crate_blue', at: [5, -1.6, 0.4], size: [0.8, 0.8, 0.8], colour: [0.12, 0.35, 0.9] },
  { name: 'demo_crate_green', at: [8, 1.8, 0.75], size: [1.5, 1.5, 1.5], colour: [0.15, 0.7, 0.25] },
  { name: 'demo_crate_yellow', at: [11.5, -1.2, 1], size: [2, 2, 2], colour: [0.95, 0.8, 0.1] },
]

export const ROBOT = 'demo_robot'
export const ROBOT_CAMERA = 'demo_robot.front_camera'

/** A unit cube centred on the origin: 24 vertices (a normal per face), 36 indices. */
function unitCube() {
  const faces = [
    { n: [1, 0, 0], u: [0, 1, 0], v: [0, 0, 1] },
    { n: [-1, 0, 0], u: [0, 0, 1], v: [0, 1, 0] },
    { n: [0, 1, 0], u: [0, 0, 1], v: [1, 0, 0] },
    { n: [0, -1, 0], u: [1, 0, 0], v: [0, 0, 1] },
    { n: [0, 0, 1], u: [1, 0, 0], v: [0, 1, 0] },
    { n: [0, 0, -1], u: [0, 1, 0], v: [1, 0, 0] },
  ]
  const pos = []
  const nrm = []
  const idx = []
  for (const f of faces) {
    const base = pos.length / 3
    for (const [su, sv] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      for (let k = 0; k < 3; k++) pos.push(0.5 * (f.n[k] + su * f.u[k] + sv * f.v[k]))
      nrm.push(...f.n)
    }
    idx.push(base, base + 1, base + 2, base, base + 2, base + 3)
  }
  return { pos: new Float32Array(pos), nrm: new Float32Array(nrm), idx: new Uint16Array(idx) }
}

/** The scene as one binary glTF (`scene.glb`), to the contract of docs/simulators.rst
 *  ("The scene"): bodies as nodes carrying `extras.body`/`body_id`, the world node turned Z-up,
 *  each drawn shape a child node of its body, a sensor camera on the robot, and the scene's
 *  `extras` stating `roqsim.scene` version 1 with an initial view. */
export function sceneGlb() {
  const cube = unitCube()
  const bin = Buffer.concat([
    Buffer.from(cube.pos.buffer),
    Buffer.from(cube.nrm.buffer),
    Buffer.from(cube.idx.buffer),
  ])
  const padded = Buffer.concat([bin, Buffer.alloc((4 - (bin.length % 4)) % 4)])

  const materials = []
  const meshes = []
  const meshFor = (name, colour, roughness = 0.7) => {
    materials.push({
      name,
      pbrMetallicRoughness: { baseColorFactor: [...colour, 1], metallicFactor: 0, roughnessFactor: roughness },
    })
    meshes.push({
      name,
      primitives: [{ attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, material: materials.length - 1 }],
    })
    return meshes.length - 1
  }

  const nodes = []
  const add = (node, parent) => {
    nodes.push(node)
    const i = nodes.length - 1
    if (parent !== undefined) (nodes[parent].children ??= []).push(i)
    return i
  }
  let bodyId = 0
  const body = (name, parent, translation, extra = {}) =>
    add({ name, ...(translation ? { translation } : {}), extras: { body: name, body_id: bodyId++, ...extra } }, parent)
  const shape = (name, parent, mesh, scale, translation) =>
    add({ name, mesh, scale, ...(translation ? { translation } : {}) }, parent)

  const world = body('world', undefined, undefined)
  nodes[world].rotation = WORLD_TURN
  shape('floor', world, meshFor('floor', [0.55, 0.56, 0.6], 0.95), [26, 12, 0.02], [6, 0, -0.01])
  for (const b of SCENE_BODIES) {
    const i = body(b.name, world, b.at)
    shape(`${b.name}_box`, i, meshFor(b.name, b.colour), b.size)
  }
  const robot = body(ROBOT, world, [0, 0, 0.15], {
    joints: [{ name: 'demo_robot_root', type: 'free', axis: [0, 0, 1], pos: [0, 0, 0] }],
  })
  shape('demo_robot_chassis', robot, meshFor('demo_robot_chassis', [0.95, 0.45, 0.1], 0.5), [0.6, 0.4, 0.2])
  shape('demo_robot_mast', robot, meshFor('demo_robot_mast', [0.2, 0.2, 0.22], 0.5), [0.08, 0.08, 0.3], [0.2, 0, 0.25])
  const camNode = add({
    name: ROBOT_CAMERA,
    translation: [0.25, 0, 0.4],
    rotation: LOOK_FORWARD,
    camera: 0,
    extras: { sensor: ROBOT_CAMERA, kind: 'rgb', width: 320, height: 240 },
  }, robot)
  void camNode

  const gltf = {
    asset: { version: '2.0', generator: 'robovast e2e fixture' },
    scene: 0,
    scenes: [{
      nodes: [world],
      extras: {
        format: 'roqsim.scene',
        version: 1,
        initialJoints: {},
        view: { lookat: [5, 0, 0.5], distance: 15, azimuth: 210, elevation: -28 },
      },
    }],
    nodes,
    meshes,
    materials,
    cameras: [{ name: ROBOT_CAMERA, type: 'perspective', perspective: { yfov: 1.0, aspectRatio: 4 / 3, znear: 0.05, zfar: 100 } }],
    buffers: [{ byteLength: padded.length }],
    bufferViews: [
      { buffer: 0, byteOffset: 0, byteLength: cube.pos.byteLength, target: 34962 },
      { buffer: 0, byteOffset: cube.pos.byteLength, byteLength: cube.nrm.byteLength, target: 34962 },
      { buffer: 0, byteOffset: cube.pos.byteLength + cube.nrm.byteLength, byteLength: cube.idx.byteLength, target: 34963 },
    ],
    accessors: [
      { bufferView: 0, componentType: 5126, count: 24, type: 'VEC3', min: [-0.5, -0.5, -0.5], max: [0.5, 0.5, 0.5] },
      { bufferView: 1, componentType: 5126, count: 24, type: 'VEC3' },
      { bufferView: 2, componentType: 5123, count: 36, type: 'SCALAR' },
    ],
  }
  let json = Buffer.from(JSON.stringify(gltf), 'utf8')
  json = Buffer.concat([json, Buffer.alloc((4 - (json.length % 4)) % 4, 0x20)])
  const header = Buffer.alloc(12)
  header.writeUInt32LE(0x46546c67, 0) // 'glTF'
  header.writeUInt32LE(2, 4)
  header.writeUInt32LE(12 + 8 + json.length + 8 + padded.length, 8)
  const jsonHead = Buffer.alloc(8)
  jsonHead.writeUInt32LE(json.length, 0)
  jsonHead.writeUInt32LE(0x4e4f534a, 4) // 'JSON'
  const binHead = Buffer.alloc(8)
  binHead.writeUInt32LE(padded.length, 0)
  binHead.writeUInt32LE(0x004e4942, 4) // 'BIN\0'
  return Buffer.concat([header, jsonHead, json, binHead, padded])
}
