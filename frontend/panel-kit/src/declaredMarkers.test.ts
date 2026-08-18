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

  it('reads a pose from an internal a variation left behind', () => {
    // The generic point: a panel field's source is a separate question from its meaning, so a datum
    // a variation recorded is bindable exactly like a scenario parameter.
    const out = declaredMarkers(
      { markers: [{ kind: 'pose', internal: '_start_pose', label: 'start' }] },
      { ...config({}), internals: { _start_pose: { position: { x: 2, y: 3 } } } },
    )
    expect(out[0].pos).toEqual([2, 3])
    expect(out[0].label).toBe('start')
  })

  it('reads a path polyline from an internal', () => {
    const out = declaredMarkers(
      { markers: [{ kind: 'path', internal: '_path', label: 'planned path' }] },
      { ...config({}), internals: { _path: [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }] } },
    )
    expect(out[0].kind).toBe('path')
    expect(out[0].points).toEqual([[0, 0], [1, 0], [1, 1]])
  })

  it('draws nothing for an internal the configuration does not have', () => {
    expect(declaredMarkers({ markers: [{ kind: 'path', internal: '_nope' }] }, config({}))).toEqual([])
  })
})
