// Run by the host UI's vitest (frontend/ui/vite.config.ts names this directory in `test.include`):
// the kit is pure shared TypeScript with no build tooling of its own, and one runner covering both
// packages beats a second install here.
import { describe, expect, it } from 'vitest'
import { parseMapYaml } from './mapYaml'

// Trimmed from configs/examples/basic_nav/files/depot.yaml — a hand-written map, inline origin.
const HAND_WRITTEN = `
image: depot.pgm
resolution: 0.05
origin: [-7.14, -7.83, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
`

// Trimmed from the metamorphic dataset's environments/secorolab/maps/secorolab.yaml — scenery_builder
// writes the origin as a BLOCK sequence, and a nested `metadata:` block beside it. Reading only the
// inline spelling defaulted this map's origin to [0, 0, 0] and drew its grid 7.2 m from where it
// belongs, with every marker on it still in the right place.
const GENERATED = `
attributed_to: https://purl.org/secorolab/scenery_builder
derived_from:
- ../json-ld/floorplan.fpm.json
image: secorolab.pgm
free_thresh: 0.196
metadata:
  updated_at: '2026-07-27T14:23:13.088986'
negate: 0
occupied_thresh: 0.65
origin:
- -7.199999999999999
- -7.750000000000001
- 0
resolution: 0.05
`

describe('parseMapYaml', () => {
  it('reads a generated map, whose origin is a block sequence', () => {
    const meta = parseMapYaml(GENERATED)
    expect(meta.image).toBe('secorolab.pgm')
    expect(meta.resolution).toBe(0.05)
    expect(meta.origin).toEqual([-7.199999999999999, -7.750000000000001, 0])
  })

  it('reads a hand-written map, whose origin is inline', () => {
    const meta = parseMapYaml(HAND_WRITTEN)
    expect(meta.origin).toEqual([-7.14, -7.83, 0])
    expect(meta.occupied_thresh).toBe(0.65)
    expect(meta.free_thresh).toBe(0.196)
    expect(meta.negate).toBe(0)
  })

  it('applies the ROS defaults for the thresholds a map may omit', () => {
    const meta = parseMapYaml('image: m.pgm\nresolution: 0.1\norigin: [0, 0, 0]')
    expect(meta.negate).toBe(0)
    expect(meta.occupied_thresh).toBe(0.65)
    expect(meta.free_thresh).toBe(0.196)
  })

  it('takes [0, 0, 0] only when no origin is declared at all', () => {
    expect(parseMapYaml('image: m.pgm\nresolution: 0.1').origin).toEqual([0, 0, 0])
  })

  it('refuses a declared origin it cannot read, rather than drawing the map somewhere else', () => {
    expect(() => parseMapYaml('image: m.pgm\nresolution: 0.1\norigin: somewhere')).toThrow(/origin/)
  })

  it('refuses a map that declares no image or resolution', () => {
    expect(() => parseMapYaml('origin: [0, 0, 0]')).toThrow(/image/)
  })
})
