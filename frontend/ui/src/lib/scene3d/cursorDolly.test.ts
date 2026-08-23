// The wheel's two load-bearing properties, both of them answers to a specific way this has gone wrong
// before:
//
//   * a notch out undoes a notch in, exactly -- the reason the step was originally linear, and the
//     thing the fraction-of-a-pivot form has to keep;
//   * the pivot can be passed through -- the reason an orbit controller's own dolly was rejected, and
//     the objection this form has to keep answering now that it scales a radius again.
//
// Neither is visible in a screenshot and both are what "flying feels right" is made of.
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera, Vector3 } from 'three'
import { dollyToCursor } from './cursorDolly'
import { MIN_PIVOT_M } from './pivot'

/** A 800x450 canvas stub -- `cursorRay` only ever asks for its bounding rect. */
const CANVAS = {
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 450 }),
} as unknown as HTMLElement

/** A wheel event at a canvas pixel. Positive `deltaY` is a notch *out*, as browsers report it. */
function wheel(deltaY: number, x = 400, y = 225): WheelEvent {
  return { deltaY, deltaMode: 0, clientX: x, clientY: y } as WheelEvent
}

function camera(): PerspectiveCamera {
  const cam = new PerspectiveCamera(36, 800 / 450, 0.01, 1000)
  cam.position.set(0, 0, 0)
  cam.lookAt(0, 0, -1)
  cam.updateMatrixWorld()
  return cam
}

const NOTCH = 100 // px, what one detent of a real wheel reports

describe('dollyToCursor', () => {
  it('steps a fraction of the pivot distance, so one notch means the same at any scale', () => {
    // The complaint this whole change answers: a step in absolute metres is right for one world size
    // and wrong for every other. 15% of the view at 46 m and at 0.6 m.
    for (const d of [46, 0.6]) {
      const cam = camera()
      const left = dollyToCursor(cam, d, wheel(-NOTCH), CANVAS)
      expect(cam.position.length()).toBeCloseTo(0.15 * d, 6)
      expect(left).toBeCloseTo(0.85 * d, 6)
    }
  })

  it('returns the camera exactly where it started when a notch out follows a notch in', () => {
    const cam = camera()
    const start = cam.position.clone()

    const near = dollyToCursor(cam, 46, wheel(-NOTCH), CANVAS)
    const back = dollyToCursor(cam, near, wheel(NOTCH), CANVAS)

    expect(cam.position.distanceTo(start)).toBeCloseTo(0, 9)
    expect(back).toBeCloseTo(46, 6)
  })

  it('cancels exactly with the cursor off-centre too', () => {
    // The reason the pivot distance is passed as a number instead of read back off the target: the
    // camera travels along the cursor ray while the pivot stays on the view axis, and a distance
    // re-derived from those two vectors loses a cos(angle) every event.
    const cam = camera()
    const start = cam.position.clone()

    const near = dollyToCursor(cam, 46, wheel(-NOTCH, 740, 40), CANVAS)
    const back = dollyToCursor(cam, near, wheel(NOTCH, 740, 40), CANVAS)

    expect(cam.position.distanceTo(start)).toBeCloseTo(0, 9)
    expect(back).toBeCloseTo(46, 6)
  })

  it('flies toward what the cursor is on, not down the view axis', () => {
    const cam = camera()
    dollyToCursor(cam, 10, wheel(-NOTCH, 740, 40), CANVAS)
    expect(cam.position.x).toBeGreaterThan(0) // cursor right of centre
    expect(cam.position.y).toBeGreaterThan(0) // and above it
  })

  it('approaches a surface without a single notch crossing it', () => {
    // The failure the fixed step produced in a building: 15% of a pivot 46 m away is 7 m a notch, so
    // one notch aimed at a wall 2 m off put the camera inside it.
    const cam = camera()
    let pivot = 46
    let notches = 0
    while (pivot > MIN_PIVOT_M) {
      const before = pivot
      pivot = dollyToCursor(cam, pivot, wheel(-NOTCH), CANVAS)
      expect(pivot).toBeLessThan(before)
      // The surface is 46 m down -z; the camera is still short of it every step of the way.
      expect(-cam.position.z).toBeLessThan(46)
      notches += 1
    }
    // It takes tens of notches to arrive, not one -- and the last ones are centimetres.
    expect(notches).toBeGreaterThan(20)
    expect(pivot).toBe(MIN_PIVOT_M)
  })

  it('still passes through, so an interior is reachable from outside it', () => {
    // The half of the old objection to radius-scaling that survives: a geometric series converging on
    // its target can never be crossed. MIN_STEP_M is what makes this one terminate instead.
    const cam = camera()
    let pivot = 46
    for (let i = 0; i < 200; i += 1) pivot = dollyToCursor(cam, pivot, wheel(-NOTCH), CANVAS)
    expect(cam.position.z).toBeLessThan(-46) // past where the surface was
  })

  it('reads a wheel that reports lines or pages, not only pixels', () => {
    const lines = camera()
    dollyToCursor(lines, 10, { ...wheel(-6), deltaMode: 1 } as WheelEvent, CANVAS)
    expect(lines.position.length()).toBeCloseTo(0.15 * 10 * (96 / 100), 6)

    const pages = camera()
    dollyToCursor(pages, 10, { ...wheel(-1), deltaMode: 2 } as WheelEvent, CANVAS)
    expect(pages.position.length()).toBeCloseTo(0.15 * 10, 6)
  })

  it('caps a trackpad fling, which is a gesture and not an intent to travel that far', () => {
    const cam = camera()
    dollyToCursor(cam, 46, wheel(-4000), CANVAS)
    expect(cam.position.length()).toBeCloseTo(0.0015 * 240 * 46, 6) // MAX_DELTA_PX, not 4000
  })

  it('does nothing on a zero delta', () => {
    const cam = camera()
    expect(dollyToCursor(cam, 12, wheel(0), CANVAS)).toBe(12)
    expect(cam.position.equals(new Vector3())).toBe(true)
  })
})
