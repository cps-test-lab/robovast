// Run by the host UI's vitest (frontend/ui/vite.config.ts names this directory in `test.include`):
// the kit is pure shared TypeScript with no build tooling of its own, and one runner covering both
// packages beats a second install here.
import { describe, expect, it } from 'vitest'
import type { ResolvedConfiguration } from './configPanel'
import { declaredMarkers } from './declaredMarkers'

const config = (parameters: Record<string, unknown>): ResolvedConfiguration => ({
  name: 'c0',
  parameters,
  sim: {},
  internals: {},
  contribution: { markers: [], files: {}, errors: [] },
})

describe('declaredMarkers', () => {
  it('passes a literal marker through', () => {
    const out = declaredMarkers(
      { markers: [{ kind: 'pose', pos: [-8, 0], yaw: 0, label: 'start' }] },
      config({}),
    )
    expect(out).toEqual([
      { kind: 'pose', pos: [-8, 0], yaw: 0, label: 'start', group: 'declared' },
    ])
  })

  it('reads a pose from the parameter a marker names', () => {
    const out = declaredMarkers(
      { markers: [{ kind: 'pose', param: 'goal_pose', label: 'goal' }] },
      config({ goal_pose: { position: { x: 21, y: 6 }, orientation: { yaw: 1.5 } } }),
    )
    expect(out[0].pos).toEqual([21, 6])
    expect(out[0].yaw).toBe(1.5)
  })

  it('accepts a bare position, which is what a hand-written .vast pose often is', () => {
    const out = declaredMarkers(
      { markers: [{ kind: 'pose', param: 'goal_pose' }] },
      config({ goal_pose: { x: 3, y: 4 } }),
    )
    expect(out[0].pos).toEqual([3, 4])
  })

  it('applies the declared offset, which is how a map-frame pose lands in a world-frame scene', () => {
    // basic_nav: goal_pose is in the map frame and map = world + (8, 0).
    const out = declaredMarkers(
      { markers: [{ kind: 'pose', param: 'goal_pose', offset: [-8, 0, 0] }] },
      config({ goal_pose: { position: { x: 21, y: 6 } } }),
    )
    expect(out[0].pos).toEqual([13, 6])
  })

  it('numbers the markers when one parameter holds several poses', () => {
    const out = declaredMarkers(
      { markers: [{ kind: 'pose', param: 'goal_poses', label: 'goal' }] },
      config({ goal_poses: [{ x: 1, y: 1 }, { x: 2, y: 2 }] }),
    )
    expect(out.map((m) => m.label)).toEqual(['goal 1', 'goal 2'])
  })

  it('draws nothing for a parameter the configuration does not have', () => {
    // Not a marker at the origin: a pose silently drawn at (0, 0) is a wrong answer, an absent
    // one is a visible question.
    expect(declaredMarkers({ markers: [{ kind: 'pose', param: 'nope' }] }, config({}))).toEqual([])
  })

  it('ignores a panel that declares no markers', () => {
    expect(declaredMarkers({}, config({}))).toEqual([])
    expect(declaredMarkers({ markers: 'not a list' }, config({}))).toEqual([])
  })
})
