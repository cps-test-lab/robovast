// Results → Run view: a run-focused, time-driven dashboard. Pick one run of a postprocessed campaign
// and replay it through the panels its .vast declares (visualization.panels) over the rosbag timeline.
// This component is the glue: it resolves the run, builds the shared PlaybackClock and DataProvider for
// it, discovers the timeline range, and hands the parsed panel specs to the PanelHost. The panels
// themselves (playback bar, costmaps, scenario tree) are independent plugins.

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import {
  robovast,
  campaignsNewestFirst,
  type CampaignSummary,
} from '@/lib/robovastClient'
import { PlaybackClock } from '@/lib/dashboard/clock'
import { dbDataProvider } from '@/lib/dashboard/dataProvider'
import { parseVastPanels } from '@/lib/dashboard/parseVastPanels'
import { PanelHost } from '@/lib/dashboard/PanelHost'
import '@/panels' // registers the built-in panels

// Tables whose timestamp column can define the run's timeline; the union of their ranges is used.
const TIME_TABLES = ['poses', 'behaviors', 'scenario_timestamps']

interface RunKey {
  config_name: string
  run_id: string
}

export function RunView({
  campaignId,
  campaigns,
  onCampaignChange,
}: {
  campaignId: string
  campaigns: CampaignSummary[]
  onCampaignChange: (campaignId: string) => void
}) {
  const sortedCampaigns = campaignsNewestFirst(campaigns.filter((c) => c.postprocessed))

  const panels = useQuery({
    queryKey: ['panels', campaignId],
    queryFn: () => robovast.listCampaignPanels(campaignId),
    enabled: !!campaignId,
    retry: false,
  })

  const runs = useQuery({
    queryKey: ['runs-list', campaignId],
    queryFn: () =>
      robovast.queryCampaignDataSql(
        campaignId,
        'SELECT config_name, run_id, status, passed FROM runs ORDER BY config_name, run_id',
      ),
    enabled: !!campaignId,
    retry: false,
  })

  const runList: RunKey[] = useMemo(
    () =>
      (runs.data?.rows ?? []).map((r) => ({
        config_name: String(r.config_name ?? ''),
        run_id: String(r.run_id ?? ''),
      })),
    [runs.data],
  )

  const [run, setRun] = useState<RunKey | null>(null)
  // Default to (and self-heal onto) the first run of the current campaign.
  useEffect(() => {
    if (!runList.length) {
      setRun(null)
      return
    }
    if (!runList.some((r) => r.config_name === run?.config_name && r.run_id === run?.run_id)) {
      setRun(runList[0])
    }
  }, [runList]) // eslint-disable-line react-hooks/exhaustive-deps

  const runKey = run ? `${campaignId}:${run.config_name}:${run.run_id}` : ''

  // One provider + clock per run. Recreated (and the old clock disposed) when the run changes.
  const provider = useMemo(
    () => (run ? dbDataProvider(campaignId, run.config_name, run.run_id) : null),
    [campaignId, run],
  )
  const clock = useMemo(() => new PlaybackClock(), [runKey])
  useEffect(() => () => clock.dispose(), [clock])

  // Discover the timeline range from whichever time tables exist, and set it on the clock.
  useEffect(() => {
    if (!provider) return
    let alive = true
    Promise.all(
      TIME_TABLES.map((t) => provider.timeRange(t).catch(() => null)),
    ).then((ranges) => {
      if (!alive) return
      const valid = ranges.filter((r): r is [number, number] => !!r)
      if (!valid.length) return
      const lo = Math.min(...valid.map((r) => r[0]))
      const hi = Math.max(...valid.map((r) => r[1]))
      clock.setRange(lo, hi)
    })
    return () => {
      alive = false
    }
  }, [provider, clock])

  const specs = useMemo(
    () => (panels.data ? parseVastPanels(panels.data.panels) : []),
    [panels.data],
  )

  const noData = /data\.db/i.test((runs.error as Error | null)?.message ?? '')

  return (
    <Stack spacing={2} sx={{ height: 'calc(100vh - 72px)' }}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Run view</Typography>
        <TextField
          select={!!sortedCampaigns.length}
          size="small"
          label="Campaign"
          value={campaignId}
          onChange={(e) => onCampaignChange(e.target.value)}
          sx={{ minWidth: 320 }}
        >
          {sortedCampaigns.map((c) => (
            <MenuItem key={c.campaign_id} value={c.campaign_id}>
              {c.campaign_id}
            </MenuItem>
          ))}
        </TextField>
        {runList.length ? (
          <TextField
            select
            size="small"
            label="Run"
            value={run ? `${run.config_name}/${run.run_id}` : ''}
            onChange={(e) => {
              const [config_name, run_id] = e.target.value.split('/')
              setRun({ config_name, run_id })
            }}
            sx={{ minWidth: 240 }}
          >
            {runList.map((r) => (
              <MenuItem key={`${r.config_name}/${r.run_id}`} value={`${r.config_name}/${r.run_id}`}>
                {r.config_name} · run {r.run_id}
              </MenuItem>
            ))}
          </TextField>
        ) : null}
      </Stack>

      {!campaignId ? (
        <Alert severity="info" variant="outlined">
          Pick a postprocessed campaign to replay a run.
        </Alert>
      ) : panels.isPending || runs.isPending ? (
        <CircularProgress size={24} />
      ) : noData ? (
        <Alert severity="info" variant="outlined">
          This campaign has no queryable data yet — run analysis postprocessing (Data browser) first.
        </Alert>
      ) : !specs.length ? (
        <Alert severity="info" variant="outlined">
          This campaign's <code>.vast</code> declares no <code>visualization.panels</code>. Add a{' '}
          <code>visualization:</code> block to define the run view.
        </Alert>
      ) : !provider ? (
        <Alert severity="info" variant="outlined">
          This campaign has no runs to replay.
        </Alert>
      ) : (
        <Box sx={{ flexGrow: 1, minHeight: 0, border: 1, borderColor: 'divider', borderRadius: 1 }}>
          <PanelHost key={runKey} panels={specs} clock={clock} data={provider} />
        </Box>
      )}
    </Stack>
  )
}
