// The arithmetic behind the Details panel's charts.
//
// This is the half of a chart that can be wrong while looking right: a bar is drawn as a
// percentage of a bound, so a bound below the data makes a bar overflow its track, and a division
// by zero makes it NaN wide -- which renders as nothing at all, indistinguishable from "no data".
// The markup is in `DetailsCharts.tsx` and is deliberately dumb so that everything worth asserting
// lands here.

import { describe, expect, it } from 'vitest'
import {
  bandDomain,
  bandPoints,
  formatAxisDuration,
  linePercents,
  linePoints,
  niceMax,
  pct,
} from './detailsGeometry'

describe('niceMax', () => {
  it('is never below the data, or a bar overflows its track', () => {
    for (const values of [[2.48], [0.43], [1], [96], [0.001, 3.7]]) {
      expect(niceMax(values)).toBeGreaterThanOrEqual(Math.max(...values))
    }
  })

  it('rounds to 1/2/5 of a power of ten, so the axis end is a number to hold', () => {
    // Real peaks from basic-nav-details-example: 2.48 cores, 0.43 cores.
    expect(niceMax([2.48])).toBe(5)
    expect(niceMax([0.43])).toBe(0.5)
    expect(niceMax([1.2])).toBe(2)
    expect(niceMax([12])).toBe(20)
  })

  it('gives 1 for empty, zero and rubbish rather than 0', () => {
    // Dividing by the bound is the next thing that happens. A campaign that measured nothing must
    // draw empty tracks, not NaN-wide ones -- which vanish, and read as "no data".
    expect(niceMax([])).toBe(1)
    expect(niceMax([0, 0])).toBe(1)
    expect(niceMax([NaN, Infinity])).toBe(1)
  })
})

describe('pct', () => {
  it('clamps into 0..100 and never returns NaN', () => {
    expect(pct(2, 4)).toBe(50)
    expect(pct(8, 4)).toBe(100)
    expect(pct(-1, 4)).toBe(0)
    expect(pct(1, 0)).toBe(0)
    expect(pct(NaN, 4)).toBe(0)
  })
})

describe('linePoints', () => {
  it('scales y to the data rather than to zero', () => {
    // A search's objective lives in a narrow band well away from zero; anchoring the axis there
    // flattens the very curve the chart exists to show. So the lowest point sits on the floor.
    const points = linePoints(
      [
        { x: 0, y: 100.1 },
        { x: 1, y: 100.3 },
        { x: 2, y: 100.2 },
      ],
      100,
      50,
    )
    expect(points).toBe('0.00,50.00 50.00,0.00 100.00,25.00')
  })

  it('does not divide by zero when every value is identical', () => {
    // Centred rather than on the floor -- see 'a series that never moves' below for why.
    const points = linePoints(
      [
        { x: 0, y: 5 },
        { x: 1, y: 5 },
      ],
      100,
      50,
    )
    expect(points).toBe('0.00,25.00 100.00,25.00')
  })
})

describe('linePercents', () => {
  it('puts the dots on the same geometry as the line', () => {
    // The dots are DOM and the line is SVG, so they are placed by two different code paths
    // against one set of numbers. If these drift, every marker sits beside its own vertex.
    const data = [
      { x: 0, y: 1 },
      { x: 1, y: 3 },
      { x: 2, y: 2 },
    ]
    const svg = linePoints(data, 100, 100).split(' ').map((p) => p.split(',').map(Number))
    const dom = linePercents(data)
    dom.forEach((p, i) => {
      expect(p.x).toBeCloseTo(svg[i][0])
      // The DOM marker is positioned from the TOP, so its y is the flipped one.
      expect(100 - p.y).toBeCloseTo(svg[i][1])
    })
  })
})

