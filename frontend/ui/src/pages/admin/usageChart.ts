// The rows and the spec behind the Admin page's usage chart, kept out of the component so the
// arithmetic that decides what is drawn can be tested without rendering Vega.
//
// Two readings per resource, and the distinction is the point of the chart: `reserved` is what the
// scheduler committed, `measured` is what is actually being consumed. A campaign that reserves nine
// cores per pod and uses two draws a wide gap, and that gap is what sizes the next sweep.
import { SERIES } from '@/colors'
import type { UsageSample } from '@/lib/robovastClient'

export type UsageKind = 'reserved' | 'measured'

/** One point of one series. Long format: a row per (sample × resource × kind) that HAS a value,
 *  which is what lets a lane reporting only one of the two simply draw one line — no `backend`
 *  branch anywhere in the chart. */
export interface UsageRow {
  // `VegaLiteChart` takes rows as `Record<string, unknown>[]` (a Vega datum is a record), which an
  // interface satisfies only with an index signature. The named fields below stay checked.
  [key: string]: unknown
  ms: number
  series: 'cpu' | 'memory'
  kind: UsageKind
  /** 0..1 of capacity. Fractions rather than absolutes because cores and bytes share no unit. */
  fraction: number
  /** What the tooltip says, e.g. `cpu measured`. Precomputed: the tooltip is the one place the
   *  numbers must be unambiguous, and a `calculate` string is a worse place to spell that out. */
  label: string
}

/** Rows for one resource of one sample, dropping what cannot be drawn.
 *
 *  A missing value is a **gap**, never a zero: a window whose measurement failed, or a lane with
 *  no such reading at all, must not be drawn as an idle cluster. Same for a zero capacity — a
 *  sample recorded while the node list was momentarily empty would otherwise put a NaN (0/0)
 *  through the line. */
function rowsFor(
  ms: number,
  series: 'cpu' | 'memory',
  capacity: number,
  values: Record<UsageKind, number | null | undefined>,
): UsageRow[] {
  if (!(capacity > 0)) return []
  const out: UsageRow[] = []
  for (const kind of ['measured', 'reserved'] as const) {
    const value = values[kind]
    if (value === null || value === undefined) continue
    out.push({ ms, series, kind, fraction: value / capacity, label: `${series} ${kind}` })
  }
  return out
}

/** The history as chart rows.
 *
 *  Epoch seconds on the wire (the convention the status models use) and Vega-Lite reads a bare
 *  number as milliseconds, so the conversion happens here rather than in a transform nobody would
 *  connect to the cause. */
export function usageRows(samples: UsageSample[]): UsageRow[] {
  return samples.flatMap((s) => {
    const ms = s.at * 1000
    return [
      ...rowsFor(ms, 'cpu', s.cpu_capacity, {
        measured: s.cpu_measured,
        reserved: s.cpu_reserved,
      }),
      ...rowsFor(ms, 'memory', s.memory_capacity_bytes, {
        measured: s.memory_measured_bytes,
        reserved: s.memory_reserved_bytes,
      }),
    ]
  })
}

const TOOLTIP = [
  { field: 'ms', type: 'temporal', format: '%Y-%m-%d %H:%M', title: 'time' },
  { field: 'label', type: 'nominal', title: null },
  { field: 'fraction', type: 'quantitative', format: '.0%', title: 'of capacity' },
]

/** Measured as a filled area, reserved as a dashed line over it, one colour per resource.
 *
 *  Reserved is the *boundary* of what was granted rather than a second quantity, so it is drawn as
 *  a line and not a second fill — two overlapping fills per resource would read as four unrelated
 *  series. Colour therefore stays two entries in the legend, and the caption names the encoding. */
export const USAGE_SPEC = {
  encoding: {
    x: {
      field: 'ms',
      type: 'temporal',
      title: null,
      axis: { grid: false, tickCount: 6 },
    },
    // Pinned to 0..1. An autoscaled axis draws a cluster at 3% exactly like one at 90%, which is
    // the only distinction this chart exists to make.
    //
    // `stack: null` is load-bearing for the area layer: Vega-Lite stacks areas grouped by a
    // nominal colour by default, so cpu at 40% and memory at 30% would be drawn as a band
    // reaching 70% — a reading of a cluster that does not exist.
    y: {
      field: 'fraction',
      type: 'quantitative',
      title: null,
      stack: null,
      scale: { domain: [0, 1] },
      axis: { format: '%', values: [0, 0.25, 0.5, 0.75, 1] },
    },
    // The domain is stated so cpu is always the first colour. Left to the data it would depend on
    // which series a given window happened to carry first, and the two would swap as you toggled
    // between 1h and 24h.
    color: {
      field: 'series',
      type: 'nominal',
      title: null,
      scale: { domain: ['cpu', 'memory'], range: [SERIES[0], SERIES[1]] },
      legend: { orient: 'top', direction: 'horizontal', offset: 0 },
    },
  },
  layer: [
    {
      transform: [{ filter: "datum.kind === 'measured'" }],
      mark: { type: 'area', interpolate: 'monotone', clip: true, opacity: 0.3, line: false },
      encoding: { tooltip: TOOLTIP },
    },
    {
      transform: [{ filter: "datum.kind === 'reserved'" }],
      mark: {
        type: 'line',
        interpolate: 'monotone',
        clip: true,
        strokeWidth: 1.5,
        strokeDash: [4, 3],
      },
      encoding: { tooltip: TOOLTIP },
    },
  ],
}
