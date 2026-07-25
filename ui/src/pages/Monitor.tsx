import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import ListItemIcon from '@mui/material/ListItemIcon'
import ListItemText from '@mui/material/ListItemText'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import DownloadRoundedIcon from '@mui/icons-material/DownloadRounded'
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded'
import ReplayRoundedIcon from '@mui/icons-material/ReplayRounded'
import DeleteOutlineRoundedIcon from '@mui/icons-material/DeleteOutlineRounded'
import Typography from '@mui/material/Typography'
import { robovast, campaignsNewestFirst, type CampaignSummary, type Status } from '@/lib/robovastClient'
import { StatusView } from '@/components/StatusView'
import { PhaseChip, PhaseDot } from '@/components/PhaseChip'
import { useDialogs } from '@/components/DialogProvider'
import { LaunchBar } from './LaunchBar'

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

  // Live per-job listing (running count + the clickable jobs list). Polled while the
  // campaign runs; one final read once it is terminal so the completed jobs still show.
  const jobs = useQuery({
    queryKey: ['jobs', id],
    queryFn: () => robovast.listJobs(id),
    refetchInterval: () =>
      isTerminal((status.data as Status | undefined)?.phase) ? false : 2000,
  })

  const stop = useMutation({
    mutationFn: () => robovast.stop(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
  })

  const { confirm } = useDialogs()
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  const closeMenu = () => setMenuAnchor(null)

  const reprocess = useMutation({
    mutationFn: () => robovast.runPostprocessing(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
  })

  const del = useMutation({
    mutationFn: () => robovast.deleteCampaign(id),
    // The row (and every cached query for this campaign) is gone on success.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  })

  const onReprocess = () => {
    closeMenu()
    reprocess.mutate()
  }

  const onDelete = async () => {
    closeMenu()
    const ok = await confirm({
      title: 'Delete campaign?',
      message: (
        <>
          Permanently delete <code>{id}</code> and all its data. This cannot be undone.
          Any copy on the external share is left untouched.
        </>
      ),
      confirmLabel: 'Delete',
      danger: true,
    })
    if (ok) del.mutate()
  }

  const phase = status.data?.phase ?? summary.phase
  const running = !isTerminal(phase)

  // The postprocessed archive is streamed from the object store — only a cluster
  // service serves it (a local service's results are already on its filesystem).
  const version = useQuery({ queryKey: ['version'], queryFn: () => robovast.version() })
  const canDownload = !running && version.data?.backend === 'kubernetes'

  return (
    <Paper sx={{ p: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" mb={1.5}>
        <PhaseDot phase={phase} />
        <Typography variant="subtitle2" sx={{ fontFamily: 'monospace' }}>
          {id}
        </Typography>
        <Box flexGrow={1} />
        {status.isFetching ? <CircularProgress size={14} /> : null}
        {!running ? (
          <>
            <IconButton
              size="small"
              aria-label="campaign actions"
              onClick={(e) => setMenuAnchor(e.currentTarget)}
              disabled={reprocess.isPending || del.isPending}
            >
              {reprocess.isPending || del.isPending ? (
                <CircularProgress size={16} />
              ) : (
                <SettingsRoundedIcon fontSize="small" />
              )}
            </IconButton>
            <Menu anchorEl={menuAnchor} open={!!menuAnchor} onClose={closeMenu}>
              <MenuItem onClick={onReprocess}>
                <ListItemIcon>
                  <ReplayRoundedIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>Retrigger postprocessing</ListItemText>
              </MenuItem>
              <MenuItem onClick={onDelete} sx={{ color: 'error.main' }}>
                <ListItemIcon>
                  <DeleteOutlineRoundedIcon fontSize="small" color="error" />
                </ListItemIcon>
                <ListItemText>Delete</ListItemText>
              </MenuItem>
            </Menu>
          </>
        ) : null}
        {canDownload ? (
          <Button
            size="small"
            variant="outlined"
            startIcon={<DownloadRoundedIcon />}
            component="a"
            href={robovast.archiveUrl(id)}
            download={`${id}.tar.gz`}
          >
            Download
          </Button>
        ) : null}
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
      ) : stop.data && !stop.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {stop.data.message ?? 'Stop had no effect.'}
        </Alert>
      ) : null}

      {reprocess.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Postprocessing failed: {(reprocess.error as Error).message}
        </Alert>
      ) : reprocess.data ? (
        <Alert severity={reprocess.data.ok ? 'success' : 'warning'} sx={{ mb: 1 }}>
          {reprocess.data.message ?? (reprocess.data.ok ? 'Postprocessing complete.' : 'No effect.')}
        </Alert>
      ) : null}

      {del.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Delete failed: {(del.error as Error).message}
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
        <StatusView status={status.data} jobs={jobs.data} startedAt={summary.started_at} liveOnly />
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
      <LaunchBar />

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
