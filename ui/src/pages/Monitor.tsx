import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import Typography from '@mui/material/Typography'
import { robovast, campaignsNewestFirst, type CampaignSummary, type Status } from '@/lib/robovastClient'
import { StatusView } from '@/components/StatusView'
import { PhaseChip } from '@/components/PhaseChip'

const TERMINAL = ['finished', 'failed', 'stopped', 'error']
const isTerminal = (phase: string | undefined) => !!phase && TERMINAL.includes(phase)

// One campaign row: fetches its own live Status and polls until the campaign reaches a terminal phase.
function CampaignCard({ summary }: { summary: CampaignSummary }) {
  const qc = useQueryClient()
  const id = summary.campaign_id

  const status = useQuery({
    queryKey: ['status', id],
    queryFn: () => robovast.getStatus(id),
    // Poll while running; stop once the fetched status is terminal.
    refetchInterval: (q) => (isTerminal((q.state.data as Status | undefined)?.phase) ? false : 1500),
  })

  const stop = useMutation({
    mutationFn: () => robovast.stop(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
  })

  const phase = status.data?.phase ?? summary.phase
  const running = !isTerminal(phase)

  return (
    <Paper sx={{ p: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" mb={1.5}>
        <Typography variant="subtitle2" sx={{ fontFamily: 'monospace' }}>
          {id}
        </Typography>
        <Box flexGrow={1} />
        {status.isFetching ? <CircularProgress size={14} /> : null}
        {running ? (
          <Button
            size="small"
            variant="outlined"
            color="error"
            startIcon={<StopRoundedIcon />}
            disabled={stop.isPending}
            onClick={() => stop.mutate()}
          >
            Stop
          </Button>
        ) : null}
      </Stack>

      {stop.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Stop failed: {(stop.error as Error).message}
        </Alert>
      ) : null}

      {status.isError ? (
        <Stack direction="row" spacing={1} alignItems="center">
          <PhaseChip phase={phase} />
          <Typography variant="caption" color="text.secondary">
            no live status ({(status.error as Error).message})
          </Typography>
        </Stack>
      ) : status.data ? (
        <StatusView status={status.data} />
      ) : (
        <Stack direction="row" spacing={1} alignItems="center">
          <PhaseChip phase={phase} />
          <Typography variant="caption" color="text.secondary">
            {summary.num_passed}/{summary.num_runs} passed
            {summary.num_failed ? ` · ${summary.num_failed} failed` : ''}
          </Typography>
        </Stack>
      )}
    </Paper>
  )
}

export function Monitor() {
  const campaigns = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => robovast.listCampaigns(100, 0),
    refetchInterval: 5000,
  })

  return (
    <Stack spacing={2}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="h6">Campaigns</Typography>
        <Box flexGrow={1} />
        <Button
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={() => campaigns.refetch()}
          disabled={campaigns.isFetching}
        >
          Refresh
        </Button>
      </Stack>

      {campaigns.isError ? (
        <Alert severity="error">
          Could not reach the service: {(campaigns.error as Error).message}
        </Alert>
      ) : campaigns.isLoading ? (
        <CircularProgress size={24} />
      ) : !campaigns.data?.campaigns.length ? (
        <Alert severity="info" variant="outlined">
          No campaigns yet — start one from the Launcher.
        </Alert>
      ) : (
        campaignsNewestFirst(campaigns.data.campaigns).map((c) => (
          <CampaignCard key={c.campaign_id} summary={c} />
        ))
      )}
    </Stack>
  )
}
