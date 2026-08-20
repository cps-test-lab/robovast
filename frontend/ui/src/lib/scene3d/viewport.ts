// Shared-candidate code: this file imports only 'three' / 'three/addons' and its siblings in this
// directory -- keep it free of robovast imports (see README.md here).
//
// A plain-three viewport for the scene descriptor: renderer + camera + lights + ground grid +
// orbit controls + the Z-up wrapper group (the descriptor is Z-up like ROS; the wrapper rotates it
// -PI/2 about X into three's Y-up -- sceneLoader's skin binding relies on exactly this rotation).
// Hosts mount it into a container element, hand it the loader's scene root, and animate through
// the loader's imperative API (jointMap/basePose); the viewport just renders continuously.
//
// Drag bindings are OrbitControls' own; the wheel is not (see cursorDolly.ts for why), and the
// frustum tracks the camera so flying out does not clip the world away.

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
  Sphere,
  Vector3,
  WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { CANVAS, GRID, GRID_CENTER } from '@/colors'
import { dollyToCursor } from './cursorDolly'
import { disposeSceneGraph } from './sceneLoader'

/** The descriptor's optional baked initial camera (a MuJoCo free camera, Z-up world frame). */
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

// Floor for the assumed scene extent, matching the ground grid: a descriptor holding one small robot
// should still see the grid it stands on, and an empty one has no bounds to measure at all.
const MIN_SCENE_RADIUS_M = 20

// The closest the near plane is ever put (see updateFrustum): the worst depth precision the scene is
// ever rendered with, which is what gridDepthOffset has to clear.
const NEAR_FLOOR_M = 0.01

/**
 * How far below z=0 to sink the ground grid so a descriptor's floor plane hides it, when the plane
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
  // constructor frames with, which is exactly where a descriptor carrying no baked camera stands.
  private view: SceneViewSpec = {}
  private onWheel: (event: WheelEvent) => void

  constructor(container: HTMLElement, opts: ViewportOptions = {}) {
    this.container = container
    this.renderer = new WebGLRenderer({ antialias: true })
    this.renderer.setPixelRatio(window.devicePixelRatio || 1)
    this.renderer.shadowMap.enabled = true
    this.renderer.domElement.style.display = 'block'
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

    // Ground grid below z=0, so a descriptor's floor plane covers it rather than fighting it; how
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
    this.setView({}) // sane default until the descriptor's baked view arrives

    // Native listener, non-passive: a passive one cannot preventDefault, and without that the wheel
    // scrolls the page under the panel instead of moving the camera.
    this.onWheel = (event: WheelEvent) => {
      event.preventDefault()
      dollyToCursor(this.camera, this.controls.target, event, this.renderer.domElement)
    }
    this.renderer.domElement.addEventListener('wheel', this.onWheel, { passive: false })

    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()

    this.renderer.setAnimationLoop(() => {
      this.controls.update()
      this.updateFrustum()
      this.updateGridDepth()
      this.renderer.render(this.scene, this.camera)
    })
  }

  /** Swap in the loader's scene root (replacing any previous one). */
  setSceneRoot(root: Group): void {
    if (this.root) this.zUpGroup.remove(this.root)
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

  /** Frame the camera per the descriptor's baked view (or defaults for missing fields). */
  setView(view: SceneViewSpec): void {
    this.view = view
    const { position, target } = cameraFromView(view)
    this.camera.position.copy(position)
    this.controls.target.copy(target)
    this.controls.update()
  }

  /** Put the camera back where the scene opened -- the descriptor's baked view, or the default one.
   *
   *  The way *out* of a lost view, and it exists because there is no way back by hand: the wheel
   *  flies rather than orbiting, so a few notches at a wall can leave the camera inside geometry or
   *  far enough out that the world is a dot, with the pivot carried along and no bound to undo it.
   *  Re-applying the opening view rather than fitting the scene's bounds, because that view is the
   *  one the world's author chose and the one the panel already framed on load -- a fit would answer
   *  a different, and to a reader unexpected, question. */
  resetView(): void {
    this.setView(this.view)
  }

  /**
   * Keep the far plane behind the world, wherever the camera has flown to.
   *
   * A fixed far plane is invisible only while the wheel cannot travel: once it can, crossing that
   * distance makes the whole world vanish. The measure has to be the camera's distance from the
   * *scene*, not from its pivot -- the wheel carries the pivot along, so the pivot distance is
   * constant by design and a frustum keyed on it would never grow. Enclosing the scene's bounding
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
   *  Measured against the scene, not the orbit pivot, for the reason updateFrustum gives: the wheel
   *  carries the pivot along, so pivot distance barely moves however far out the camera flies.
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
    this.controls.dispose()
    // Free the scene root's GPU resources, through the same helper a scene *swap* uses.
    if (this.root) disposeSceneGraph(this.root)
    this.renderer.dispose()
    this.renderer.domElement.remove()
  }
}
