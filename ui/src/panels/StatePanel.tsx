// StatePanel (type `state`): the barest state-at-time rendering -- a live key/value list of chosen
// columns at the current playback time. Reactive (re-renders on each clock change) and blind to the
// data source, like every panel over a TimeSeriesSource.
//
// Bindings (vast visualization.panels):
//   source: { table, time_column }
//   fields: [ { column, label?, unit? }, ... ]

import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { registerPanel } from '@/lib/dashboard/registry'
import { useClock } from '@/lib/dashboard/clock'
import { useTimeSeries, type TimeSeriesBinding } from '@/lib/dashboard/timeSeries'
import type { PanelProps } from '@/lib/dashboard/types'

interface FieldCfg {
  column: string
  label?: string
  unit?: string
}

function fmt(v: unknown): string {
  const n = Number(v)
  if (Number.isFinite(n)) return Math.abs(n) >= 1000 || (n !== 0 && Math.abs(n) < 0.01) ? n.toExponential(2) : n.toFixed(3)
  return v == null || v === '' ? '—' : String(v)
}

function StatePanel({ spec, clock, data }: PanelProps) {
  const source = (spec.config.source ?? {}) as TimeSeriesBinding
  const fields = (spec.config.fields ?? []) as FieldCfg[]
  const { t } = useClock(clock)

  const query = useTimeSeries(source, data, fields.map((f) => f.column))

  if (query.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (query.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(query.error as Error).message}
      </Alert>
    )
  if (!fields.length)
    return (
      <Alert severity="info" sx={{ m: 1 }}>
        No <code>fields</code> configured. Add <code>fields: [{'{'} column: ... {'}'}]</code> to this panel.
      </Alert>
    )

  const row = query.data?.at(t) ?? null

  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: 1 }}>
      {fields.map((f) => (
        <Box
          key={f.column}
          sx={{ display: 'flex', justifyContent: 'space-between', gap: 2, py: 0.25, fontSize: 13 }}
        >
          <Box component="span" sx={{ color: 'text.secondary' }}>
            {f.label ?? f.column}
          </Box>
          <Box component="span" sx={{ fontVariantNumeric: 'tabular-nums' }}>
            {fmt(row?.[f.column])}
            {f.unit ? <Box component="span" sx={{ color: 'text.secondary', ml: 0.5 }}>{f.unit}</Box> : null}
          </Box>
        </Box>
      ))}
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'state',
    label: 'State',
    defaultPosition: { anchor: 'top-left', width: 240 },
    resizable: true,
    minimizable: true,
  },
  component: StatePanel,
})

export default StatePanel
