import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import Typography from '@mui/material/Typography'
import { SERIES } from '@/colors'
import { robovast, type UsageHistory } from '@/lib/robovastClient'
import { formatLocalTime } from '@/lib/time'
import { VegaLiteChart } from '@/components/VegaLiteChart'

type Window = '1h' | '24h'

// CPU and memory as a fraction of capacity. Fractions rather than absolutes because cores
// and bytes share no unit — one axis for both would be two scales — and because "how full
// is this thing?" is the question the sidebar meters already answer, so it reads the same
// way here.
const SPEC = {
  // The guards live in the spec, not in TS: a sample recorded while the node list was
  // momentarily empty has capacity 0, and 0/0 would put a NaN through the line rather than
  // the gap that is the truth.
  transform: [
    {
      calculate: 'datum.cpu_capacity > 0 ? datum.cpu_used / datum.cpu_capacity : null',
      as: 'cpu',
    },
    {
      calculate:
        'datum.memory_capacity_bytes > 0'
        + ' ? datum.memory_used_bytes / datum.memory_capacity_bytes : null',
      as: 'memory',
    },
    { fold: ['cpu', 'memory'], as: ['series', 'fraction'] },
  ],
  mark: { type: 'line', interpolate: 'monotone', strokeWidth: 1.5, clip: true },
  encoding: {
    x: {
      field: 'ms',
      type: 'temporal',
      title: null,
      axis: { grid: false, tickCount: 6 },
    },
    // Pinned to 0..1. An autoscaled axis draws a cluster at 3% exactly like one at 90%,
    // which is the only distinction this chart exists to make.
    y: {
      field: 'fraction',
      type: 'quantitative',
      title: null,
      scale: { domain: [0, 1] },
      axis: { format: '%', values: [0, 0.25, 0.5, 0.75, 1] },
    },
    // The domain is stated so cpu is always the first colour. Left to fold order it would
    // depend on which series a given window happened to carry first, and the two would
    // swap as you toggled between 1h and 24h.
    color: {
      field: 'series',
      type: 'nominal',
      title: null,
      scale: { domain: ['cpu', 'memory'], range: [SERIES[0], SERIES[1]] },
      legend: { orient: 'top', direction: 'horizontal', offset: 0 },
    },
    tooltip: [
      { field: 'ms', type: 'temporal', format: '%Y-%m-%d %H:%M', title: 'time' },
      { field: 'series', type: 'nominal', title: null },
      { field: 'fraction', type: 'quantitative', format: '.0%', title: 'used' },
    ],
  },
}

// What the record actually covers, said out loud. The ring lives in the serving process,
// so a service started ten minutes ago has ten minutes of history and an empty 24h view is
// the truth rather than a broken chart — the one thing this must never imply is a period
// it cannot speak for.
function coverage(data: UsageHistory, window: Window): string {
  const spanS = window === '1h' ? 3600 : 24 * 3600
  const startedMs = data.service_started_at * 1000
  const volatile = 'kept in memory only, so a restart clears it'
  if (!data.samples.length) {
    return `no readings yet — sampling every ${data.sample_interval_s}s, ${volatile}`
  }
  if (startedMs > Date.now() - spanS * 1000) {
    // `formatLocalTime` and not `formatLocalClock`: the latter formats a time *relative to
    // now*, which would render "the service started" as a moment in the future.
    return `history begins ${formatLocalTime(new Date(startedMs).toISOString())}, when this`
      + ` service started — ${volatile}`
  }
  return `one point every ${Math.round(data.step_s)}s — ${volatile}`
}

export function UsageHistoryChart() {
  const [window, setWindow] = useState<Window>('1h')
  const history = useQuery({
    queryKey: ['usageHistory', window],
    queryFn: () => robovast.usageHistory(window),
    // The recorder samples every 30s, so polling faster only costs round trips. Refetch on
    // focus for the reason the sidebar meters do: a stale chart is what you look at first
    // on coming back to the tab.
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    retry: false,
  })

  return (
    <Stack spacing={1}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Typography variant="subtitle2">Cluster usage</Typography>
        <ToggleButtonGroup
          size="small"
          exclusive
          value={window}
          onChange={(_, next: Window | null) => next && setWindow(next)}
        >
          <ToggleButton value="1h">1h</ToggleButton>
          <ToggleButton value="24h">24h</ToggleButton>
        </ToggleButtonGroup>
      </Stack>
      {history.isSuccess ? (
        <>
          <Box sx={{ minHeight: 200 }}>
            <VegaLiteChart
              spec={SPEC}
              // Epoch seconds on the wire (the convention the status models use), and
              // Vega-Lite reads a bare number as milliseconds — so the conversion happens
              // here rather than as a fourth transform nobody would connect to the cause.
              rows={history.data.samples.map((s) => ({ ...s, ms: s.at * 1000 }))}
              height={200}
            />
          </Box>
          <Typography variant="caption" color="text.secondary">
            {coverage(history.data, window)}
          </Typography>
        </>
      ) : (
        <Typography variant="caption" color="text.disabled">
          {history.isError ? 'could not read usage history' : 'loading…'}
        </Typography>
      )}
    </Stack>
  )
}
