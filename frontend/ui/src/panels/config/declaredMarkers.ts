// Markers a .vast declares directly on the scene3d config panel.
//
// The complement to what a variation contributes. A campaign whose factor is a plain
// `ParameterVariationList` has no variation that knows about placement, so nothing is contributed —
// and its start and goal poses are exactly what a reader wants to see. Declaring them in the file is
// how that campaign gets them:
//
//   - scene3d:
//       markers:
//         - {kind: pose, pos: [-8.0, 0.0], yaw: 0.0, label: start}
//         - {kind: pose, param: goal_pose, offset: [-8.0, 0.0, 0.0], label: goal}
//
// `param:` reads a resolved scenario parameter, so the marker follows the selected configuration.
// `offset:` is a literal translation applied afterwards — it is how a map-frame parameter is placed
// in a world-frame scene, declared in the file where the reader can see the reason rather than
// guessed at by the panel.

import type { ResolvedConfiguration, SceneMarker } from '@robovast/panel-kit'

/** A declared marker: a SceneMarker plus the two authoring-only keys the panel resolves. */
interface DeclaredMarker extends SceneMarker {
  /** Name of a resolved scenario parameter to read the position (and yaw) from. */
  param?: string
  /** Added to the resolved position. */
  offset?: number[]
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' ? v : undefined
}

/** `[x, y]` and a yaw from a pose-shaped value: `{position: {x, y}, orientation: {yaw}}`, or a bare
 *  `{x, y}`, which is what a hand-written .vast pose often is. */
function readPose(value: unknown): { pos?: number[]; yaw?: number } {
  if (!isRecord(value)) return {}
  const position = isRecord(value.position) ? value.position : value
  const x = num(position.x)
  const y = num(position.y)
  const z = num(position.z)
  const orientation = isRecord(value.orientation) ? value.orientation : undefined
  const yaw = orientation ? num(orientation.yaw) : undefined
  if (x == null || y == null) return { yaw }
  return { pos: z == null ? [x, y] : [x, y, z], yaw }
}

function translate(
  pos: number[] | null | undefined,
  offset: number[] | null | undefined,
): number[] | undefined {
  if (!pos) return undefined
  if (!offset) return pos
  return pos.map((v, i) => v + (offset[i] ?? 0))
}

/**
 * The markers the panel's own bindings declare, resolved against *config*.
 *
 * A `param:` naming something the configuration does not have yields no marker rather than a marker
 * at the origin: a pose silently drawn at (0, 0) is a wrong answer, and an absent one is a visible
 * question.
 */
export function declaredMarkers(
  bindings: Record<string, unknown>,
  config: ResolvedConfiguration,
): SceneMarker[] {
  const declared = bindings?.markers
  if (!Array.isArray(declared)) return []

  const out: SceneMarker[] = []
  for (const entry of declared) {
    if (!isRecord(entry)) continue
    const marker = entry as unknown as DeclaredMarker
    const { param, offset, ...rest } = marker

    if (!param) {
      const pos = translate(rest.pos, offset)
      if (rest.kind === 'path' ? rest.points : pos) {
        out.push({ ...rest, pos, group: rest.group || 'declared' })
      }
      continue
    }

    const value = config.parameters?.[param]
    // One parameter may hold a pose or a list of them (a campaign sweeping several goals), and both
    // read the same way.
    const values = Array.isArray(value) ? value : [value]
    values.forEach((one, i) => {
      const { pos, yaw } = readPose(one)
      const moved = translate(pos, offset)
      if (!moved) return
      out.push({
        ...rest,
        pos: moved,
        yaw: rest.yaw ?? yaw,
        label: values.length > 1 ? `${rest.label ?? param} ${i + 1}` : rest.label ?? param,
        group: rest.group || 'declared',
      })
    })
  }
  return out
}
