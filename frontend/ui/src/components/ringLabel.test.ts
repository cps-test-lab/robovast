import { describe, expect, it } from 'vitest'
import { ringHoleWidth, ringLabelWidth } from './StatusView'

/** The widest thing the hole ever shows.
 *
 *  `100` is the ceiling for a budget share (clamped in `ringBudget`), and a round count reaches
 *  three digits on any search worth watching -- `pb2-s1` ran 30 rounds. Four would be a search
 *  of a thousand rounds, which nothing here produces.
 */
const WIDEST = ['100', '999']

// This is the check that was missing. `67%` was chosen for the hole on the reasoning that a
// percent is "at most four characters whatever the criterion is" -- true, and irrelevant: four
// characters do not fit. It shipped, and a reader found it by looking, reporting the arc colour
// as too bright when the cause was the label's outer edges sitting on the arc.
describe('the ring label fits its hole', () => {
  it.each(WIDEST)('%s fits', (label) => {
    expect(ringLabelWidth(label)).toBeLessThan(ringHoleWidth())
  })

  it('rejects the label that shipped, so the regression cannot come back', () => {
    // `67%` at the old 10px was ~20px against a ~17px hole. The `%` is what does not fit: it is
    // the widest glyph in the string, and dropping it is what made the value legible.
    expect(ringLabelWidth('67%')).toBeGreaterThan(ringHoleWidth())
    expect(ringLabelWidth('100%')).toBeGreaterThan(ringHoleWidth())
  })

  it('leaves real headroom rather than fitting by a hair', () => {
    // The width model is an estimate, so a label that only just fits is one the real font may
    // overflow. 15% of the hole is the margin the current values actually have.
    for (const label of WIDEST) {
      expect(ringLabelWidth(label)).toBeLessThan(ringHoleWidth() * 0.95)
    }
  })
})
