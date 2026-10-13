// The scene model of a glTF scene: bodies by name, joints from the body extras moving bodies as the
// simulator does, the root in the world's Z-up frame, and `reset` returning every body to the file's
// pose so one model can serve a second run.
import { Matrix4, Object3D, Quaternion, Vector3 } from 'three'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchOf, gltfScene } from './gltfFixture'
import { loadScene } from './sceneLoader'

const URL_ = '/campaigns/c/scene_assets/k/scene.glb'

/** The arm's hinge sits at 0.3 in the file, about z through (0.1, 0, 0), with a reference of 0.2. */
const HINGE = { name: 'shoulder', type: 'hinge', axis: [0, 0, 1], pos: [0.1, 0, 0], ref: 0.2 }

/** The arm's pose relative to its parent when the hinge is at `q`: its pose at the reference, then
 *  the turn by `q - ref` about the hinge's axis through its anchor. */
function armAt(q: number): Matrix4 {
  const rest = new Matrix4().makeTranslation(0, 0, 0.5)
  const [x, y, z] = HINGE.pos
  return rest
    .multiply(new Matrix4().makeTranslation(x, y, z))
    .multiply(new Matrix4().makeRotationZ(q - HINGE.ref))
    .multiply(new Matrix4().makeTranslation(-x, -y, -z))
}

function scene() {
  const filed = armAt(0.3)
  const t = new Vector3()
  const r = new Quaternion()
  filed.decompose(t, r, new Vector3())
  return gltfScene(
    [
      { name: 'world', parent: 0 },
      { name: 'base', parent: 0, translation: [1, 0, 0] },
      { name: 'arm', parent: 1, translation: t.toArray(), rotation: r.toArray() as [number, number, number, number], joints: [HINGE] },
      { name: 'robot/tip: [1]', parent: 2, translation: [0, 0, 0.4] },
    ],
    { format: 'roqsim.scene', version: 1, initialJoints: { shoulder: 0.3 }, view: { distance: 4 } },
  )
}

function matrixOf(root: Object3D, name: string): Matrix4 {
  return root.getObjectByName(name)!.matrix.clone()
}

async function load() {
  vi.stubGlobal('window', { location: { href: 'http://example.test/' } })
  vi.stubGlobal('fetch', vi.fn(fetchOf(scene())))
  return loadScene(URL_)
}

afterEach(() => vi.unstubAllGlobals())

describe('loadScene', () => {
  it('names every body as the scene does and roots them in the world frame', async () => {
    const model = await load()
    expect(model.bodies).toEqual(['world', 'base', 'arm', 'robot/tip: [1]'])
    expect(model.joints).toEqual(['shoulder'])
    expect(model.initialJoints).toEqual({ shoulder: 0.3 })
    expect(model.view).toEqual({ distance: 4 })
    expect(model.frameNode()?.name).toBe('world')
    expect(model.frameNode('robot/tip: [1]')?.name).toBe('robot/tip: [1]')
    // The file's turn onto Y-up is taken off: the world node is the identity, so the root is Z-up.
    expect(model.frameNode()!.matrix.equals(new Matrix4())).toBe(true)
  })

  it('moves a body by its hinge as the simulator does, about the anchor, from the reference', async () => {
    const model = await load()
    const close = (a: Matrix4, b: Matrix4) =>
      a.elements.forEach((v, i) => expect(v).toBeCloseTo(b.elements[i], 6))
    close(matrixOf(model.root, 'arm'), armAt(0.3))
    model.jointMap.shoulder(-1.1)
    close(matrixOf(model.root, 'arm'), armAt(-1.1))
  })

  it('seats a body at a world pose whatever its parent', async () => {
    const model = await load()
    model.basePose('base', [2, 0, 0], [Math.SQRT1_2, 0, 0, Math.SQRT1_2]) // a quarter turn about z
    model.basePose('robot/tip: [1]', [5, 6, 7], [1, 0, 0, 0])
    model.root.updateMatrixWorld(true)
    const tip = model.frameNode('robot/tip: [1]')!.getWorldPosition(new Vector3())
    tip.toArray().forEach((v, i) => expect(v).toBeCloseTo([5, 6, 7][i], 9))
  })

  it('resets every body and joint to the pose the model was loaded in', async () => {
    const model = await load()
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
