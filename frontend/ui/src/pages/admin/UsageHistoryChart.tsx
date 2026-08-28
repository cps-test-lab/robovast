import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import Typography from '@mui/material/Typography'
import { robovast, type UsageHistory } from '@/lib/robovastClient'
import { formatLocalTime } from '@/lib/time'
import { VegaLiteChart } from '@/components/VegaLiteChart'
import { USAGE_SPEC, usageRows } from './usageChart'

type Window = '1h' | '24h'

// CPU and memory as a fraction of capacity, each as a measured fill under a reserved line — see
// `usageChart.ts` for the rows and the spec. Fractions rather than absolutes because cores and
// bytes share no unit — one axis for both would be two scales — and because "how full is this
// thing?" is the question the sidebar meters already answer, so it reads the same way here.

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

// What the fill and the dashed line mean, and — when the lane cannot measure — why there is no
// fill at all. The reason comes from the live reading rather than the history, because a sample
// carries no reason: a null there is a gap, and only `/usage` knows whether the cause is a missing
// metrics-server, RBAC that was never reconciled, or a lane that simply reserves nothing.
function encodingNote(rows: { kind: string }[], metricsUnavailable?: string | null): string {
  const has = (kind: string) => rows.some((r) => r.kind === kind)
  if (has('measured') && has('reserved')) return 'filled = measured, dashed = reserved'
  if (has('reserved') && metricsUnavailable) return `reserved only — ${metricsUnavailable}`
  if (has('reserved')) return 'reserved only'
  // The local lane: it measures and reserves nothing, so one fill is the whole truth.
  if (has('measured')) return 'filled = measured; this lane reserves nothing'
  return ''
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
  // Only for `metrics_unavailable`, and only to explain a missing fill. `['usage']` and a
  // 15s interval on purpose: that is the key and cadence the sidebar meter and the Monitor
  // cards already poll (Sidebar.tsx `ConnectionStatus`), so this shares their one poll
  // instead of adding a second — a different key or interval would silently split it.
  const usage = useQuery({
    queryKey: ['usage'],
    queryFn: () => robovast.resourceUsage(),
    refetchInterval: 15000,
    retry: false,
  })
  const rows = usageRows(history.data?.samples ?? [])

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
            <VegaLiteChart spec={USAGE_SPEC} rows={rows} height={200} />
          </Box>
          <Typography variant="caption" color="text.secondary">
            {[encodingNote(rows, usage.data?.metrics_unavailable), coverage(history.data, window)]
              .filter(Boolean)
              .join(' · ')}
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
