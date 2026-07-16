// Renders a Vega-Lite spec over query-result rows. The rows are injected as the spec's
// `data.values`, so a spec only declares its `mark`/`encoding` (field names = query column
// aliases) — used by both the eval chart builder (3b) and user-declared plot specs (3c).
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

export function VegaLiteChart({
  spec,
  rows,
  height = 260,
}: {
  spec: Record<string, unknown>
  rows: Record<string, unknown>[]
  height?: number
}) {
  const full = {
    width: 'container',
    height,
    ...spec,
    data: { values: rows },
    config: { ...DARK_CONFIG, ...((spec.config as object) ?? {}) },
  } as VisualizationSpec
  return <VegaLite spec={full} actions={false} style={{ width: '100%' }} />
}
