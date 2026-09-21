// `reset` is what makes a loaded model reusable for another run: whatever the last run's motion did,
// the model has to come back exactly as `loadScene` returned it.
import { Matrix4, Object3D } from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadScene } from './sceneLoader'

const DESCRIPTOR = {
  up: 'z',
  bodies: [
    { name: 'world', parent: 0, pos: [0, 0, 0], quat: [1, 0, 0, 0] },
    { name: 'base', parent: 0, pos: [1, 0, 0], quat: [1, 0, 0, 0] },
    { name: 'arm', parent: 1, pos: [0, 0, 0.5], quat: [1, 0, 0, 0] },
  ],
  joints: [{ name: 'shoulder', body: 2, type: 'hinge', axis: [0, 0, 1], pos: [0, 0, 0], qposadr: 0 }],
  initialJoints: { shoulder: 0.3 },
  geoms: [],
  meshes: [],
  materials: [],
  textures: [],
}

afterEach(() => vi.unstubAllGlobals())

function matrixOf(root: Object3D, name: string): Matrix4 {
  return root.getObjectByName(name)!.matrix.clone()
}

describe('SceneModel.reset', () => {
  it('returns every body and joint to the pose the model was loaded in', async () => {
    vi.stubGlobal('window', { location: { href: 'http://example.test/' } })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
      ok: true,
      json: async () => DESCRIPTOR,
      arrayBuffer: async () => new ArrayBuffer(0),
      url,
    })))
    const model = await loadScene('/campaigns/c/scene_assets/k/scene.json')
    const base0 = matrixOf(model.root, 'base')
    const arm0 = matrixOf(model.root, 'arm')

    model.basePose('base', [4, 5, 6], [0, 0, 0, 1])
    model.jointMap.shoulder(1.2)
    expect(matrixOf(model.root, 'base').equals(base0)).toBe(false)
    expect(matrixOf(model.root, 'arm').equals(arm0)).toBe(false)

    model.reset()
    expect(matrixOf(model.root, 'base').equals(base0)).toBe(true)
    expect(matrixOf(model.root, 'arm').equals(arm0)).toBe(true)
  })
})
