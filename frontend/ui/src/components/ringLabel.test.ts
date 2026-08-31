import { describe, expect, it } from 'vitest'
import { ringLabel, ringLabelField, ringLabelWidth } from './StatusView'

const CHECK = '\u2713'

/** The widest thing the hole ever shows.
 *
 *  `99%` is the ceiling for a share: an exact full one becomes a tick instead, which is what lets
 *  the others keep their sign. A round count reaches three digits on any search worth watching
 *  (`pb2-s1` ran 30 rounds); four would be a thousand-round search, which nothing here produces.
 */
const WIDEST = ['99%', '999']

// This is the check that was missing when `67%` shipped at 10px in a 16.8px hole -- it overflowed
// onto the arc, and was reported as the arc colour being too bright. It needed multiplication, not
// a browser.
describe('the ring label fits its field', () => {
  it.each(WIDEST)('%s fits', (label) => {
    expect(ringLabelWidth(label)).toBeLessThan(ringLabelField())
  })

  it('leaves real headroom rather than fitting by a hair', () => {
    // The width model is a per-glyph estimate, not measured text, so a label that only just fits
    // is one the real font may overflow.
    for (const label of WIDEST) {
      expect(ringLabelWidth(label)).toBeLessThan(ringLabelField() * 0.95)
    }
  })

  it('rejects the three-digit percent, which is why 100% is a tick', () => {
    // The one value that cannot fit at any legible size. Replacing it costs one glyph and buys
    // the `%` sign back for every other value.
    expect(ringLabelWidth('100%')).toBeGreaterThan(ringLabelField())
  })
})

describe('ringLabel', () => {
  it('keeps the sign for a partial share', () => {
    expect(ringLabel(0.67, 5)).toBe('67%')
    expect(ringLabel(0.015, 5)).toBe('2%')
  })

  it('marks an exhausted budget with a tick instead of 100%', () => {
    expect(ringLabel(1, 8)).toBe(CHECK)
  })

  it('does not hand the tick to something merely close', () => {
    // `Math.round` would call 99.6% a hundred and claim a budget spent while a run of it
    // remains. Capped at 99 short of exhaustion -- understating, the same direction ringBudget
    // clamps in.
    expect(ringLabel(0.996, 5)).toBe('99%')
    expect(ringLabel(0.9999, 5)).toBe('99%')
  })

  it('shows the round count when nothing bounds the search', () => {
    // No denominator, so no share -- and no sign either, which the hover disambiguates.
    expect(ringLabel(null, 12)).toBe('12')
  })
})
