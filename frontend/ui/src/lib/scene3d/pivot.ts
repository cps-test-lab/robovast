// Shared-candidate code: this file imports only 'three' and its siblings in this directory -- keep it
// free of robovast imports (see README.md here).
//
// Where the orbit pivot goes. One idea, and every mouse gesture in the viewport rests on it: the pivot
// is a point on the surface you are aiming at, re-chosen when a gesture starts.
//
// It matters because an orbit controller scales *everything* by the pivot distance -- the arc a rotate
// swings the eye through is radius x angle, its pan speed is |eye - target| * tan(fov/2) per pixel, and
// this directory's wheel (cursorDolly.ts) steps a fixed fraction of the same number. So one distance
// decides how all three feel, and a viewport that never re-measures it inherits whatever the world was
// authored with: 46 m for the robolab building, 3.2 m for a tabletop. At the opening frame that number
// is right by construction. Ten metres of travel later it is describing somewhere the camera no longer
// is, and a drag meant to look around a corridor swings the eye through the wall instead.

import { Box3, Object3D, PerspectiveCamera, Raycaster, Sphere, Vector2, Vector3 } from 'three'

// The pivot never comes closer than this. A surface pressed against the lens would otherwise take pan
// and the wheel to a standstill -- the same crawl at the other end of the range from the one this
// module exists to fix. Exported because the wheel holds the same floor as it closes in.
export const MIN_PIVOT_M = 0.25

// ...nor further than this multiple of the scene's own radius. Aimed at the sky (or at nothing, in a
// scene that has not loaded) the fallbacks below can hand back something unbounded; a pivot outside
// the world is a pivot no gesture can be scaled by.
const MAX_PIVOT_SCENE_RADII = 4

// Ground plane in three's frame. Scene content is authored Z-up and lives under the viewport's
// up-rotated wrapper, so the world's z = 0 floor is this y = 0 plane.
const GROUND_Y = 0

const _raycaster = new Raycaster()
const _forward = new Vector3()
const _box = new Box3()
const _sphere = new Sphere()

/** Whether `object` and every ancestor up to the root is visible.
 *
 *  `Raycaster` does not check this -- it tests layers, not `visible` -- so a marker group the panel has
 *  switched off would otherwise be a perfectly good pivot the user cannot see. */
function shown(object: Object3D): boolean {
  for (let node: Object3D | null = object; node; node = node.parent) {
    if (!node.visible) return false
  }
  return true
}

/** Unit direction the camera is looking, in world coordinates. */
function forward(camera: PerspectiveCamera): Vector3 {
  return camera.getWorldDirection(_forward)
}

/**
 * How far the surface under `ndc` is from the eye, for use as the pivot distance.
 *
 * Three answers, in order, so this always returns something usable:
 *
 *   1. what the cursor ray hits in `root` -- the answer we want, and the reason the pivot tracks the
 *      world instead of the world's opening camera;
 *   2. failing that, where the ray meets the ground plane ahead of the eye, which is what "aim at the
 *      floor across the room" should mean even where the floor is a hole in the geometry;
 *   3. failing that -- aimed at the sky, or at a scene that has not loaded -- `current`, unchanged.
 *      Refusing to answer is better than inventing a distance, and leaving the pivot alone is exactly
 *      "refuse" spelled as a number.
 *
 * `root` and not the whole `Scene`: the ground grid is a sibling of the scene root, and a pivot on
 * the grid is a pivot on a decoration.
 *
 * Cast once per *gesture*, never per frame -- `Raycaster` is brute force over triangles (three
 * sphere-culls each mesh first, which is what makes a 161-object building affordable at all).
 */
export function measureDepth(
  camera: PerspectiveCamera,
  root: Object3D | null,
  ndc: Vector2,
  current: number,
  sceneRadius: number,
): number {
  // `setFromCamera` reads `camera.matrixWorld`, which the render loop refreshes once per frame -- but
  // a gesture can start between two frames, and a trackpad delivers several wheel events inside one.
  // Same refresh, and the same reason, as cursorDolly's cursorRay.
  camera.updateMatrixWorld()
  _raycaster.setFromCamera(ndc, camera)
  const max = Math.max(MIN_PIVOT_M, MAX_PIVOT_SCENE_RADII * sceneRadius)
  const clamp = (d: number) => Math.min(Math.max(d, MIN_PIVOT_M), max)

  if (root) {
    for (const hit of _raycaster.intersectObject(root, true)) {
      if (shown(hit.object)) return clamp(hit.distance)
    }
  }

  const dir = _raycaster.ray.direction
  const t = Math.abs(dir.y) > 1e-6 ? (GROUND_Y - camera.position.y) / dir.y : -1
  if (t > 0) return clamp(t)

  return clamp(current)
}

/**
 * Put `target` `distance` metres in front of the eye, along the direction the camera already looks.
 * **The rendered image does not change.**
 *
 * Only the eye and the view angles reach the picture; an orbit controller's target is bookkeeping for
 * where the eye swings *around*, and nothing draws it. So this is a re-parameterisation rather than a
 * correction -- which is what lets it run at the start of a gesture without the view twitching, and
 * what makes it safe to run on every gesture rather than once.
 *
 * roqsim's native MuJoCo viewer holds its flight-mode pivot the same way and for the same reason; see
 * `roqsim.rendering.set_orbit_radius`, where the argument is spelled out at length and pinned by
 * tests.
 */
export function retargetPivot(camera: PerspectiveCamera, target: Vector3, distance: number): void {
  camera.updateMatrixWorld()
  target.copy(camera.position).addScaledVector(forward(camera), distance)
}

/**
 * Where the camera should stand to fill the view with `object`: its bounding sphere at a distance the
 * field of view just covers, reached **along the current view direction**.
 *
 * Keeping the direction is the point. Framing something by also rotating to a "good" angle moves two
 * things at once, and a viewer who was looking down a corridor arrives somewhere they cannot place.
 * Travel is legible; being spun is not.
 *
 * The distance is `margin * r / sin(halfFov)`, with the narrower of the vertical and horizontal fields
 * governing so nothing clips on a wide panel -- the same formula as roqsim's `_frame_distance`.
 * Returns the destination rather than moving anything, so the caller can ease into it.
 */
export function frameObject(
  camera: PerspectiveCamera,
  object: Object3D,
  margin = 1.2,
): { position: Vector3; target: Vector3 } | null {
  camera.updateMatrixWorld()
  _box.setFromObject(object)
  if (_box.isEmpty()) return null
  _box.getBoundingSphere(_sphere)
  const radius = Math.max(_sphere.radius, MIN_PIVOT_M)
  if (!Number.isFinite(radius)) return null

  const halfFovY = (camera.fov / 2) * (Math.PI / 180)
  const halfFov = Math.min(halfFovY, Math.atan(Math.tan(halfFovY) * Math.max(camera.aspect, 1e-6)))
  const distance = (margin * radius) / Math.sin(halfFov)

  const target = _sphere.center.clone()
  const position = target.clone().addScaledVector(forward(camera), -distance)
  return { position, target }
}

/** The mesh under `ndc`, or null. Shares `measureDepth`'s "root only, visible only" rules so that
 *  what a double-click frames is what a drag would have pivoted on. */
export function pickObject(
  camera: PerspectiveCamera,
  root: Object3D | null,
  ndc: Vector2,
): Object3D | null {
  if (!root) return null
  camera.updateMatrixWorld()
  _raycaster.setFromCamera(ndc, camera)
  for (const hit of _raycaster.intersectObject(root, true)) {
    if (shown(hit.object)) return hit.object
  }
  return null
}
