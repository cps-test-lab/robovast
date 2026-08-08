// Renders a Vega-Lite spec over rows the caller supplies. Two ways to bind them:
//
//   rows      -- injected as the spec's `data.values`, so a spec only declares its `mark`/`encoding`
//                (field names = query column aliases). Used by the eval chart builder and by
//                user-declared `evaluation.plots` specs.
//   datasets  -- named datasets handed to Vega's view, for a spec that references `{name: ...}`.
//                Updating one dataset re-renders it without re-parsing the spec, which is what lets
//                the run-view `vega` panel move a playback cursor at clock rate.
//
// Pass one or the other; `datasets` wins if both are given.
import { VegaLite } from 'react-vega'
import type { VisualizationSpec } from 'react-vega'

// Dark theme matching the app (teal/amber), applied unless the spec overrides it.
const DARK_CONFIG = {
  background: 'transparent',
  axis: {
    labelColor: '#cfd8dc',
    titleColor: '#cfd8dc',
    gridColor: 'rgba(255,255,255,0.08)',
    domainColor: 'rgba(255,255,255,0.2)',
    tickColor: 'rgba(255,255,255,0.2)',
  },
  legend: { labelColor: '#cfd8dc', titleColor: '#cfd8dc' },
  title: { color: '#cfd8dc' },
  view: { stroke: 'transparent' },
  range: { category: ['#2dd4bf', '#f0b429', '#4ade80', '#f48fb1', '#60a5fa', '#c084fc'] },
}

// `width`/`height` are properties of a unit or layer spec -- Vega-Lite warns "Width \"container\"
// only works for single views and layered views" on a concat/facet/repeat spec, whose children carry
// their own size. So the convenience defaults below are only applied to specs that can take them.
const CONTAINER_KEYS = ['vconcat', 'hconcat', 'concat', 'facet', 'repeat']
const takesSize = (spec: Record<string, unknown>) => !CONTAINER_KEYS.some((k) => k in spec)

const CONCAT_KEYS = ['vconcat', 'hconcat', 'concat'] as const

/** Make a concat spec fill its container, since it cannot take a top-level width.
 *
 *  Responsive sizing is pushed down instead: `width: "container"` on each CHILD compiles cleanly (a
 *  child is a unit/layer spec) and every child reads the same enclosing element, so stacked charts
 *  stay aligned. `autosize` has to be set explicitly here -- Vega-Lite infers `fit-x` by itself only
 *  for single and layered views, and without it the plot area would be the *full* container width
 *  with the axis labels pushing the total past it (a horizontal scrollbar rather than a fitted
 *  chart). An author-declared width or autosize still wins.
 *
 *  `facet`/`repeat` are deliberately not handled: their child is templated, and container width is
 *  not supported there. Such a spec keeps whatever size it declares. */
function fillContainerWidth(spec: Record<string, unknown>): Record<string, unknown> {
  const key = CONCAT_KEYS.find((k) => Array.isArray(spec[k]))
  if (!key) return spec
  const children = (spec[key] as Record<string, unknown>[]).map((child) =>
    'width' in child ? child : { ...child, width: 'container' },
  )
  return { autosize: { type: 'fit-x', contains: 'padding' }, ...spec, [key]: children }
}

export function VegaLiteChart({
  spec,
  rows,
  datasets,
  height = 260,
}: {
  spec: Record<string, unknown>
  rows?: Record<string, unknown>[]
  datasets?: Record<string, Record<string, unknown>[]>
  height?: number
}) {
  const full = {
    ...(takesSize(spec) ? { width: 'container', height } : {}),
    ...(takesSize(spec) ? spec : fillContainerWidth(spec)),
    // With named datasets the spec carries its own `data` references, so injecting values here
    // would override them.
    ...(datasets ? {} : { data: { values: rows ?? [] } }),
    config: { ...DARK_CONFIG, ...((spec.config as object) ?? {}) },
  } as VisualizationSpec
  return <VegaLite spec={full} data={datasets} actions={false} style={{ width: '100%' }} />
}
