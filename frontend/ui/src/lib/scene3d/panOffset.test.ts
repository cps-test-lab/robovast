// A two-finger pan carries the point at the pivot distance along with the fingers.
import { PerspectiveCamera, Vector3 } from 'three'
import { describe, expect, it } from 'vitest'
import { panOffset } from './viewport'

/** Where a world point lands on a `width` x `height` canvas, in CSS pixels from its top-left. */
function onScreen(camera: PerspectiveCamera, point: Vector3, width: number, height: number) {
  camera.updateMatrixWorld()
  const ndc = point.clone().project(camera)
  return { x: ((ndc.x + 1) / 2) * width, y: ((1 - ndc.y) / 2) * height }
}

describe('panOffset', () => {
  it('keeps the point at the pivot distance under the fingers', () => {
    const width = 400
    const height = 300
    const camera = new PerspectiveCamera(36, width / height, 0.01, 100)
    camera.position.set(1, 2, 6)
    camera.lookAt(1, 0, 0)
    camera.updateMatrixWorld()
    const distance = 6.3
    const pivot = camera.position.clone().addScaledVector(camera.getWorldDirection(new Vector3()), distance)
    const before = onScreen(camera, pivot, width, height)

    camera.position.add(panOffset(camera, distance, height, 30, -12))
    const after = onScreen(camera, pivot, width, height)

    expect(after.x - before.x).toBeCloseTo(30, 6)
    expect(after.y - before.y).toBeCloseTo(-12, 6)
  })

  it('scales with the pivot distance: twice as far, twice the travel for the same fingers', () => {
    const camera = new PerspectiveCamera(36, 1, 0.01, 100)
    camera.position.set(0, 0, 5)
    camera.lookAt(0, 0, 0)
    const near = panOffset(camera, 2, 500, 40, 0).length()
    const far = panOffset(camera, 4, 500, 40, 0).length()
    expect(far / near).toBeCloseTo(2, 9)
  })
})
