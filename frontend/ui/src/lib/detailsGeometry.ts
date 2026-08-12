// Geometry for the Details panel's charts, once they stopped being Vega specs.
//
// The panel's five charts are fixed and hand-designed for a ~190x110px column, so a spec compiler
// bought nothing and cost a great deal: its bundle (the only reason the panel lazy-loads at all),
// hardcoded hex colours instead of theme tokens, its own tooltip idiom beside MUI's, and a class of
// silent defects where a valid spec draws the wrong thing -- axis labels rendered black on the dark
// card because a `config.axis` override replaced the theme's block.
//
// What is left is this file: the arithmetic that turns numbers into percentages, which is all those
// charts ever needed. Pure and unit-tested, because it is the part that can be wrong while looking
// right. The DOM is in `DetailsCharts.tsx`.

/** A rounded upper bound for an axis, at or above `max`.
 *
 *  Bars are drawn as a percentage of this, so it has to be >= every value or a bar overflows its
 *  track. Rounded to 1/2/5 x a power of ten so the number a reader infers from a full-width bar is
 *  one they can hold: 2.48 cores becomes 5 rather than 2.48, and 0.43 becomes 0.5.
 *
 *  Zero and non-finite input give 1 rather than 0 -- dividing by the bound is the next thing that
 *  happens, and a chart of all-zero values should draw empty tracks, not NaN widths. */
export function niceMax(values: number[]): number {
  const max = values.reduce((m, v) => (Number.isFinite(v) && v > m ? v : m), 0)
  if (!(max > 0)) return 1
  const magnitude = 10 ** Math.floor(Math.log10(max))
  for (const step of [1, 2, 5, 10]) {
    if (max <= step * magnitude) return step * magnitude
  }
  return 10 * magnitude
}

/** `value` as a 0..100 percentage of `max`, clamped. The unit every bar's width is set in. */
export function pct(value: number, max: number): number {
  if (!Number.isFinite(value) || !(max > 0)) return 0
  return Math.min(100, Math.max(0, (value / max) * 100))
}

/** A duration for an AXIS: the unit is chosen from the range being labelled, not from the value.
 *
 *  `format.ts`'s `formatDuration` is deliberately coarse — it answers "how long did this run take"
 *  in one glanceable token, and rounds 101s and 114s both to "2m". On an axis that is a defect: the
 *  two ends of the histogram read `2m … 2m`, which says the campaign's runs all took the same time
 *  when they varied by 13 seconds.
 *
 *  So the unit comes from the span. A 13-second spread is labelled in seconds however large the
 *  values are, because what a reader is reading off an axis is the DIFFERENCE between its ends. */
export function formatAxisDuration(value: number, span: number): string {
  const v = Math.max(0, value)
  // Ten minutes' worth of seconds is at most four digits, which still fits a 9px label — and
  // seconds are what keeps the ends of a narrow range distinguishable.
  if (span < 600) return `${Math.round(v)}s`
  if (span < 90 * 60) {
    const m = v / 60
    return `${m < 10 ? Math.round(m * 10) / 10 : Math.round(m)}m`
  }
  const h = v / 3600
  return `${h < 10 ? Math.round(h * 10) / 10 : Math.round(h)}h`
}

export interface Point {
  x: number
  y: number
}

/** SVG `points` for a straight polyline, for a series whose values genuinely interpolate.
 *
 *  A search's best-so-far is such a series: it is one measurement per round, and the line between
 *  two rounds is a reading aid rather than a claim about a moment in between. `yMin` is taken from
 *  the data instead of forced to zero, because a search's interesting range is usually a narrow
 *  band well away from it and anchoring at zero flattens the very curve the chart exists to show. */
export function linePoints(
  points: Point[],
  width: number,
  height: number,
  domain?: [number, number],
): string {
  if (!points.length) return ''
  const xs = points.map((p) => p.x)
  const xMin = Math.min(...xs)
  const xSpan = Math.max(...xs) - xMin || 1
  const heights = fractions(points, domain)
  return points
    .map((p, i) => {
      const x = ((p.x - xMin) / xSpan) * width
      return `${x.toFixed(2)},${(height * (1 - heights[i])).toFixed(2)}`
    })
    .join(' ')
}

/** Each point's height as a 0..1 fraction, 0 at the bottom.
 *
 *  A CONSTANT series sits in the middle, not on the floor. Anchoring at the minimum is right when
 *  values differ — that is what stops a narrow band from being flattened — but when they are all
 *  equal the minimum IS the value, and every point lands at zero: a search whose objective never
 *  moved drew its line along the bottom edge with its dots half-clipped by it, which reads as "the
 *  objective collapsed" rather than "the objective held". Centring says "no change" without
 *  claiming a direction. */
function fractions(points: Point[], domain?: [number, number]): number[] {
  if (domain) {
    // An explicit scale wins outright, including for a constant series: on a fixed axis a flat
    // line at 1 and a flat line at 0.2 are different findings, and centring both would erase
    // exactly the difference the fixed scale was chosen to show.
    const [lo, hi] = domain
    const span = hi - lo || 1
    return points.map((p) => Math.min(1, Math.max(0, (p.y - lo) / span)))
  }
  const ys = points.map((p) => p.y)
  const yMin = Math.min(...ys)
  const ySpan = Math.max(...ys) - yMin
  if (ySpan === 0) return points.map(() => 0.5)
  return points.map((p) => (p.y - yMin) / ySpan)
}

/** The y scale for an objective: the unit interval when the values live in it, else the data's own
 *  range.
 *
 *  A normalized objective -- a rate, a fraction, a success probability -- carries meaning in its
 *  ABSOLUTE position: `best = 1` is "as good as it can get", and a curve read against 0..1 says how
 *  much of the available room the search actually took. Anchoring such a series to its own range
 *  throws that away, and draws 0.98 -> 0.99 identically to 0.1 -> 0.9.
 *
 *  An objective on any other scale (metres of path, seconds of settling time) has no such meaning:
 *  0 is not special, its interesting band is usually narrow and far from zero, and forcing a fixed
 *  domain would flatten it into a straight line or push it off the top. Those keep the data range.
 *  Either way the plot's y labels state the range in force, so the reader is never guessing. */
export function objectiveDomain(values: number[]): [number, number] | undefined {
  if (!values.length) return undefined
  return values.every((v) => v >= 0 && v <= 1) ? [0, 1] : undefined
}

/** Where each point lands, as 0..100 percentages — for placing DOM nodes (a dot, a label) on the
 *  same geometry the SVG line uses, without a second scale to keep in step. */
export function linePercents(points: Point[], domain?: [number, number]): Point[] {
  if (!points.length) return []
  const xs = points.map((p) => p.x)
  const xMin = Math.min(...xs)
  const xSpan = Math.max(...xs) - xMin || 1
  const heights = fractions(points, domain)
  return points.map((p, i) => ({
    x: ((p.x - xMin) / xSpan) * 100,
    y: heights[i] * 100,
  }))
}
