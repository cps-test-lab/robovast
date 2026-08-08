// Shared-candidate code: this file imports only 'three' and its siblings in this directory -- keep it
// free of robovast imports (see README.md here).
//
// The wheel behaviour for an orbit-style 3D view: fly along the ray under the cursor, translating the
// eye and the orbit pivot together. This is deliberately NOT what an orbit controller's own dolly
// does, and the difference is the whole point of the file -- see dollyToCursor.

import { PerspectiveCamera, Vector3 } from 'three'

// Fraction of the pivot distance covered per pixel of wheel delta -- 15% at the ~100 px a notch
// reports. Linear in the delta, not exponential: an exponential is the right form for a *scale* (the
// 2D panels zoom their px-per-metre that way) but this is a translation, and exp() would make a notch
// out longer than a notch in, so scrolling in and back out would not return you where you started.
const WHEEL_RATE = 0.0015

// A pivot distance near zero would make the relative step vanish -- the same crawl this function
// exists to avoid. Below this the step becomes absolute so the wheel always goes somewhere.
const MIN_STEP_M = 0.05

// Beyond a notch or two per event the delta is a trackpad fling or a pinch, not an intent to travel
// that far; without a cap one gesture teleports the camera out of the world.
const MAX_DELTA_PX = 240

// deltaY is only in pixels for deltaMode 0. Firefox reports lines, and page-mode exists in the spec.
const PX_PER_LINE = 16
const PX_PER_PAGE = 100

/** Wheel delta in pixels, whatever unit the browser chose to report, capped at one gesture's worth. */
function normalizeDelta(event: WheelEvent): number {
  const scale =
    event.deltaMode === 1 ? PX_PER_LINE : event.deltaMode === 2 ? PX_PER_PAGE : 1
  const dy = event.deltaY * scale
  return Math.max(-MAX_DELTA_PX, Math.min(MAX_DELTA_PX, dy))
}

/**
 * Unit vector from the eye through the pixel under the cursor.
 *
 * `unproject` reads `camera.matrixWorld`, which a render loop refreshes once per frame -- but a
 * trackpad delivers several wheel events *within* one frame. Refresh it here, or the second event
 * unprojects against the previous pose while subtracting the current position: the two frames mix
 * into a ray pointing roughly back the way we came, and stacked events cancel out instead of
 * accumulating.
 */
function cursorRay(camera: PerspectiveCamera, event: WheelEvent, domElement: HTMLElement): Vector3 {
  camera.updateMatrixWorld()
  const rect = domElement.getBoundingClientRect()
  const ndcX = ((event.clientX - rect.left) / rect.width) * 2 - 1
  const ndcY = -((event.clientY - rect.top) / rect.height) * 2 + 1
  // z = 0.5 is any point on the ray in front of the near plane; unprojecting it and subtracting the
  // eye gives the direction, which is all we use -- the depth of the sample does not matter.
  return new Vector3(ndcX, ndcY, 0.5).unproject(camera).sub(camera.position).normalize()
}

/**
 * Fly `camera` along the ray under the cursor, carrying the orbit pivot `target` with it.
 *
 * The step is a *fraction of the current pivot distance*, so one notch covers the same fraction of
 * the view whether you are half a metre or fifty metres out. Translating both vectors by the same
 * delta leaves the orbit radius unchanged -- and with it the mouse-look pivot and an orbit
 * controller's distance-scaled pan speed.
 *
 * Scaling the radius instead -- what an orbit controller's own dolly does, pulling the eye toward a
 * fixed pivot -- is what stalls. The travel is then a geometric series converging on the pivot: close
 * in, a notch moves millimetres and the view appears frozen, and the pivot can never be passed
 * through, so an interior is unreachable from a viewpoint framed outside it.
 *
 * No raycast against the scene: flying *through* geometry is intended, and anchoring the step to a
 * hit point would make it jump every time a wall crossed the pointer.
 *
 * Mutates `camera.position`, `target` and the camera's world matrix in place. The caller owns calling
 * `preventDefault()` and letting its controller re-derive its own state from the moved vectors.
 */
export function dollyToCursor(
  camera: PerspectiveCamera,
  target: Vector3,
  event: WheelEvent,
  domElement: HTMLElement,
): void {
  const fraction = -normalizeDelta(event) * WHEEL_RATE // > 0 scrolling forward
  if (fraction === 0) return

  let travel = camera.position.distanceTo(target) * fraction
  if (Math.abs(travel) < MIN_STEP_M) travel = Math.sign(fraction) * MIN_STEP_M

  const dir = cursorRay(camera, event, domElement)
  camera.position.addScaledVector(dir, travel)
  target.addScaledVector(dir, travel)
}
