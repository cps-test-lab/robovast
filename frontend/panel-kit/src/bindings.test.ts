// Run by the host UI's vitest (see frontend/ui/vite.config.ts `test.include`).
import { describe, expect, it } from 'vitest'

import { resolveBinding, resolveStringBinding } from './bindings'
import type { ResolvedConfiguration } from './configPanel'

const config = (over: Partial<ResolvedConfiguration> = {}): ResolvedConfiguration => ({
  name: 'c0',
  parameters: { map_file: 'maps/generated.yaml' },
  sim: {},
  internals: { _path: [{ x: 0, y: 1 }] },
  contribution: { markers: [], files: { map: 'maps/contributed.yaml' }, errors: [] },
  ...over,
})

describe('resolveBinding', () => {
  it('reads a bare value as the literal it is', () => {
    expect(resolveBinding('files/depot.yaml', config())).toBe('files/depot.yaml')
    expect(resolveBinding([1, 2], config())).toEqual([1, 2])
  })

  it('reads the three named sources', () => {
    expect(resolveBinding({ param: 'map_file' }, config())).toBe('maps/generated.yaml')
    expect(resolveBinding({ internal: '_path' }, config())).toEqual([{ x: 0, y: 1 }])
    expect(resolveBinding({ role: 'map' }, config())).toBe('maps/contributed.yaml')
  })

  it('reads a mapping that names no source as the literal', () => {
    // A pose is `{x, y}` and a vega spec is a mapping too — demanding `literal:` around either
    // would be noise, so a mapping without a source key is the value.
    expect(resolveBinding({ x: 1, y: 2 }, config())).toEqual({ x: 1, y: 2 })
    expect(resolveBinding({ literal: { x: 1 } }, config())).toEqual({ x: 1 })
  })

  it('yields undefined for what the configuration does not have', () => {
    // Absent, never defaulted: a field quietly filled with something plausible is a wrong answer
    // that looks right.
    expect(resolveBinding({ param: 'nope' }, config())).toBeUndefined()
    expect(resolveBinding({ internal: '_nope' }, config())).toBeUndefined()
    expect(resolveBinding({ role: 'nope' }, config())).toBeUndefined()
    expect(resolveBinding(undefined, config())).toBeUndefined()
  })

  it('narrows to a non-empty string for a path-shaped field', () => {
    expect(resolveStringBinding({ param: 'map_file' }, config())).toBe('maps/generated.yaml')
    expect(resolveStringBinding({ internal: '_path' }, config())).toBeUndefined()  // a list is not a path
    expect(resolveStringBinding('', config())).toBeUndefined()
  })
})
