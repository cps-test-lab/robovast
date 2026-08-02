import { useEffect, useState } from 'react'
import { lazyView } from '@/lib/lazyView'
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
import CloudUploadRoundedIcon from '@mui/icons-material/CloudUploadRounded'
import Typography from '@mui/material/Typography'
import {
  robovast,
  isTerminalPhase,
  type CampaignSummary,
  type ListCampaignsResponse,
  type Status,
} from '@/lib/robovastClient'
import { formatLocalTime } from '@/lib/time'
import { formatDuration } from '@/lib/format'
import { StatusView } from '@/components/StatusView'
import { PhaseChip, PhaseDot } from '@/components/PhaseChip'
import { useDialogs } from '@/components/DialogProvider'
import { LaunchBar } from './LaunchBar'
// Deferred, not statically imported: this dialog embeds a Monaco editor, and Monaco is
// ~3.9 MB. Monitor is the view the app opens on, so importing it here put the whole editor
// on the critical path of the campaign list — for a dialog reached by a menu item most
// sessions never click. Mounted only once opened, so the chunk is fetched on first use.
const PostprocessingDialog = lazyView('Postprocessing settings',
  () => import('./PostprocessingDialog').then((m) => ({ default: m.PostprocessingDialog })))

// Phases before the run loop starts. They have no progress bar of their own, so the only
// signal that one is wedged rather than slow is how long it has been held.
const PRE_RUN_PHASES: ReadonlySet<string> = new Set([
  'initializing', 'building', 'starting', 'plugin install', 'variation',
])

