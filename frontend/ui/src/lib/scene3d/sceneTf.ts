// Shared-candidate code: this file imports only 'three' -- keep it free of robovast imports (see
// README.md in this directory).
// Not used by the playback path yet (rosbags_tf_to_csv already resolves poses to the map frame);
// kept for the future live view, which composes raw /tf chains in the browser.

import { Euler, Matrix4, Quaternion, Vector3 } from 'three'

// A minimal TF tree for placing scene bodies whose live world pose arrives as a *chain* of /tf
// transforms (e.g. map -> odom -> base_link) rather than a single world-frame transform. Edges are
// stored child -> { parent, T_parent_child }; `resolve` composes a frame's pose in a chosen root.
//
// This is the browser-side of the federated architecture's rule: each system keeps its local TF tree
// (frames unchanged) and the viewer composes `world -> root -> ... -> frame`, so a robot spawned away
// from the origin lands in the right place and multiple systems share one scene (see the repo docs).
export class TransformTree {
  private edges = new Map<string, { parent: string; m: Matrix4 }>()

  /** pos = [x,y,z]; quat = three order (x,y,z,w). Latest transform for `child` wins. */
  set(parent: string, child: string, pos: readonly number[], quat: readonly number[]): void {
    const m = new Matrix4().compose(
      new Vector3(pos[0] ?? 0, pos[1] ?? 0, pos[2] ?? 0),
      new Quaternion(quat[0] ?? 0, quat[1] ?? 0, quat[2] ?? 0, quat[3] ?? 1),
      new Vector3(1, 1, 1),
    )
    this.edges.set(child, { parent, m })
  }

  /** Ingest a tf2_msgs/TFMessage (as delivered by rosbridge). */
  ingest(message: unknown): void {
    const transforms = (message as { transforms?: unknown })?.transforms
    if (!Array.isArray(transforms)) return
    for (const t of transforms) {
      const tf = t as {
        header?: { frame_id?: unknown }
        child_frame_id?: unknown
        transform?: { translation?: Record<string, number>; rotation?: Record<string, number> }
      }
      const parent = tf.header?.frame_id
      const child = tf.child_frame_id
      const tr = tf.transform?.translation
      const ro = tf.transform?.rotation
      if (typeof parent !== 'string' || typeof child !== 'string' || !tr || !ro) continue
      this.set(parent, child, [tr.x, tr.y, tr.z], [ro.x, ro.y, ro.z, ro.w])
    }
  }

  /** Every frame that has an incoming edge (i.e. is a child of something). */
  frames(): string[] {
    return [...this.edges.keys()]
  }

  /** T_root_frame (frame's pose expressed in `root`), or null if `frame` doesn't chain up to `root`. */
  resolve(frame: string, root: string): Matrix4 | null {
    const acc = new Matrix4() // identity == T_frame_frame
    let cur = frame
    const seen = new Set<string>()
    while (cur !== root) {
      const edge = this.edges.get(cur)
      if (!edge || seen.has(cur)) return null // no path to root, or a cycle
      seen.add(cur)
      acc.premultiply(edge.m) // acc = T_parent_cur * acc
      cur = edge.parent
    }
    return acc
  }
}

/** A `world -> root` matrix from a `{ xyz, rpy }` config (rpy = XYZ euler, radians). Identity default. */
export function worldTransformMatrix(cfg?: { xyz?: readonly number[]; rpy?: readonly number[] }): Matrix4 {
  const xyz = cfg?.xyz ?? [0, 0, 0]
  const rpy = cfg?.rpy ?? [0, 0, 0]
  return new Matrix4().compose(
    new Vector3(xyz[0] ?? 0, xyz[1] ?? 0, xyz[2] ?? 0),
    new Quaternion().setFromEuler(new Euler(rpy[0] ?? 0, rpy[1] ?? 0, rpy[2] ?? 0, 'XYZ')),
    new Vector3(1, 1, 1),
  )
}
