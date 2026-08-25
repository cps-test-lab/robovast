// Run by the host UI's vitest (frontend/ui/vite.config.ts names this directory in `test.include`):
// the kit is pure shared TypeScript with no build tooling of its own, and one runner covering both
// packages beats a second install here.
import { describe, expect, it } from 'vitest'
import { numberSequence, parseFlatYaml } from './flatYaml'

describe('parseFlatYaml', () => {
  it('reads an inline sequence', () => {
    expect(parseFlatYaml('origin: [-7.14, -7.83, 0]').origin).toEqual([-7.14, -7.83, 0])
  })

  it('reads a block sequence, which is what a generator writes', () => {
    const out = parseFlatYaml(['origin:', '- -7.2', '- -7.75', '- 0', 'resolution: 0.05'].join('\n'))
    expect(out.origin).toEqual([-7.2, -7.75, 0])
    expect(out.resolution).toBe(0.05)
  })

  it('keeps a block sequence of strings as strings', () => {
    const lines = ['derived_from:', '- ../json-ld/floorplan.fpm.json', 'negate: 0']
    const out = parseFlatYaml(lines.join('\n'))
    expect(out.derived_from).toEqual(['../json-ld/floorplan.fpm.json'])
    expect(out.negate).toBe(0)
  })

  it('skips a nested mapping instead of flattening it, so nothing shadows a real key', () => {
    const out = parseFlatYaml(
      ['resolution: 0.05', 'metadata:', "  updated_at: '2026-07-27'", '  resolution: 999'].join('\n'),
    )
    expect(out.resolution).toBe(0.05)
    expect(out.updated_at).toBeUndefined()
    expect(out.metadata).toEqual([])
  })

  it('distinguishes an absent key from one that opens an empty block', () => {
    const out = parseFlatYaml('origin:')
    expect(out.origin).toEqual([])
    expect(out.missing).toBeUndefined()
  })

  it('drops comments and blank lines, and keeps a value containing a colon', () => {
    const out = parseFlatYaml(['# a map', '', 'image: map.pgm  # the raster', 'url: a:b'].join('\n'))
    expect(out.image).toBe('map.pgm')
    expect(out.url).toBe('a:b')
  })
})

describe('numberSequence', () => {
  it('accepts either declared length', () => {
    expect(numberSequence([1, 2], 'origin', [2, 3])).toEqual([1, 2])
    expect(numberSequence([1, 2, 3], 'origin', [2, 3])).toEqual([1, 2, 3])
  })

  it('falls back only when the key is absent', () => {
    expect(numberSequence(undefined, 'origin', [2, 3], [0, 0, 0])).toEqual([0, 0, 0])
  })

  it('throws on a key that is present but unreadable, rather than defaulting it', () => {
    expect(() => numberSequence('nonsense', 'origin', [2, 3], [0, 0, 0])).toThrow(/origin/)
    expect(() => numberSequence([], 'origin', [2, 3], [0, 0, 0])).toThrow(/origin/)
    expect(() => numberSequence([1, 'x'], 'origin', [2, 3], [0, 0, 0])).toThrow(/origin/)
    expect(() => numberSequence([1, 2, 3, 4], 'origin', [2, 3], [0, 0, 0])).toThrow(/2 or 3/)
  })

  it('throws when a key is absent and no fallback is offered', () => {
    expect(() => numberSequence(undefined, 'extent', [2])).toThrow(/extent/)
  })
})