// One campaign row: fetches its own live Status and polls until the campaign reaches a terminal phase.
function CampaignCard({ summary }: { summary: CampaignSummary }) {
  const qc = useQueryClient()
  const id = summary.campaign_id

  const status = useQuery({
    queryKey: ['status', id],
    queryFn: () => robovast.getStatus(id),
    // Poll while running; stop once the fetched status is terminal.
    refetchInterval: (q) => (isTerminalPhase((q.state.data as Status | undefined)?.phase) ? false : 1500),
  })

  // Live per-job listing (running count + the clickable jobs list). Polled while the
  // campaign runs; one final read once it is terminal so the completed jobs still show.
  const jobs = useQuery({
    queryKey: ['jobs', id],
    queryFn: () => robovast.listJobs(id),
    refetchInterval: () =>
      isTerminalPhase((status.data as Status | undefined)?.phase) ? false : 2000,
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
  const [ppOpen, setPpOpen] = useState(false)

  const del = useMutation({
    mutationFn: () => robovast.deleteCampaign(id),
    // The row (and every cached query for this campaign) is gone on success.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  })

  const onReprocess = () => {
    closeMenu()
    setPpOpen(true)
  }

  const share = useMutation({
    mutationFn: () => robovast.runShare(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
  })

  const onShare = () => {
    closeMenu()
    share.mutate()
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
  const running = !isTerminalPhase(phase)
  // How long the current phase has been held, shown only while a *pre-run* phase is in
  // effect. Those are the phases with no progress bar to watch, so a stalled project
  // push or image build otherwise looks exactly like a slow one — indefinitely.
  const phaseSince = status.data?.phase_since
  const phaseAge =
    phaseSince && PRE_RUN_PHASES.has(phase)
      ? formatDuration(Math.max(0, Date.now() / 1000 - phaseSince))
      : null
  // Once running, the *phase* age is noise but the **progress** age is not: the run
  // counter was assumed to carry that signal and does not — a wedged run sat at
  // `progress: 0` indefinitely and looked identical to a slow one. `stalled` is only
  // asserted against the declared per-run budget; with none, the age is shown alone
  // rather than a threshold being invented here.
  const progressSince = status.data?.progress_since
  const progressDeadline = status.data?.progress_deadline_s
  const progressAgeS =
    progressSince && running && !PRE_RUN_PHASES.has(phase)
      ? Math.max(0, Date.now() / 1000 - progressSince)
      : null
  const progressAge = progressAgeS === null ? null : formatDuration(progressAgeS)
  // Tri-state, matching the status contract: true / false / null ("no declared
  // execution.timeout, so no verdict"). Rendering null as "not stalled" would put a
  // reassuring grey label on a run that may already be dead.
  const stalled =
    progressAgeS === null || !progressDeadline ? null : progressAgeS > progressDeadline
  // A finished campaign can still carry a post-run step failure (postprocessing / share);
  // prefer the live status, fall back to the list summary. Re-triggerable via the menu.
  const postprocError = status.data?.postprocessing_error ?? summary.postprocessing_error
  const shareError = status.data?.share_error ?? summary.share_error

  // The postprocessed archive is streamed from the object store — only a cluster
  // service serves it (a local service's results are already on its filesystem).
  const version = useQuery({ queryKey: ['version'], queryFn: () => robovast.version() })
  const canDownload = !running && version.data?.backend === 'kubernetes'

  return (
    <Paper sx={{ p: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" mb={1.5}>
        <PhaseDot phase={phase} />
        {phaseAge ? (
          <Typography variant="caption" color="text.secondary">
            {phaseAge}
          </Typography>
        ) : null}
        {progressAge ? (
          <Typography
            variant="caption"
            color={stalled ? 'error.main' : 'text.secondary'}
            title={
              stalled
                ? `No run has completed for ${progressAge}, past the ${progressDeadline}s expected ` +
                  `per run — the run is not merely slow. Read what it is repeating in the log ` +
                  `panel below.`
                : stalled === null
                  ? `Time since a run last completed. This .vast declares no ` +
                    `execution.timeout, so there is no budget to judge it against — ` +
                    `compare it yourself against how long one run should take.`
                  : `Time since a run last completed (within the declared per-run budget).`
            }
          >
            {stalled ? `stalled ${progressAge}` : progressAge}
          </Typography>
        ) : null}
        <Typography variant="subtitle2" sx={{ fontFamily: 'monospace' }}>
          {id}
        </Typography>
        {summary.started_at ? (
          <Typography variant="caption" color="text.secondary">
            {formatLocalTime(summary.started_at)}
          </Typography>
        ) : null}
        <Box flexGrow={1} />
        {status.isFetching ? <CircularProgress size={14} /> : null}
        {!running ? (
          <>
            <IconButton
              size="small"
              aria-label="campaign actions"
              onClick={(e) => setMenuAnchor(e.currentTarget)}
              disabled={del.isPending}
            >
              {del.isPending ? (
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
              <MenuItem onClick={onShare} disabled={share.isPending}>
                <ListItemIcon>
                  <CloudUploadRoundedIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>Retrigger upload-to-share</ListItemText>
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

      {summary.description ? (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {summary.description}
        </Typography>
      ) : null}

      {stop.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Stop failed: {(stop.error as Error).message}
        </Alert>
      ) : stop.data && !stop.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {stop.data.message ?? 'Stop had no effect.'}
        </Alert>
      ) : null}

      {del.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Delete failed: {(del.error as Error).message}
        </Alert>
      ) : null}

      {share.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Upload-to-share failed: {(share.error as Error).message}
        </Alert>
      ) : share.data && !share.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {share.data.message ?? 'Upload-to-share had no effect.'}
        </Alert>
      ) : share.data?.ok ? (
        <Alert severity="success" sx={{ mb: 1 }}>
          {share.data.message ?? 'Upload-to-share complete.'}
        </Alert>
      ) : null}

      {!running && postprocError ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          Postprocessing failed: {postprocError} — the runs finished; retrigger
          postprocessing from the actions menu.
        </Alert>
      ) : null}

      {!running && shareError ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          Upload-to-share failed: {shareError} — retrigger it from the actions menu.
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

      {ppOpen && (
        <PostprocessingDialog campaignId={id} open onClose={() => setPpOpen(false)} />
      )}
    </Paper>
  )
}

// Live campaign list over SSE. The server pushes the full list on connect and on
// every change (a server-side loop over list_campaigns), so this is the single
// source for the list — no polling. EventSource reconnects on its own after a
// dropped connection; `reconnect` forces a fresh connection (the Refresh button).
function useCampaignStream(reconnect: number) {
  const [data, setData] = useState<ListCampaignsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState(false)

  useEffect(() => {
    const es = new EventSource(robovast.campaignsStreamUrl())
    es.onopen = () => setLive(true)
    es.onmessage = (e) => {
      setData(JSON.parse(e.data) as ListCampaignsResponse)
      setError(null)
      setLive(true)
    }
    es.addEventListener('streamerror', (e) => {
      setError(JSON.parse((e as MessageEvent).data))
    })
    es.onerror = () => {
      // Transport-level drop: EventSource retries on its own; reflect the gap but
      // keep showing the last list until the next frame lands.
      if (es.readyState !== EventSource.CLOSED) setLive(false)
    }
    return () => es.close()
  }, [reconnect])

  return { data, error, live }
}

export function Monitor() {
  const [reconnect, setReconnect] = useState(0)
  const { data, error, live } = useCampaignStream(reconnect)

  return (
    <Stack spacing={2}>
      <LaunchBar />

      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="h6">Campaigns</Typography>
        {data && !live ? (
          <Typography variant="caption" color="text.secondary">
            reconnecting…
          </Typography>
        ) : null}
        <Box flexGrow={1} />
        <Button
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={() => setReconnect((n) => n + 1)}
        >
          Refresh
        </Button>
      </Stack>

      {error ? (
        <Alert severity="error">Could not reach the service: {error}</Alert>
      ) : !data ? (
        <CircularProgress size={24} />
      ) : !data.campaigns.length ? (
        <Alert severity="info" variant="outlined">
          No campaigns yet — start one from the Launcher.
        </Alert>
      ) : (
        data.campaigns.map((c) => (
          <CampaignCard key={c.campaign_id} summary={c} />
        ))
      )}
    </Stack>
  )
}
