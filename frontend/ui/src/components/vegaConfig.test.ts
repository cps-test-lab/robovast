// The dark theme survives a spec that overrides part of a config block.
//
// Worth its own test because the failure is silent and looks like nobody's fault: a spec sets
// `config.axis.labelFontSize`, a plain spread replaces the whole `axis` block, and the theme's
// `labelColor` goes with it — Vega then draws BLACK axis labels on a dark card. Nothing throws,
// nothing type-errors, and the spec never mentioned a colour. It shipped that way in the Details
// panel's four charts.

import { describe, expect, it } from 'vitest'
import { mergeVegaConfig } from './VegaLiteChart'

const theme = {
  background: 'transparent',
  axis: { labelColor: '#cfd8dc', titleColor: '#cfd8dc', gridColor: 'rgba(255,255,255,0.08)' },
  legend: { labelColor: '#cfd8dc' },
  range: { category: ['#2dd4bf', '#f0b429'] },
}

describe('mergeVegaConfig', () => {
  it('keeps the theme colours a spec overriding font sizes never mentioned', () => {
    const merged = mergeVegaConfig(theme, {
      axis: { labelFontSize: 9, labelLimit: 90, domain: false },
    })
    expect(merged.axis).toMatchObject({
      labelColor: '#cfd8dc',
      gridColor: 'rgba(255,255,255,0.08)',
      labelFontSize: 9,
      domain: false,
    })
  })

  it('lets a spec win where it does state a value', () => {
    const merged = mergeVegaConfig(theme, { axis: { labelColor: '#ff0000' } })
    expect((merged.axis as { labelColor: string }).labelColor).toBe('#ff0000')
  })

  it('leaves untouched blocks alone', () => {
    const merged = mergeVegaConfig(theme, { axis: { labelFontSize: 9 } })
    expect(merged.legend).toEqual(theme.legend)
    expect(merged.background).toBe('transparent')
  })

  it('replaces a scalar and an array wholesale rather than combining them', () => {
    // Two levels is the depth a Vega config has. A `range.category` palette must replace the
    // theme's, not concatenate with it, or the categorical colours drift out of the palette.
    const merged = mergeVegaConfig(theme, {
      background: '#000000',
      range: { category: ['#111111'] },
    })
    expect(merged.background).toBe('#000000')
    expect(merged.range).toEqual({ category: ['#111111'] })
  })
})