describe('formatAxisDuration', () => {
  it('keeps the ends of a narrow range apart', () => {
    // The defect this exists for: basic_nav's runs span 101s to 114s, and `formatDuration` renders
    // both ends as "2m" -- an axis claiming every run took the same time.
    const span = 114 - 101
    expect(formatAxisDuration(101, span)).toBe('101s')
    expect(formatAxisDuration(114, span)).toBe('114s')
  })

  it('takes its unit from the span, not from the value', () => {
    // 101s is "1.7m" on an axis spanning half an hour and "101s" on one spanning 13 seconds. What
    // a reader takes off an axis is the difference between its ends, so the span decides.
    expect(formatAxisDuration(101, 1800)).toBe('1.7m')
    expect(formatAxisDuration(2400, 1800)).toBe('40m')
    expect(formatAxisDuration(3600, 6 * 3600)).toBe('1h')
    expect(formatAxisDuration(45 * 3600, 6 * 3600)).toBe('45h')
  })

  it('never renders a negative', () => {
    expect(formatAxisDuration(-5, 10)).toBe('0s')
  })
})

describe('a series that never moves', () => {
  it('draws through the middle, not along the floor', () => {
    // icra-random-recovery-3x5 found its maximum in batch 0 and held it: objective 1, 1, 1.
    // Anchoring at the minimum put every point at the bottom edge with the dots half-clipped by
    // it, which reads as "the objective collapsed" rather than "the objective held".
    expect(linePoints([{ x: 0, y: 1 }, { x: 1, y: 1 }, { x: 2, y: 1 }], 100, 50)).toBe(
      '0.00,25.00 50.00,25.00 100.00,25.00',
    )
    expect(linePercents([{ x: 0, y: 1 }, { x: 1, y: 1 }]).map((p) => p.y)).toEqual([50, 50])
  })

  it('still anchors a series that does move at its minimum', () => {
    // The centring must not cost the narrow-band behaviour it sits beside: a real spread still
    // fills the plot, or a 0.4% improvement would look identical to a doubling.
    expect(linePoints([{ x: 0, y: 1 }, { x: 1, y: 2 }], 100, 50)).toBe('0.00,50.00 100.00,0.00')
  })
})

describe('bandDomain', () => {
  it('keeps the unit interval for a rate-shaped objective', () => {
    expect(bandDomain([0.1, 0.4, 0.9])).toEqual([0, 1])
  })

  it('uses the combined range of every series it is given', () => {
    // The regression it guards: scaling the band and the line separately made the
    // best-so-far line leave the band it is supposed to sit inside.
    expect(bandDomain([2, 5, 9])).toEqual([2, 9])
  })

  it('widens a flat series so it reads as held rather than collapsed', () => {
    const [lo, hi] = bandDomain([7, 7, 7])
    expect(lo).toBeLessThan(7)
    expect(hi).toBeGreaterThan(7)
  })

  it('survives having nothing finite to scale', () => {
    expect(bandDomain([])).toEqual([0, 1])
    expect(bandDomain([NaN])).toEqual([0, 1])
  })
})

describe('bandPoints', () => {
  const bands = [
    { x: 0, lo: 0.2, hi: 0.6 },
    { x: 1, lo: 0.4, hi: 0.8 },
  ]

  it('traces the highs left-to-right and the lows back again', () => {
    const pts = bandPoints(bands, 100, 10, [0, 1])
    const xs = pts.split(' ').map((p) => Number(p.split(',')[0]))
    expect(xs).toEqual([0, 100, 100, 0])
    const ys = pts.split(' ').map((p) => Number(p.split(',')[1]))
    // y is inverted (0 at the top), so the `hi` edge sits above the `lo` edge.
    expect(ys[0]).toBeLessThan(ys[3])
  })

  it('shares its scale with linePoints, so the band cannot drift from the line', () => {
    const domain: [number, number] = [0, 1]
    const band = bandPoints(bands, 100, 10, domain).split(' ')
    const line = linePoints(bands.map((b) => ({ x: b.x, y: b.hi })), 100, 10, domain).split(' ')
    expect(band.slice(0, 2)).toEqual(line)
  })

  it('draws nothing for a single batch, which has no band to trace', () => {
    expect(bandPoints([{ x: 0, lo: 1, hi: 2 }], 100, 10, [0, 3])).toBe('')
  })
})
