// Turning a variation's contributed markers into three.js geometry.
//
// The markers are neutral shapes at places (see robovast.common.scene_markers), so this draws a box
// or a polyline and knows nothing about obstacles or goals. That is what lets the config view show a
// variation the UI has never heard of.
//
// Like its neighbours in this directory this imports only `three`, so the whole scene3d/ directory
// stays extractable — the one exception is useSceneGeometry, which is the service seam.

import {
  BoxGeometry,
  BufferGeometry,
  Color,
  ConeGeometry,
  CylinderGeometry,
  Group,
  Line,
  LineBasicMaterial,
  Mesh,
  MeshBasicMaterial,
  MeshStandardMaterial,
  SphereGeometry,
  Vector3,
  type Material,
} from 'three'

/** One thing to draw, as the service serves it. Mirrors the panel-kit type; duplicated as a local
 *  structural type rather than imported so this file keeps its "three only" rule. */
export interface Marker {
  kind: 'box' | 'cylinder' | 'sphere' | 'pose' | 'path' | 'point'
  pos?: number[] | null
  size?: number[] | null
  radius?: number | null
  height?: number | null
  yaw?: number | null
  points?: number[][] | null
  label?: string
  color?: string
  group?: string
}

/** Used when a marker names no colour, so a variation need not know the view's palette. */
const DEFAULT_COLOR = '#38bdf8'

/** Solids are translucent: a marker sits *in* the world, and an opaque box at a goal pose hides the
 *  geometry the reader is trying to judge it against. */
const SOLID_OPACITY = 0.45

/** How far above the floor a path or a pose disc is drawn. Enough to clear z-fighting with a floor
 *  plane at z=0 without looking like it hovers. */
const GROUND_LIFT = 0.02

const POSE_DISC_RADIUS = 0.22
const POSE_ARROW_LENGTH = 0.55

function vec(p: number[] | null | undefined): Vector3 {
  return new Vector3(p?.[0] ?? 0, p?.[1] ?? 0, p?.[2] ?? 0)
}

function solid(geometry: BufferGeometry, color: string): Mesh {
  return new Mesh(
    geometry,
    new MeshStandardMaterial({
      color: new Color(color),
      transparent: true,
      opacity: SOLID_OPACITY,
      depthWrite: false,
    }),
  )
}

/** A marker's mesh/line, positioned. Z-up throughout: the descriptor is MuJoCo's frame, and the
 *  viewport wraps the whole scene rather than each object, so nothing here converts. */
function build(marker: Marker): Group | null {
  const color = marker.color || DEFAULT_COLOR
  const group = new Group()

  switch (marker.kind) {
    case 'box': {
      const [sx, sy, sz] = marker.size ?? [0.5, 0.5, 0.5]
      const mesh = solid(new BoxGeometry(sx, sy, sz), color)
      // A box's `pos` is its footprint centre on the floor, matching how a placement plugin
      // compiles it -- so it is lifted by half its height rather than buried to the waist.
      mesh.position.copy(vec(marker.pos)).setZ((marker.pos?.[2] ?? 0) + sz / 2)
      mesh.rotation.z = marker.yaw ?? 0
      group.add(mesh)
      break
    }
    case 'cylinder': {
      const h = marker.height ?? 1
      // three's cylinder is Y-up; the scene is Z-up.
      const geometry = new CylinderGeometry(marker.radius ?? 0.25, marker.radius ?? 0.25, h, 24)
      geometry.rotateX(Math.PI / 2)
      const mesh = solid(geometry, color)
      mesh.position.copy(vec(marker.pos)).setZ((marker.pos?.[2] ?? 0) + h / 2)
      group.add(mesh)
      break
    }
    case 'sphere': {
      const mesh = solid(new SphereGeometry(marker.radius ?? 0.25, 20, 14), color)
      mesh.position.copy(vec(marker.pos))
      group.add(mesh)
      break
    }
    case 'pose': {
      // A disc for where, a cone for which way. Drawn rather than a single arrow so a pose with no
      // meaningful yaw still reads as a place.
      const disc = new Mesh(
        new CylinderGeometry(POSE_DISC_RADIUS, POSE_DISC_RADIUS, 0.02, 24).rotateX(Math.PI / 2),
        new MeshBasicMaterial({ color: new Color(color), transparent: true, opacity: 0.85 }),
      )
      disc.position.copy(vec(marker.pos)).setZ((marker.pos?.[2] ?? 0) + GROUND_LIFT)
      group.add(disc)

      if (marker.yaw != null) {
        const cone = new Mesh(
          new ConeGeometry(0.09, 0.26, 16).rotateZ(-Math.PI / 2),
          new MeshBasicMaterial({ color: new Color(color) }),
        )
        const yaw = marker.yaw
        cone.position
          .copy(vec(marker.pos))
          .add(new Vector3(Math.cos(yaw) * POSE_ARROW_LENGTH, Math.sin(yaw) * POSE_ARROW_LENGTH, 0))
          .setZ((marker.pos?.[2] ?? 0) + GROUND_LIFT)
        cone.rotation.z = yaw
        group.add(cone)
      }
      break
    }
    case 'path': {
      const pts = (marker.points ?? []).map(
        (p) => new Vector3(p[0] ?? 0, p[1] ?? 0, (p[2] ?? 0) + GROUND_LIFT),
      )
      if (pts.length < 2) return null
      group.add(
        new Line(
          new BufferGeometry().setFromPoints(pts),
          new LineBasicMaterial({ color: new Color(color) }),
        ),
      )
      break
    }
    case 'point': {
      const mesh = new Mesh(
        new SphereGeometry(0.06, 8, 6),
        new MeshBasicMaterial({ color: new Color(color), transparent: true, opacity: 0.5 }),
      )
      mesh.position.copy(vec(marker.pos)).setZ((marker.pos?.[2] ?? 0) + GROUND_LIFT)
      group.add(mesh)
      break
    }
    default:
      // A kind this build does not know: skip it rather than fail, so a package shipping a richer
      // marker degrades against an older UI instead of blanking the view.
      return null
  }
  return group
}

export interface MarkerLayer {
  root: Group
  /** The group names present, in first-seen order — a panel's legend and visibility toggles. */
  groups: string[]
  setGroupVisible: (group: string, visible: boolean) => void
  dispose: () => void
}

/** Build every marker into one group, indexed by the `group` each declares. */
export function buildMarkers(markers: Marker[]): MarkerLayer {
  const root = new Group()
  const byGroup = new Map<string, Group>()

  for (const marker of markers) {
    const built = build(marker)
    if (!built) continue
    const name = marker.group || 'markers'
    let container = byGroup.get(name)
    if (!container) {
      container = new Group()
      container.name = name
      byGroup.set(name, container)
      root.add(container)
    }
    container.add(built)
  }

  return {
    root,
    groups: [...byGroup.keys()],
    setGroupVisible: (group, visible) => {
      const container = byGroup.get(group)
      if (container) container.visible = visible
    },
    dispose: () => {
      // Geometries and materials are not owned by the scene graph, so removing the group is not
      // enough: without this every config click leaks a world's worth of buffers onto the GPU.
      root.traverse((node) => {
        const mesh = node as Mesh | Line
        if (mesh.geometry) mesh.geometry.dispose()
        const material = mesh.material as Material | Material[] | undefined
        if (Array.isArray(material)) material.forEach((m) => m.dispose())
        else material?.dispose()
      })
      root.clear()
      byGroup.clear()
    },
  }
}
