// Shared-candidate code: this file imports only 'three' / 'three/addons' and its siblings in this
// directory -- keep it free of robovast imports (see README.md here).
//
// A plain-three viewport for the scene: renderer + camera + lights + ground grid +
// orbit controls + the Z-up wrapper group (the scene is Z-up like ROS; the wrapper rotates it
// -PI/2 about X into three's Y-up -- sceneLoader's skin binding relies on exactly this rotation).
// Hosts mount it into a container element, hand it the loader's scene root, and animate through
// the loader's imperative API (jointMap/basePose); the viewport just renders continuously.
//
// Drag bindings are OrbitControls' own; the wheel is not (see cursorDolly.ts for why), and the
// frustum tracks the camera so flying out does not clip the world away. Fingers are the viewport's
// own beyond one: touchNavigation.ts maps a pinch onto the wheel's flight, and adds a two-finger pan,
// a twist and a double tap.
//
// What makes all three gestures scale to the world is that the orbit pivot is re-chosen onto the
// surface under the cursor whenever a gesture starts (`anchor`, and pivot.ts for the reasoning).
// Rotate, pan and wheel are all scaled by the pivot distance, so that one measurement is what decides
// whether the same drag reads as looking around a corridor or as being flung through its wall.

import {
  AmbientLight,
  Box3,
  Color,
  DirectionalLight,
  GridHelper,
  Group,
  MathUtils,
  Matrix4,
  PerspectiveCamera,
  Scene,
  TOUCH,
  Sphere,
  Vector2,
  Vector3,
  Object3D,
  WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { CANVAS, GRID, GRID_CENTER } from '@/colors'
import { dollyToCursor } from './cursorDolly'
import { MIN_PIVOT_M, frameObject, measureDepth, pickObject, retargetPivot } from './pivot'
import { letterbox, type SceneCamera } from './sceneCameras'
import { TouchNavigation, type ClientPoint } from './touchNavigation'

/** The scene's optional baked initial camera (a MuJoCo free camera, Z-up world frame). */
export interface SceneViewSpec {
  lookat?: number[]
  distance?: number
  azimuth?: number // degrees about world +Z
  elevation?: number // degrees; negative looks down
}

export interface ViewportOptions {
  background?: string
  gridColor?: string
  gridCenterColor?: string
}

// The Z-up -> Y-up wrapper rotation; also used to bring the (Z-up) camera spec into three coords.
const UP_MATRIX = new Matrix4().makeRotationX(-Math.PI / 2)

// Floor for the assumed scene extent, matching the ground grid: a scene holding one small robot
// should still see the grid it stands on, and an empty one has no bounds to measure at all.
const MIN_SCENE_RADIUS_M = 20

// The closest the near plane is ever put (see updateFrustum): the worst depth precision the scene is
// ever rendered with, which is what gridDepthOffset has to clear.
const NEAR_FLOOR_M = 0.01

// A wheel gesture is over once this long passes with no event. Long enough that a trackpad's ragged
// stream (several events per frame, then gaps) stays one gesture; short enough that letting go, aiming
// somewhere else and scrolling again re-measures.
const WHEEL_GESTURE_GAP_MS = 150

// What surrounds a sensor's picture where the canvas is not its shape: the bars of a letterbox, so the
// picture's edge is the sensor's and not the world's background running on past it.
const LETTERBOX = new Color(0x000000)

// How far a twist or a look may tilt the view toward straight up or down: just short of it, where the
// view direction and the world's vertical would coincide and the turn axis would vanish.
const MAX_PITCH = Math.PI / 2 - 0.05

// How long a double-click's flight to its target takes. Long enough to read as travel rather than a
// cut, short enough not to be something to sit through.
const FOCUS_MS = 260

/**
 * How far below z=0 to sink the ground grid so a scene's floor plane hides it, when the plane
 * the two share is `viewDistance` metres from the camera.
 *
 * A fixed millimetre does not do it. A 24-bit depth buffer resolves roughly z^2 / (near * 2^24)
 * metres at distance z, so an offset that separates grid from floor on a tabletop is far under the
 * buffer's resolution once the camera sits tens of metres out -- and the two planes then z-fight,
 * which is what the grid showing *through* the floor in mottled patches is.
 *
 * It cannot be sized off the scene instead: `sceneRadius` is floored at MIN_SCENE_RADIUS_M, so the
 * decimetre a warehouse needs would also apply to an empty world holding one small robot, whose
 * wheels would then visibly float above the grid. Keyed on the view distance, the offset stays
 * sub-millimetre while anything is close enough for it to be seen, and by the range where it reaches
 * a decimetre a decimetre is about a pixel.
 */
function gridDepthOffset(viewDistance: number): number {
  const depthResolution = (viewDistance * viewDistance) / (NEAR_FLOOR_M * 2 ** 24)
  return Math.max(0.001, 3 * depthResolution)
}

/** Camera pose from a MuJoCo free-camera spec: position = lookat - distance * forward. */
function cameraFromView(view: SceneViewSpec): { position: Vector3; target: Vector3 } {
  const [lx, ly, lz] = view.lookat ?? [0, 0, 0]
  const az = MathUtils.degToRad(view.azimuth ?? 90)
  const el = MathUtils.degToRad(view.elevation ?? -45)
  const d = view.distance ?? 5
  const forward = new Vector3(
    Math.cos(el) * Math.cos(az),
    Math.cos(el) * Math.sin(az),
    Math.sin(el),
  )
  const target = new Vector3(lx, ly, lz)
  const position = target.clone().sub(forward.multiplyScalar(d))
  // Both are Z-up world points; the scene content lives inside the up-rotated group, so bring the
  // camera into the same (three, Y-up) frame.
  return { position: position.applyMatrix4(UP_MATRIX), target: target.applyMatrix4(UP_MATRIX) }
}

/**
 * How far to move the camera, and its pivot with it, so that a screen offset of (`dx`, `dy`) CSS
 * pixels carries the point `distance` ahead of the camera along with the fingers.
 *
 * Scaled by the pivot distance for the reason rotate and the wheel are (pivot.ts): measured against
 * the surface under the fingers, the same two-finger pan is a few centimetres across a tabletop and
 * metres across a building.
 */
export function panOffset(
  camera: PerspectiveCamera,
  distance: number,
  height: number,
  dx: number,
  dy: number,
): Vector3 {
  const perPx = (2 * distance * Math.tan(MathUtils.degToRad(camera.fov) / 2)) / Math.max(1, height)
  camera.updateMatrixWorld()
  const right = new Vector3().setFromMatrixColumn(camera.matrixWorld, 0).normalize()
  const up = new Vector3().setFromMatrixColumn(camera.matrixWorld, 1).normalize()
  // The world follows the fingers, so the camera goes the other way: left for a drag to the right,
  // and up for a drag down the screen (client y grows downward).
  return right.multiplyScalar(-dx * perPx).add(up.multiplyScalar(dy * perPx))
}

/** Take *root* out of *group*, leaving it loadable into another viewport.
 *
 *  The viewport shows a scene root and never owns one: a model outlives the viewport that
 *  displayed it (`sceneModelCache` parks one for the next mount), and freeing it here would
 *  hand the next mount an emptied graph -- a world that renders as nothing rather than as an
 *  error. Whoever loaded the model frees it.
 */
export function detachSceneRoot(group: Object3D, root: Object3D | null): void {
  if (root) group.remove(root)
}

export class SceneViewport {
  private renderer: WebGLRenderer
  private scene = new Scene()
  private camera: PerspectiveCamera
  private controls: OrbitControls
  private zUpGroup = new Group()
  private grid: GridHelper
  private root: Group | null = null
  private resizeObserver: ResizeObserver
  private container: HTMLElement
  // The loaded world's bounding sphere, in three (Y-up) world coords: what the far plane must enclose.
  private sceneRadius = MIN_SCENE_RADIUS_M
  private sceneCenter = new Vector3()
  // The view last framed, so `resetView` has a home to return to. It starts as the empty spec the
  // constructor frames with, which is exactly where a scene carrying no baked camera stands.
  private view: SceneViewSpec = {}
  private onWheel: (event: WheelEvent) => void
  private onPointerDown: (event: PointerEvent) => void
  private onDoubleClick: (event: MouseEvent) => void
  private onPointerMove: (event: PointerEvent) => void
  private onPointerUp: (event: PointerEvent) => void
  private onPointerCancel: (event: PointerEvent) => void
  private touch: TouchNavigation
  // The kind of pointer that pressed last. A browser that turns a double tap into a `dblclick` as
  // well would otherwise fly twice; the double tap is touchNavigation's.
  private lastPointerType = ''
  // How far ahead the orbit pivot sits. Held as a number rather than read back off `controls.target`
  // because the wheel travels along the cursor ray while the pivot stays on the view axis: a distance
  // re-derived from the two vectors would shrink by an extra cos(angle to the cursor) per event.
  private pivotDistance = MIN_PIVOT_M
  // When the last wheel event arrived, so a burst re-measures the pivot once rather than per event.
  private lastWheelAt = -Infinity
  // The in-flight double-click focus, if any: where the camera is travelling from and to.
  private focus: { from: Vector3; to: Vector3; fromT: Vector3; toT: Vector3; t0: number } | null =
    null
  // The sensor camera the scene is shown through, or null for the free orbit view.
  private viewpoint: SceneCamera | null = null

  constructor(container: HTMLElement, opts: ViewportOptions = {}) {
    this.container = container
    this.renderer = new WebGLRenderer({ antialias: true })
    this.renderer.setPixelRatio(window.devicePixelRatio || 1)
    this.renderer.shadowMap.enabled = true
    this.renderer.domElement.style.display = 'block'
    // Fingers move the scene, never the page: no scroll, no browser pinch-zoom over the canvas. Set
    // here rather than left to OrbitControls, which sets it too but is not what this depends on.
    this.renderer.domElement.style.touchAction = 'none'
    container.appendChild(this.renderer.domElement)

    this.scene.background = new Color(opts.background ?? CANVAS)

    // Aspect is set by resize(); near/far are placeholders that updateFrustum() replaces on the first
    // frame -- only the 0.01 near floor it clamps to survives.
    this.camera = new PerspectiveCamera(36, 1, 0.01, 200)

    this.scene.add(new AmbientLight(0xffffff, 1.2))
    const key = new DirectionalLight(0xffffff, 1.9)
    key.position.set(3.4, 4.6, 2.4)
    key.castShadow = true
    key.shadow.mapSize.set(2048, 2048)
    this.scene.add(key)
    const fill = new DirectionalLight(0xffffff, 0.6)
    fill.position.set(-2.2, 2.0, -1.8)
    this.scene.add(fill)

    // Ground grid below z=0, so a scene's floor plane covers it rather than fighting it; how
    // far below is a function of where the camera is (updateGridDepth, every frame).
    this.grid = new GridHelper(
      40,
      40,
      new Color(opts.gridCenterColor ?? GRID_CENTER),
      new Color(opts.gridColor ?? GRID),
    )
    this.scene.add(this.grid)

    // Scene content is authored Z-up; render it under the Y-up wrapper.
    this.zUpGroup.matrixAutoUpdate = false
    this.zUpGroup.matrix.copy(UP_MATRIX)
    this.scene.add(this.zUpGroup)

    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.08
    // Drag bindings stay OrbitControls'; its dolly does not, because shrinking the orbit radius
    // stalls the wheel a short way from the pivot -- cursorDolly.ts flies instead.
    this.controls.enableZoom = false
    // One finger is OrbitControls' rotate. Two are the viewport's alone (touchNavigation.ts): the
    // controls' own two-finger dolly-pan would fight the pinch and the pan on every event.
    this.controls.touches = { ONE: TOUCH.ROTATE, TWO: null as unknown as TOUCH }
    this.setView({}) // sane default until the scene's baked view arrives

    // Native listener, non-passive: a passive one cannot preventDefault, and without that the wheel
    // scrolls the page under the panel instead of moving the camera.
    this.onWheel = (event: WheelEvent) => {
      event.preventDefault()
      if (this.viewpoint) return // a sensor's view is where the sensor is, not where the wheel goes
      const now = event.timeStamp
      if (now - this.lastWheelAt > WHEEL_GESTURE_GAP_MS) this.anchor(event)
      this.lastWheelAt = now
      this.dollyBy(event)
    }
    this.renderer.domElement.addEventListener('wheel', this.onWheel, { passive: false })

    // Capture phase, so the pivot is already on the right surface by the time OrbitControls' own
    // pointerdown runs. Both rotate and pan read the pivot distance, so this one hook is what scales
    // them: it costs one raycast per gesture, and never one per frame.
    //
    // Fingers also go to the touch navigation, which knows a pinch from a turn. It is fed whatever
    // the viewpoint, so it always knows how many fingers are down; it acts only on the free camera.
    this.touch = new TouchNavigation({
      free: () => this.viewpoint === null,
      anchor: (at) => this.anchor(at),
      dolly: (at, deltaY) => this.dolly(at, deltaY),
      panBy: (dx, dy) => this.panBy(dx, dy),
      lookBy: (yaw, pitch) => this.lookBy(yaw, pitch),
      focusAt: (at) => this.focusUnderCursor(at),
      setRotate: (enabled) => {
        this.controls.enableRotate = enabled
      },
    })
    this.onPointerDown = (event: PointerEvent) => {
      this.lastPointerType = event.pointerType
      this.touch.down(event)
      if (this.viewpoint) return
      this.focus = null
      this.anchor(event)
    }
    this.renderer.domElement.addEventListener('pointerdown', this.onPointerDown, { capture: true })
    this.onPointerMove = (event: PointerEvent) => this.touch.move(event)
    this.renderer.domElement.addEventListener('pointermove', this.onPointerMove)
    this.onPointerUp = (event: PointerEvent) => this.touch.up(event)
    this.renderer.domElement.addEventListener('pointerup', this.onPointerUp)
    this.onPointerCancel = (event: PointerEvent) => this.touch.cancel(event)
    this.renderer.domElement.addEventListener('pointercancel', this.onPointerCancel)

    this.onDoubleClick = (event: MouseEvent) => {
      if (this.lastPointerType === 'touch') return
      if (!this.viewpoint) this.focusUnderCursor(event)
    }
    this.renderer.domElement.addEventListener('dblclick', this.onDoubleClick)

    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()

    this.renderer.setAnimationLoop(() => {
      if (this.viewpoint) {
        this.renderViewpoint(this.viewpoint)
        return
      }
      this.advanceFocus()
      this.controls.update()
      this.updateFrustum()
      this.updateGridDepth()
      this.renderer.render(this.scene, this.camera)
    })
  }

  /**
   * Show the scene through a sensor camera of the loaded scene, or (`null`) through the free orbit
   * camera again, which keeps the framing it had.
   *
   * The sensor camera is a node of the scene graph, so it moves with its body as the motion source
   * seats poses, and its projection is the sensor's own. The picture is letterboxed to the sensor's
   * aspect, and the ground grid -- a viewer's aid, not part of the world -- is hidden while it shows.
   */
  setViewpoint(viewpoint: SceneCamera | null): void {
    this.viewpoint = viewpoint
    this.focus = null
    this.controls.enabled = viewpoint === null
    this.grid.visible = viewpoint === null
    if (!viewpoint) {
      const { width, height } = this.canvasSize()
      this.renderer.setScissorTest(false)
      this.renderer.setViewport(0, 0, width, height)
    }
  }

  private canvasSize(): { width: number; height: number } {
    return { width: this.container.clientWidth, height: this.container.clientHeight }
  }

  /** One frame through *viewpoint*: bars over the whole canvas, then the scene in the box. */
  private renderViewpoint(viewpoint: SceneCamera): void {
    const { width, height } = this.canvasSize()
    if (!width || !height) return
    const box = letterbox(width, height, viewpoint.aspect ?? width / height)
    const camera = viewpoint.camera
    const aspect = viewpoint.projectionAspect ?? box.width / box.height
    if (camera.aspect !== aspect) {
      camera.aspect = aspect
      camera.updateProjectionMatrix()
    }
    this.renderer.setScissorTest(false)
    this.renderer.setViewport(0, 0, width, height)
    this.renderer.setClearColor(LETTERBOX)
    this.renderer.clear()
    this.renderer.setViewport(box.x, box.y, box.width, box.height)
    this.renderer.setScissor(box.x, box.y, box.width, box.height)
    this.renderer.setScissorTest(true)
    this.renderer.render(this.scene, camera)
  }

  /** Normalized device coordinates of a pointer event within the canvas. */
  private ndc(event: { clientX: number; clientY: number }): Vector2 {
    const rect = this.renderer.domElement.getBoundingClientRect()
    return new Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    )
  }

  /**
   * Put the orbit pivot on the surface under the cursor, without moving the camera.
   *
   * Called at the start of a gesture and nowhere else. What it buys is that rotate, pan and the wheel
   * are all scaled by the pivot distance, so measuring it once against the actual world is what makes
   * all three fit a 3 m tabletop and a 65 m building without a mode, a setting, or a binding change.
   */
  private anchor(event: { clientX: number; clientY: number }): void {
    this.pivotDistance = measureDepth(
      this.camera,
      this.root,
      this.ndc(event),
      this.pivotDistance,
      this.sceneRadius,
    )
    retargetPivot(this.camera, this.controls.target, this.pivotDistance)
    this.controls.update()
  }

  /** Fly along the ray under the pointer, as one wheel event does (cursorDolly.ts). */
  private dollyBy(event: WheelEvent): void {
    this.focus = null // a wheel during a focus flight means the human wants to steer, not to watch
    this.pivotDistance = dollyToCursor(
      this.camera,
      this.pivotDistance,
      event,
      this.renderer.domElement,
    )
    // Back onto the view axis at its new distance. Keeping the pivot dead ahead is what stops
    // OrbitControls' `lookAt(target)` from re-centring it every frame -- off-centre scrolling would
    // otherwise swing the view a couple of degrees a notch, and steering by aiming would drift.
    retargetPivot(this.camera, this.controls.target, this.pivotDistance)
  }

  /** A pinch's step: the wheel's flight for a wheel delta of `deltaY` pixels at `at`. */
  private dolly(at: ClientPoint, deltaY: number): void {
    this.dollyBy({ deltaY, deltaMode: 0, clientX: at.clientX, clientY: at.clientY } as WheelEvent)
  }

  /** Two fingers moved the view: what was under their midpoint stays under it. */
  private panBy(dx: number, dy: number): void {
    const height = this.renderer.domElement.clientHeight
    const move = panOffset(this.camera, this.pivotDistance, height, dx, dy)
    this.focus = null
    this.camera.position.add(move)
    this.controls.target.add(move)
  }

  /** Turn the view in place: `yaw` about the world's vertical, `pitch` up, both in radians. The pivot
   *  stays at its distance, now ahead in the new direction, so the next turn is about it. */
  private lookBy(yaw: number, pitch: number): void {
    const up = new Vector3(0, 1, 0) // three's vertical; the scene's Z-up sits under the wrapper
    const dir = this.camera.getWorldDirection(new Vector3())
    dir.applyAxisAngle(up, yaw)
    const current = Math.asin(Math.max(-1, Math.min(1, dir.y)))
    const next = Math.max(-MAX_PITCH, Math.min(MAX_PITCH, current + pitch))
    const right = dir.clone().cross(up)
    if (right.lengthSq() > 1e-9) dir.applyAxisAngle(right.normalize(), next - current)
    this.focus = null
    this.controls.target.copy(this.camera.position).addScaledVector(dir.normalize(), this.pivotDistance)
    this.controls.update()
  }

  /** Double-click or double tap: travel to frame whatever is under the pointer, keeping the view
   *  direction. */
  private focusUnderCursor(event: ClientPoint): void {
    const hit = pickObject(this.camera, this.root, this.ndc(event))
    if (!hit) return // empty space names nothing to go to; do not invent a destination
    const dest = frameObject(this.camera, hit)
    if (!dest) return
    this.focus = {
      from: this.camera.position.clone(),
      to: dest.position,
      fromT: this.controls.target.clone(),
      toT: dest.target,
      t0: performance.now(),
    }
  }

  /** Advance an in-flight double-click focus by one frame. */
  private advanceFocus(): void {
    const focus = this.focus
    if (!focus) return
    const raw = Math.min((performance.now() - focus.t0) / FOCUS_MS, 1)
    const s = raw < 0.5 ? 2 * raw * raw : 1 - 2 * (1 - raw) * (1 - raw) // ease in-out, quadratic
    this.camera.position.lerpVectors(focus.from, focus.to, s)
    this.controls.target.lerpVectors(focus.fromT, focus.toT, s)
    this.pivotDistance = Math.max(this.camera.position.distanceTo(this.controls.target), MIN_PIVOT_M)
    if (raw >= 1) this.focus = null
  }

  /** Show the loader's scene root, in place of any previous one (see
   *  {@link detachSceneRoot} for what happens to that one). */
  setSceneRoot(root: Group): void {
    detachSceneRoot(this.zUpGroup, this.root)
    this.root = root
    this.zUpGroup.add(root)
    // One traversal per scene swap, to size the far plane against the world rather than a constant.
    const box = new Box3().setFromObject(root)
    const sphere = box.isEmpty() ? null : box.getBoundingSphere(new Sphere())
    const ok = sphere !== null && Number.isFinite(sphere.radius)
    this.sceneRadius = ok ? Math.max(MIN_SCENE_RADIUS_M, sphere.radius) : MIN_SCENE_RADIUS_M
    if (ok) this.sceneCenter.copy(sphere.center)
    else this.sceneCenter.set(0, 0, 0)
  }

  /** Frame the camera per the scene's baked view (or defaults for missing fields). */
  setView(view: SceneViewSpec): void {
    this.view = view
    const { position, target } = cameraFromView(view)
    this.camera.position.copy(position)
    this.controls.target.copy(target)
    // The world's authored framing distance, until the first gesture measures one against geometry.
    this.pivotDistance = Math.max(position.distanceTo(target), MIN_PIVOT_M)
    this.controls.update()
  }

  /** Put the camera back where the scene opened -- the scene's baked view, or the default one.
   *
   *  The way *out* of a lost view, and it exists because there is no way back by hand: the wheel
   *  flies rather than orbiting, so a few notches at a wall can leave the camera inside geometry or
   *  far enough out that the world is a dot, with the pivot carried along and no bound to undo it.
   *  Re-applying the opening view rather than fitting the scene's bounds, because that view is the
   *  one the world's author chose and the one the panel already framed on load -- a fit would answer
   *  a different, and to a reader unexpected, question. */
  resetView(): void {
    this.focus = null // abandon a focus flight rather than let it fly back out of the reset view
    this.setView(this.view)
  }

  /**
   * Keep the far plane behind the world, wherever the camera has flown to.
   *
   * A fixed far plane is invisible only while the wheel cannot travel: once it can, crossing that
   * distance makes the whole world vanish. The measure has to be the camera's distance from the
   * *scene*, not from its pivot -- the pivot sits on whatever surface the last gesture aimed at, which
   * says how close the nearest thing is and nothing at all about how far the far side of the world
   * has been left behind. A frustum keyed on it would clip the world away. Enclosing the scene's bounding
   * sphere is exactly the condition for nothing to be clipped; near follows far so the depth-buffer
   * ratio stays fixed rather than z-fighting once far grows large -- though the clamp is the usual
   * case: far has to pass 2 km before the ratio moves near off NEAR_FLOOR_M.
   */
  private updateFrustum(): void {
    const reach = this.camera.position.distanceTo(this.sceneCenter) + this.sceneRadius
    const far = Math.max(50, reach * 1.2)
    // Rebuilding the projection matrix for a sub-percent change every frame is wasted work.
    if (Math.abs(far - this.camera.far) < this.camera.far * 0.01) return
    this.camera.far = far
    this.camera.near = Math.max(NEAR_FLOOR_M, far / 2e5)
    this.camera.updateProjectionMatrix()
  }

  /** Re-sink the ground grid for how far the camera currently is from the world.
   *
   *  Measured against the scene, not the orbit pivot, for the reason updateFrustum gives: the pivot
   *  reports the nearest surface aimed at, not how far out the camera has flown.
   */
  private updateGridDepth(): void {
    this.grid.position.y = -gridDepthOffset(this.camera.position.distanceTo(this.sceneCenter))
  }

  private resize(): void {
    const w = this.container.clientWidth
    const h = this.container.clientHeight
    if (!w || !h) return
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    this.renderer.setSize(w, h)
  }

  dispose(): void {
    this.renderer.setAnimationLoop(null)
    this.resizeObserver.disconnect()
    this.renderer.domElement.removeEventListener('wheel', this.onWheel)
    this.renderer.domElement.removeEventListener('pointerdown', this.onPointerDown, {
      capture: true,
    })
    this.renderer.domElement.removeEventListener('pointermove', this.onPointerMove)
    this.renderer.domElement.removeEventListener('pointerup', this.onPointerUp)
    this.renderer.domElement.removeEventListener('pointercancel', this.onPointerCancel)
    this.renderer.domElement.removeEventListener('dblclick', this.onDoubleClick)
    this.controls.dispose()
    detachSceneRoot(this.zUpGroup, this.root)
    this.renderer.dispose()
    this.renderer.domElement.remove()
  }
}
