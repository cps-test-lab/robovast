// The pivot is the one number every mouse gesture in the viewport is scaled by -- an orbit
// controller's rotate arc is radius x angle, its pan speed is proportional to the same radius, and
// cursorDolly steps a fraction of it. So what is worth pinning here is not that a raycast can hit a
// box, but the two properties the viewport's feel rests on: that re-anchoring the pivot never moves
// the picture, and that measuring it always answers with something inside the world.
import { describe, expect, it } from 'vitest'
import {
  BoxGeometry,
  Group,
  Mesh,
  MeshBasicMaterial,
  PerspectiveCamera,
  Vector2,
  Vector3,
} from 'three'
import { MIN_PIVOT_M, frameObject, measureDepth, pickObject, retargetPivot } from './pivot'

const CENTRE = new Vector2(0, 0)

/** A camera at the origin looking down -z, three's default orientation. */
function camera(): PerspectiveCamera {
  const cam = new PerspectiveCamera(36, 16 / 9, 0.01, 1000)
  cam.position.set(0, 0, 0)
  cam.lookAt(0, 0, -1)
  cam.updateMatrixWorld()
  return cam
}

/** A unit box centred `z` metres in front of that camera, wrapped in a root like the loader's. */
function boxAt(z: number, size = 1): Group {
  const mesh = new Mesh(new BoxGeometry(size, size, size), new MeshBasicMaterial())
  mesh.position.set(0, 0, z)
  const root = new Group()
  root.add(mesh)
  root.updateMatrixWorld(true)
  return root
}

describe('measureDepth', () => {
  it('returns the distance to the surface under the cursor, not to its centre', () => {
    // A unit box centred 10 m out presents its near face at 9.5 m. Aiming at a wall and getting the
    // distance to the middle of the wall would put the pivot inside it.
    expect(measureDepth(camera(), boxAt(-10), CENTRE, 999, 100)).toBeCloseTo(9.5, 5)
  })

  it('takes the nearest surface when several are in line', () => {
    const root = boxAt(-30)
    root.add(boxAt(-4).children[0])
    root.updateMatrixWorld(true)
    expect(measureDepth(camera(), root, CENTRE, 999, 100)).toBeCloseTo(3.5, 5)
  })

  it('ignores geometry a panel has switched off', () => {
    // Raycaster tests layers, not `visible`, so without the guard a hidden marker group would be a
    // pivot the human cannot see.
    const root = boxAt(-4)
    root.children[0].visible = false
    root.add(boxAt(-30).children[0])
    root.updateMatrixWorld(true)
    expect(measureDepth(camera(), root, CENTRE, 999, 100)).toBeCloseTo(29.5, 5)
  })

  it('falls through to the ground plane where the geometry has a hole', () => {
    const cam = camera()
    cam.position.set(0, 5, 0)
    cam.lookAt(0, 0, -5) // 45 degrees down: the y=0 plane is 5*sqrt(2) away
    cam.updateMatrixWorld()
    expect(measureDepth(cam, null, CENTRE, 999, 100)).toBeCloseTo(Math.sqrt(50), 4)
  })

  it('leaves the pivot alone when aimed at the sky', () => {
    // Refusing to answer is better than inventing a distance, and returning `current` is "refuse"
    // spelled as a number.
    const cam = camera()
    cam.position.set(0, 5, 0)
    cam.lookAt(0, 20, -5) // upward: no geometry, and the ground plane is behind
    cam.updateMatrixWorld()
    expect(measureDepth(cam, null, CENTRE, 12.5, 100)).toBeCloseTo(12.5, 5)
  })

  it('clamps a surface against the lens, so pan and the wheel never crawl', () => {
    // near face at 0.19 m, well inside the floor
    expect(measureDepth(camera(), boxAt(-0.2, 0.02), CENTRE, 999, 100)).toBe(MIN_PIVOT_M)
  })

  it('clamps to the scene it is in, so a runaway fallback cannot leave the world', () => {
    const cam = camera()
    cam.position.set(0, 1000, 0)
    cam.lookAt(0, 0, -1000)
    cam.updateMatrixWorld()
    expect(measureDepth(cam, null, CENTRE, 999, 20)).toBe(80) // 4 x sceneRadius
  })
})

describe('retargetPivot', () => {
  it('moves the pivot without moving the picture', () => {
    // The whole reason this can run at the start of every gesture: only the eye and the view angles
    // reach the image, so re-spelling the pivot is a re-parameterisation and not a correction. Same
    // argument as roqsim.rendering.set_orbit_radius, which the native viewer relies on.
    const cam = camera()
    cam.position.set(3, 2, 7)
    cam.lookAt(-1, 0, 0)
    cam.updateMatrixWorld()
    const eye = cam.position.clone()
    const look = cam.getWorldDirection(new Vector3()).clone()

    const target = new Vector3(-1, 0, 0)
    retargetPivot(cam, target, 1.5)

    expect(cam.position.distanceTo(eye)).toBeCloseTo(0, 12)
    expect(cam.getWorldDirection(new Vector3()).angleTo(look)).toBeCloseTo(0, 12)
    expect(target.distanceTo(eye)).toBeCloseTo(1.5, 10)
    // ...and it is dead ahead, which is what stops OrbitControls re-centring it every frame.
    expect(target.clone().sub(eye).normalize().angleTo(look)).toBeCloseTo(0, 10)
  })
})

describe('frameObject', () => {
  it('frames the object without turning the camera', () => {
    // Framing by also rotating to a "good" angle moves two things at once, and a viewer who was
    // looking down a corridor arrives somewhere they cannot place.
    const cam = camera()
    const root = boxAt(-40, 2)
    const look = cam.getWorldDirection(new Vector3()).clone()

    const dest = frameObject(cam, root.children[0])!
    expect(dest.target.z).toBeCloseTo(-40, 6)
    // The destination is behind the target along the same view direction, at the framing distance.
    const back = dest.target.clone().sub(dest.position)
    expect(back.clone().normalize().angleTo(look)).toBeCloseTo(0, 10)

    // margin * r / sin(halfFov), with the narrower of the two fields governing.
    const halfFovY = (cam.fov / 2) * (Math.PI / 180)
    const halfFov = Math.min(halfFovY, Math.atan(Math.tan(halfFovY) * cam.aspect))
    expect(back.length()).toBeCloseTo((1.2 * Math.sqrt(3)) / Math.sin(halfFov), 4)
  })

  it('returns null for an object with no geometry to frame', () => {
    expect(frameObject(camera(), new Group())).toBeNull()
  })
})

describe('pickObject', () => {
  it('answers with the same surface a drag would have pivoted on, or nothing', () => {
    const root = boxAt(-10)
    expect(pickObject(camera(), root, CENTRE)).toBe(root.children[0])
    expect(pickObject(camera(), root, new Vector2(0.99, 0.99))).toBeNull()
    expect(pickObject(camera(), null, CENTRE)).toBeNull()
  })
})
