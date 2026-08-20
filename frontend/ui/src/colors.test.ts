// `withAlpha` does its own hex parsing, so the channel order is worth pinning: a red/blue swap
// produces a colour that is still plausibly in the scheme's family and so survives a glance at
// the UI. The rest of colors.ts is constants and needs no test.

import { describe, expect, it } from 'vitest'
import { ACCENT, SERIES, accent, withAlpha } from './colors'

describe('withAlpha', () => {
  it('keeps the channels in order', () => {
    expect(withAlpha('#a8ffcf', 0.12)).toBe('rgba(168, 255, 207, 0.12)')
  })

  it('does not confuse red with blue on an asymmetric colour', () => {
    // 0x2d != 0xbf, so a swapped parse shows up here and not in a grey.
    expect(withAlpha(SERIES[0], 0.25)).toBe('rgba(45, 212, 191, 0.25)')
  })

  it('agrees with the accent helper on the accent', () => {
    expect(withAlpha(ACCENT, 0.28)).toBe(accent(0.28))
  })
})
