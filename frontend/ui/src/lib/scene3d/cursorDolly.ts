// Shared-candidate code: this file imports only 'three' and its siblings in this directory -- keep it
// free of robovast imports (see README.md here).
//
// The wheel behaviour for an orbit-style 3D view: fly along the ray under the cursor, closing on the
// surface the gesture was aimed at. This is deliberately NOT what an orbit controller's own dolly
// does, and the difference is the whole point of the file -- see dollyToCursor.

import { PerspectiveCamera, Vector3 } from 'three'
import { MIN_PIVOT_M } from './pivot'

// Fraction of the pivot distance covered per pixel of wheel delta -- 15% at the ~100 px a notch
// reports.
const WHEEL_RATE = 0.0015

// A pivot distance near zero would make the relative step vanish -- the same crawl this function
// exists to avoid. Below this the step becomes absolute so the wheel always goes somewhere. It is
// also what lets the camera pass *through* the surface it is closing on: with a floor the approach
// terminates instead of converging.
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
 * Fly `camera` along the ray under the cursor, and report how far the orbit pivot now is.
 *
 * `pivotDistance` in, new pivot distance out -- the caller owns where the pivot actually sits (the
 * viewport keeps it dead ahead, `pivot.ts` `retargetPivot`). Passing the distance as a number rather
 * than reading it back off the target vector is what keeps the two ends of a gesture consistent: the
 * camera travels along the *cursor* ray while the pivot sits along the *view* axis, so a distance
 * re-derived from the geometry would shrink by an extra `cos(angle to the cursor)` every event, and
 * neither the step size nor the cancellation below would survive an off-centre cursor.
 *
 * The step is a fraction of that distance, so it shrinks as the camera closes on the surface the
 * gesture was aimed at and grows as it retreats: approach is asymptotic and a notch can never cross
 * the thing you are pointing at. This is what turns "15% of the view" into 15% of *the wall in front
 * of you* rather than 15% of the distance the world happened to be authored at.
 *
 * **This is radius-scaling toward a pivot, which is the shape an orbit controller's own dolly has and
 * which this file was originally written to avoid. That objection was real and does not apply here**,
 * for two reasons, both of them properties of the pivot rather than of the arithmetic:
 *
 *   - *The pivot is passable.* A geometric series converging on a fixed target is what makes an orbit
 *     controller freeze a short way out, with a building interior unreachable from a camera framed
 *     outside it. MIN_STEP_M floors the step, so the series terminates instead of converging: a few
 *     notches punch through the surface, and the next gesture anchors on whatever is behind it.
 *   - *It does not jump.* Anchoring on a hit point would twitch every time a wall crossed the pointer
 *     if it were sampled per event. It is sampled per gesture, so mid-scroll the anchor is frozen and
 *     a wall drifting across the cursor changes nothing.
 *
 * Scrolling out uses `f/(1 - f)` where scrolling in uses `f`, which is the exact inverse: a notch in
 * takes d to d(1 - f), and a notch out takes d(1 - f) back to d(1 - f) + d(1 - f)*f/(1 - f) = d. So in
 * and out still cancel exactly -- the property the original linear step was chosen for, kept in the
 * form a pivot left behind needs. (Outside MIN_STEP_M's floor, which trades the last few centimetres
 * for being able to pass through at all.)
 *
 * Mutates `camera.position` and the camera's world matrix in place. The caller owns calling
 * `preventDefault()` and moving the pivot to the returned distance.
 */
export function dollyToCursor(
  camera: PerspectiveCamera,
  pivotDistance: number,
  event: WheelEvent,
  domElement: HTMLElement,
): number {
  const fraction = -normalizeDelta(event) * WHEEL_RATE // > 0 scrolling forward
  if (fraction === 0) return pivotDistance

  // Forward shortens the pivot distance by `fraction`; backward has to lengthen it by the inverse of
  // that same step, or scrolling out would undo less than scrolling in put in.
  const rate = fraction > 0 ? fraction : fraction / (1 + fraction)
  let travel = pivotDistance * rate
  if (Math.abs(travel) < MIN_STEP_M) travel = Math.sign(fraction) * MIN_STEP_M

  camera.position.addScaledVector(cursorRay(camera, event, domElement), travel)
  // Never let the pivot reach zero: at zero the relative step vanishes and only MIN_STEP_M is left,
  // which is the crawl this whole file exists to avoid. Once the camera has passed through the
  // surface it keeps this much pivot ahead of it, until the next gesture measures a real one.
  return Math.max(pivotDistance - travel, MIN_PIVOT_M)
}
