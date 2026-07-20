// Shared-candidate code: this file imports only 'three' / 'three/addons' -- keep it free of
// robovast imports (see README.md in this directory).
//
// A plain-three viewport for the scene descriptor: renderer + camera + lights + ground grid +
// orbit controls + the Z-up wrapper group (the descriptor is Z-up like ROS; the wrapper rotates it
// -PI/2 about X into three's Y-up -- sceneLoader's skin binding relies on exactly this rotation).
// Hosts mount it into a container element, hand it the loader's scene root, and animate through
// the loader's imperative API (jointMap/basePose); the viewport just renders continuously.

import {
  AmbientLight,
  Color,
  DirectionalLight,
  GridHelper,
  Group,
  MathUtils,
  Matrix4,
  Mesh,
  PerspectiveCamera,
  Scene,
  Vector3,
  WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

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
  private root: Group | null = null
  private resizeObserver: ResizeObserver
  private container: HTMLElement

  constructor(container: HTMLElement, opts: ViewportOptions = {}) {
    this.container = container
    this.renderer = new WebGLRenderer({ antialias: true })
    this.renderer.setPixelRatio(window.devicePixelRatio || 1)
    this.renderer.shadowMap.enabled = true
    this.renderer.domElement.style.display = 'block'
    container.appendChild(this.renderer.domElement)

    this.scene.background = new Color(opts.background ?? '#12171f')

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

    // Ground grid just below z=0 so a descriptor floor plane doesn't z-fight it.
    const grid = new GridHelper(
      40,
      40,
      new Color(opts.gridCenterColor ?? '#54c6b4'),
      new Color(opts.gridColor ?? '#30524d'),
    )
    grid.position.y = -0.001
    this.scene.add(grid)

    // Scene content is authored Z-up; render it under the Y-up wrapper.
    this.zUpGroup.matrixAutoUpdate = false
    this.zUpGroup.matrix.copy(UP_MATRIX)
    this.scene.add(this.zUpGroup)

    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.08
    this.setView({}) // sane default until the descriptor's baked view arrives

    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()

    this.renderer.setAnimationLoop(() => {
      this.controls.update()
      this.renderer.render(this.scene, this.camera)
    })
  }

  /** Swap in the loader's scene root (replacing any previous one). */
  setSceneRoot(root: Group): void {
    if (this.root) this.zUpGroup.remove(this.root)
    this.root = root
    this.zUpGroup.add(root)
  }

  /** Frame the camera per the descriptor's baked view (or defaults for missing fields). */
  setView(view: SceneViewSpec): void {
    const { position, target } = cameraFromView(view)
    this.camera.position.copy(position)
    this.controls.target.copy(target)
    this.controls.update()
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
    this.controls.dispose()
    // Free the scene root's GPU resources (the loader builds plain Meshes).
    this.root?.traverse((obj) => {
      if (obj instanceof Mesh) {
        obj.geometry.dispose()
        const materials = Array.isArray(obj.material) ? obj.material : [obj.material]
        for (const m of materials) {
          m.map?.dispose?.()
          m.dispose()
        }
      }
    })
    this.renderer.dispose()
    this.renderer.domElement.remove()
  }
}
