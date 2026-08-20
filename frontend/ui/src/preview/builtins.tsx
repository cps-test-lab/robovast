// Host-native previews for the built-in variation types (no plugin loading). Each receives the
// declared variation params + this config's resolved value for the varied parameter, and draws the
// distribution/values with a marker at the resolved value. Registered by variation-type name.
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { ComponentType } from 'react'
import type { Data } from 'plotly.js'
import { MARK, SERIES, withAlpha } from '@/colors'
import { MiniPlot } from './plot'

export interface PreviewProps {
  params: Record<string, unknown>
  /** This config's resolved value for the varied parameter (params.name), if scalar. */
  value?: unknown
}

const num = (v: unknown): number | undefined =>
  typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' && !isNaN(Number(v)) ? Number(v) : undefined

const marker = (x: number, yTop: number): Data => ({
  x: [x, x],
  y: [0, yTop],
  mode: 'lines',
  line: { color: MARK, width: 2, dash: 'dash' },
  hoverinfo: 'x',
})

function UniformPreview({ params, value }: PreviewProps) {
  const min = num(params.min)
  const max = num(params.max)
  const v = num(value)
  if (min == null || max == null || max <= min) {
    return <Caption>uniform{v != null ? `: value=${v}` : ''}</Caption>
  }
  const h = 1 / (max - min)
  const data: Data[] = [
    { x: [min, min, max, max], y: [0, h, h, 0], fill: 'toself', mode: 'lines',
      line: { color: SERIES[0] }, fillcolor: withAlpha(SERIES[0], 0.25), hoverinfo: 'skip' },
  ]
  if (v != null) data.push(marker(v, h))
  return <MiniPlot data={data} layout={{ title: { text: `uniform(${min}, ${max})` } }} />
}

function GaussianPreview({ params, value }: PreviewProps) {
  const mean = num(params.mean)
  const std = num(params.std)
  const v = num(value)
  if (mean == null || std == null || std <= 0) {
    return <Caption>gaussian{v != null ? `: value=${v}` : ''}</Caption>
  }
  const lo = mean - 4 * std
  const hi = mean + 4 * std
  const xs: number[] = []
  const ys: number[] = []
  const norm = 1 / (std * Math.sqrt(2 * Math.PI))
  for (let i = 0; i <= 80; i++) {
    const x = lo + ((hi - lo) * i) / 80
    xs.push(x)
    ys.push(norm * Math.exp(-0.5 * ((x - mean) / std) ** 2))
  }
  const data: Data[] = [
    { x: xs, y: ys, fill: 'tozeroy', mode: 'lines', line: { color: SERIES[0] },
      fillcolor: withAlpha(SERIES[0], 0.2), hoverinfo: 'skip' },
  ]
  if (v != null) data.push(marker(v, norm))
  return <MiniPlot data={data} layout={{ title: { text: `gaussian(μ=${mean}, σ=${std})` } }} />
}

function ListPreview({ params, value }: PreviewProps) {
  const values = Array.isArray(params.values) ? params.values : []
  return (
    <Stack spacing={0.5}>
      <Caption>{String(params.name ?? 'values')}</Caption>
      <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
        {values.map((val, i) => {
          const label = typeof val === 'object' ? JSON.stringify(val) : String(val)
          const active = String(val) === String(value)
          return <Chip key={i} size="small" label={label} color={active ? 'primary' : 'default'}
            variant={active ? 'filled' : 'outlined'} />
        })}
      </Box>
    </Stack>
  )
}

function Caption({ children }: { children: React.ReactNode }) {
  return (
    <Typography variant="caption" color="text.secondary">
      {children}
    </Typography>
  )
}

/** Built-in variation-type name → host-native preview component. */
export const BUILTIN_PREVIEWS: Record<string, ComponentType<PreviewProps>> = {
  ParameterVariationDistributionUniform: UniformPreview,
  ParameterVariationDistributionGaussian: GaussianPreview,
  ParameterVariationList: ListPreview,
}
