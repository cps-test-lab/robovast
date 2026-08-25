// A small Plotly component built on the light `plotly.js-dist-min` bundle (via react-plotly.js's
// factory), themed for the dark app. Used by the built-in host-native variation previews.
import Plotly from 'plotly.js-dist-min'
import createPlotlyComponent from 'react-plotly.js/factory'
import type { Data, Layout } from 'plotly.js'

import { CHART_LABEL } from '@/colors'
const Plot = createPlotlyComponent(Plotly)

const BASE_LAYOUT: Partial<Layout> = {
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: { color: CHART_LABEL, size: 11 },
  margin: { l: 36, r: 10, t: 24, b: 28 },
  showlegend: false,
  height: 180,
}

export function MiniPlot({ data, layout }: { data: Data[]; layout?: Partial<Layout> }) {
  return (
    <Plot
      data={data}
      layout={{ ...BASE_LAYOUT, ...layout }}
      config={{ displayModeBar: false, responsive: true }}
      style={{ width: '100%', height: '180px' }}
      useResizeHandler
    />
  )
}
