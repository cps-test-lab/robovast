// Sensor cameras from a scene descriptor: what is read, what is refused, and that a loaded camera
// looks where the sensor looked and follows its body.
import { MathUtils, Vector3 } from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  INFINITE_FAR_RATIO,
  buildSceneCamera,
  letterbox,
  parseSceneCameras,
  viewpoints,
  type SceneCameraEntry,
} from './sceneCameras'
import { loadScene } from './sceneLoader'

const URL_ = '/campaigns/c/scene_assets/k/scene.json'

/** A camera mounted the way a forward-looking robot camera is: MuJoCo's `xyaxes="0 -1 0 0 0 1"`,
 *  image right = body -y, image up = body +z, so it looks along the body's +x. */
const FORWARD: [number, number, number, number] = [-0.5, -0.5, 0.5, 0.5]

const ENTRY: SceneCameraEntry = {
  name: 'robot.oakd.oakd_camera',
  body: 1,
  pos: [0.1, 0, 0.2],
  quat: FORWARD,
  perspective: { yfov: 0.9920451468335769, aspectRatio: 4 / 3, znear: 0.05, zfar: 250 },
  kind: 'rgb',
  width: 320,
  height: 240,
}

const DESCRIPTOR = {
  format: 'roqsim.web_scene',
  version: 1,
  up: 'z',
  bodies: [
    { name: 'world', parent: 0, pos: [0, 0, 0], quat: [1, 0, 0, 0] },
    { name: 'base_link', parent: 0, pos: [0, 0, 0], quat: [1, 0, 0, 0] },
  ],
  joints: [],
  initialJoints: {},
  geoms: [],
  meshes: [],
  materials: [],
  textures: [],
  cameras: [ENTRY, { ...ENTRY, name: 'robot.depth', kind: 'depth' }],
}

const close = (a: Vector3, b: [number, number, number]) =>
  a.toArray().forEach((v, i) => expect(v).toBeCloseTo(b[i], 9))

describe('parseSceneCameras', () => {
  it('reads no cameras from a descriptor that places none', () => {
    expect(parseSceneCameras(undefined, 2, URL_)).toEqual([])
  })

  it('reads an entry as stated', () => {
    expect(parseSceneCameras([ENTRY], 2, URL_)).toEqual([ENTRY])
  })

  it.each([
    [{ ...ENTRY, body: 2 }, /body 2/],
    [{ ...ENTRY, name: '' }, /no name/],
    [{ ...ENTRY, quat: [1, 0, 0] }, /quat/],
    [{ ...ENTRY, perspective: { ...ENTRY.perspective, yfov: 4 } }, /yfov/],
    [{ ...ENTRY, perspective: { ...ENTRY.perspective, zfar: 0.01 } }, /zfar/],
    [{ ...ENTRY, width: 0 }, /width/],
  ])('refuses an entry it cannot place, naming it', (entry, why) => {
    expect(() => parseSceneCameras([entry], 2, URL_)).toThrow(why)
    expect(() => parseSceneCameras([entry], 2, URL_)).toThrow(/cameras\[0\]/)
  })
})

describe('buildSceneCamera', () => {
  it('projects as the sensor does', () => {
    const { camera, aspect, projectionAspect, kind } = buildSceneCamera(ENTRY)
    expect(camera.fov).toBeCloseTo(MathUtils.radToDeg(ENTRY.perspective.yfov), 12)
    expect(camera.aspect).toBeCloseTo(4 / 3, 12)
    expect([camera.near, camera.far]).toEqual([0.05, 250])
    expect(aspect).toBeCloseTo(320 / 240, 12)
    expect(projectionAspect).toBeCloseTo(4 / 3, 12)
    expect(kind).toBe('rgb')
    expect(camera.name).toBe(ENTRY.name)
  })

  it('reads an absent zfar as glTF does, as far away', () => {
    const { perspective } = ENTRY
    const { camera } = buildSceneCamera({ ...ENTRY, perspective: { ...perspective, zfar: undefined } })
    expect(camera.far).toBe(perspective.znear * INFINITE_FAR_RATIO)
  })

  it('takes the shape of the canvas when nothing states one', () => {
    const { width: _w, height: _h, ...bare } = ENTRY
    const { aspect } = buildSceneCamera({
      ...bare,
      perspective: { yfov: 1, znear: 0.1 },
    })
    expect(aspect).toBeUndefined()
  })

  it('looks down its -z with +y up, which for this mount is the body +x', () => {
    const { camera } = buildSceneCamera({ ...ENTRY, pos: [0, 0, 0] })
    camera.updateMatrixWorld(true)
    close(camera.getWorldDirection(new Vector3()), [1, 0, 0])
    close(camera.up.clone().applyQuaternion(camera.quaternion), [0, 0, 1])
  })
})

describe('viewpoints', () => {
  it('offers the cameras that see colour, reading an unstated kind as colour', () => {
    const cams = [
      buildSceneCamera(ENTRY),
      buildSceneCamera({ ...ENTRY, name: 'd', kind: 'depth' }),
      buildSceneCamera({ ...ENTRY, name: 'plain', kind: undefined }),
    ]
    expect(viewpoints(cams).map((c) => c.name)).toEqual([ENTRY.name, 'plain'])
  })
})

describe('letterbox', () => {
  it('fits the picture inside the canvas, centred', () => {
    expect(letterbox(800, 300, 4 / 3)).toEqual({ x: 200, y: 0, width: 400, height: 300 })
    expect(letterbox(400, 600, 4 / 3)).toEqual({ x: 0, y: 150, width: 400, height: 300 })
  })
})

describe('loadScene cameras', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('puts each camera on its body, where it follows the body', async () => {
    vi.stubGlobal('window', { location: { href: 'http://example.test/' } })
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => DESCRIPTOR,
      arrayBuffer: async () => new ArrayBuffer(0),
    })))
    const model = await loadScene(URL_)
    expect(model.cameras.map((c) => [c.name, c.kind])).toEqual([
      [ENTRY.name, 'rgb'],
      ['robot.depth', 'depth'],
    ])
    const { camera } = model.cameras[0]
    expect(camera.parent?.name).toBe('base_link')

    // Drive the body: half a turn about z, 2 m along x. The camera rides 0.1 m ahead of it, now
    // behind, and looks back along -x.
    model.basePose('base_link', [2, 0, 0], [0, 0, 0, 1])
    model.root.updateMatrixWorld(true)
    close(camera.getWorldPosition(new Vector3()), [1.9, 0, 0.2])
    close(camera.getWorldDirection(new Vector3()), [-1, 0, 0])
  })
})
