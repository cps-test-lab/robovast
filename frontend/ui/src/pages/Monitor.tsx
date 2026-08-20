import { useEffect, useState } from 'react'
import { lazyView } from '@/lib/lazyView'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Collapse from '@mui/material/Collapse'
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
// Postprocessing recomputes metrics from the preserved rosbags, so it gets the
// derive-statistics-from-data icon; the replay arrow goes to the entry that actually runs
// the campaign again.
import QueryStatsRoundedIcon from '@mui/icons-material/QueryStatsRounded'
import DeleteOutlineRoundedIcon from '@mui/icons-material/DeleteOutlineRounded'
import KeyboardArrowDownRoundedIcon from '@mui/icons-material/KeyboardArrowDownRounded'
import KeyboardArrowUpRoundedIcon from '@mui/icons-material/KeyboardArrowUpRounded'
import CloudUploadRoundedIcon from '@mui/icons-material/CloudUploadRounded'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import {
  robovast,
  hasRecordedRuns,
  hasResults,
  isTerminalPhase,
  type CampaignSummary,
  type JobSummary,
  type ListCampaignsResponse,
  type Status,
} from '@/lib/robovastClient'
import { ConfigIcon, ExplorerIcon, RunViewIcon } from '@/components/viewIcons'
import { openCampaignConfig, openResultsView } from '@/lib/nav'
import { formatLocalTime } from '@/lib/time'
import { formatDuration } from '@/lib/format'
import { useLiveStream } from '@/lib/liveStream'
import { ErrorText, StatusView } from '@/components/StatusView'
import { CampaignOrigin } from '@/components/CampaignOrigin'
import { LaunchedBy } from '@/components/LaunchedBy'
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

// A post-run step that failed (postprocessing, upload-to-share). The headline names the step and
// what to do about it, and is always visible — that is what the phase indicator's warning refers
// to. The backend's text below it opens by itself only on the newest campaign: on a long list of
// finished campaigns, every older traceback expanded turns the page into a wall of stack frames
// nobody asked for. Anyone who wants an old one opens it.
//
// What toggles it is deliberately not the whole bar. The headline and the chevron always do,
// either direction. The bar's empty space opens it while collapsed — a big target for the common
// direction — but does nothing once open, and the error text itself never toggles at all: that is
// the part people drag across to copy a path out of, and a collapse mid-selection would take the
// text away as they read it.
function StepFailure({
  headline,
  error,
  defaultOpen,
}: {
  headline: string
  error: string
  defaultOpen: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <Alert
      severity="warning"
      sx={{ mb: 1, cursor: open ? 'default' : 'pointer' }}
      onClick={() => !open && setOpen(true)}
      action={
        <IconButton
          color="inherit"
          size="small"
          aria-label={open ? 'Hide error details' : 'Show error details'}
          aria-expanded={open}
          // The bar's own handler ignores clicks while open, so this only guards the
          // collapsed case, where both handlers would otherwise fire for one click.
          onClick={(e) => {
            e.stopPropagation()
            setOpen((o) => !o)
          }}
        >
          {open ? (
            <KeyboardArrowUpRoundedIcon fontSize="small" />
          ) : (
            <KeyboardArrowDownRoundedIcon fontSize="small" />
          )}
        </IconButton>
      }
    >
      {/* Not selectable: it is a click target, and a double-click meant as a toggle would
          otherwise leave a word highlighted. The error text below stays selectable. */}
      <Box
        component="span"
        onClick={(e) => {
          e.stopPropagation()
          setOpen((o) => !o)
        }}
        sx={{ display: 'block', cursor: 'pointer', userSelect: 'none' }}
      >
        {headline}
      </Box>
      <Collapse in={open} unmountOnExit>
        <ErrorText>{error}</ErrorText>
      </Collapse>
    </Alert>
  )
}

// One campaign row: fetches its own live Status and polls until the campaign reaches a terminal
// phase. `newest` is the top card in the (newest-first) list — the campaign the user is here to
// watch. It is the only one whose post-run failures open by themselves; see StepFailure.
function CampaignCard({ summary, newest }: { summary: CampaignSummary; newest: boolean }) {
  const qc = useQueryClient()
  const id = summary.campaign_id

  const status = useQuery({
    queryKey: ['status', id],
    queryFn: () => robovast.getStatus(id),
    // Poll while running; stop once the fetched status is terminal.
    refetchInterval: (q) => (isTerminalPhase((q.state.data as Status | undefined)?.phase) ? false : 1500),
    // The poll above is suspended while the tab is hidden — deliberately, so a backgrounded
    // monitor does not hammer the service — which is exactly why coming back has to read
    // once itself. Without this the card shows a phase from before the tab was switched
    // away, for however long the timer takes to restart. The app-wide default is off (see
    // main.tsx); a live campaign's phase is the case that earns the exception.
    refetchOnWindowFocus: true,
  })

  // Live per-job listing (running count + the clickable jobs list). Polled while the
  // campaign runs, and re-read on return for the same reason as the status above.
  const terminal = isTerminalPhase((status.data as Status | undefined)?.phase)
  const jobs = useQuery({
    queryKey: ['jobs', id],
    queryFn: () => robovast.listJobs(id),
    refetchInterval: () => (terminal ? false : 2000),
    refetchOnWindowFocus: true,
  })

  // Stopping the poll on its own leaves the last in-flight listing on screen forever —
  // jobs that were still `running` up to one poll before the campaign ended keep their
  // rows, so the live view never empties out. Read once more after the phase turns
  // terminal to pick up their final state.
  useEffect(() => {
    if (terminal) qc.invalidateQueries({ queryKey: ['jobs', id] })
  }, [terminal, id, qc])

  const stop = useMutation({
    mutationFn: () => robovast.stop(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
  })

  // One job, killed by hand; the campaign keeps running. Invalidates the jobs query (the row's
  // status changes) and the status one (its `runs.killed` counter moves) — which is also why
  // this lives here rather than in StatusView: those queries are owned by this card.
  const stopJob = useMutation({
    mutationFn: ({ jobName, reason }: { jobName: string; reason?: string }) =>
      robovast.stopJob(id, jobName, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs', id] })
      qc.invalidateQueries({ queryKey: ['status', id] })
    },
  })

  const { confirm, prompt } = useDialogs()

  // One dialog, not a confirm followed by a prompt: submitting *is* the confirmation, and the
  // reason field is the point of asking at all. Cancel (null) means don't stop — an empty
  // string is a deliberate "no reason given" and still goes through.
  const onStopJob = async (job: JobSummary) => {
    const reason = await prompt({
      title: `Stop job ${job.display_name || job.job_name}?`,
      message:
        'The rest of the campaign keeps running. This run is permanently recorded as ' +
        'killed — it will not count as a pass or a failure, and it cannot be resumed.',
      label: 'Reason (optional)',
      placeholder: 'e.g. stuck in nav recovery, will never finish',
      confirmLabel: 'Stop job',
    })
    if (reason === null) return
    stopJob.mutate({ jobName: job.job_name, reason: reason.trim() || undefined })
  }

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

  // Unlike the two entries below it, this one produces a DIFFERENT campaign — so it
  // invalidates the listing (where the new card appears) and nothing about this one.
  const retrigger = useMutation({
    mutationFn: () => robovast.retriggerCampaign(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['campaigns'] }),
  })

  const onRetrigger = () => {
    closeMenu()
    retrigger.mutate()
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
  // A campaign freezes its project into `_config/` only once variation has expanded, so during a
  // pre-run phase there is provably nothing to open and the shortcut is hidden rather than offered
  // and answered with a 404. From then on it stays, running or finished: the configuration a
  // campaign is running is worth reading while it runs.
  const hasConfig = !PRE_RUN_PHASES.has(phase)
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
  // …and that failure must reach the phase indicator, which otherwise paints such a campaign
  // green: `finished` describes the runs, not the results. Suppressed while running, where the
  // error belongs to the attempt currently being retried.
  const failedSteps = running
    ? []
    : [postprocError ? 'postprocessing' : '', shareError ? 'upload to share' : ''].filter(Boolean)
  const stepIssue = failedSteps.length ? `${failedSteps.join(' + ')} failed` : null

  // The postprocessed archive is streamed from the object store — only a cluster
  // service serves it (a local service's results are already on its filesystem).
  const version = useQuery({ queryKey: ['version'], queryFn: () => robovast.version() })
  const canDownload = !running && version.data?.backend === 'kubernetes'

  // Lane capacity, for the Details panel's "jobs in flight" estimate. Same query key as the
  // sidebar's connection meter, so every card on the page and the sidebar share one poll
  // rather than each issuing its own.
  const usage = useQuery({
    queryKey: ['usage'],
    queryFn: () => robovast.resourceUsage(),
    refetchInterval: 15000,
    retry: false,
  })

  // Shortcuts into the Results views, offered only where they lead somewhere. Both read the
  // *summary* — the very object the Results topic filters on — rather than the live status, so a
  // button can never open a view that would greet the reader with an empty state. `hasResults`
  // implies a terminal phase, so neither shows while the campaign runs; the summary arrives over
  // the same stream as everything else here, so they appear by themselves once postprocessing ends.
  const canExplore = hasResults(summary)
  const canReplay = canExplore && hasRecordedRuns(summary)

  return (
    <Paper sx={{ p: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" mb={1.5}>
        <PhaseDot phase={phase} issue={stepIssue} />
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
        <CampaignOrigin origin={summary.origin}>
          <Typography variant="subtitle2" sx={{ fontFamily: 'monospace' }}>
            {id}
          </Typography>
        </CampaignOrigin>
        <LaunchedBy name={summary.created_by} />
        {summary.started_at ? (
          <Typography variant="caption" color="text.secondary">
            {formatLocalTime(summary.started_at)}
          </Typography>
        ) : null}
        <Box flexGrow={1} />
        {status.isFetching ? <CircularProgress size={14} /> : null}
        {hasConfig ? (
          <Tooltip title="Open this campaign's configuration (read-only)">
            <IconButton
              size="small"
              aria-label="open campaign config"
              onClick={() => openCampaignConfig(id)}
            >
              <ConfigIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null}
        {canExplore ? (
          <Tooltip title="Open this campaign in the results Explorer">
            <IconButton
              size="small"
              aria-label="open results explorer"
              onClick={() => openResultsView('explorer', id)}
            >
              <ExplorerIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null}
        {canReplay ? (
          <Tooltip title="Replay this campaign's runs in the Run view">
            <IconButton
              size="small"
              aria-label="open run view"
              onClick={() => openResultsView('run', id)}
            >
              <RunViewIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null}
        {!running ? (
          <>
            {/* Empty title while the delete is in flight: the button is disabled then, and a
                disabled button fires no events for the tooltip to listen to (MUI warns about it). */}
            <Tooltip title={del.isPending ? '' : 'Campaign actions'}>
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
            </Tooltip>
            <Menu anchorEl={menuAnchor} open={!!menuAnchor} onClose={closeMenu}>
              {/* First, and the only entry here that starts a separate campaign rather than
                  re-running a step of this one. */}
              <MenuItem onClick={onRetrigger} disabled={retrigger.isPending}>
                <ListItemIcon>
                  <ReplayRoundedIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>Retrigger campaign</ListItemText>
              </MenuItem>
              <MenuItem onClick={onReprocess}>
                <ListItemIcon>
                  <QueryStatsRoundedIcon fontSize="small" />
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

      {/* The headline stays one line and the backend's own text goes below it in ErrorText — a
          traceback spliced into the middle of a sentence buries the advice that follows it. */}
      {stop.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Stop failed.
          <ErrorText>{(stop.error as Error).message}</ErrorText>
        </Alert>
      ) : stop.data && !stop.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          <ErrorText>{stop.data.message ?? 'Stop had no effect.'}</ErrorText>
        </Alert>
      ) : null}

      {/* A refusal is the expected outcome when the job finished between the poll that drew the
          button and the click — so the server's own message (which names the phase it is in) is
          the whole explanation, and it is a warning rather than an error. */}
      {stopJob.isError ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          Could not stop that job.
          <ErrorText>{(stopJob.error as Error).message}</ErrorText>
        </Alert>
      ) : stopJob.data && !stopJob.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          <ErrorText>{stopJob.data.message ?? 'Stopping the job had no effect.'}</ErrorText>
        </Alert>
      ) : null}

      {del.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Delete failed.
          <ErrorText>{(del.error as Error).message}</ErrorText>
        </Alert>
      ) : null}

      {/* The new campaign's card appears at the top of this (newest-first) list, which is not
          the same as knowing WHICH id is yours — so the id is named here. Its own description
          also reads "retrigger of <this id>". */}
      {retrigger.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Retrigger failed — this campaign was not modified.
          <ErrorText>{(retrigger.error as Error).message}</ErrorText>
        </Alert>
      ) : retrigger.data ? (
        <Alert severity="success" sx={{ mb: 1 }}>
          Retriggered as <code>{retrigger.data.campaign_id}</code>.
          {retrigger.data.note ? <ErrorText>{retrigger.data.note}</ErrorText> : null}
        </Alert>
      ) : null}

      {share.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Upload-to-share failed.
          <ErrorText>{(share.error as Error).message}</ErrorText>
        </Alert>
      ) : share.data && !share.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          <ErrorText>{share.data.message ?? 'Upload-to-share had no effect.'}</ErrorText>
        </Alert>
      ) : share.data?.ok ? (
        <Alert severity="success" sx={{ mb: 1 }}>
          <ErrorText>{share.data.message ?? 'Upload-to-share complete.'}</ErrorText>
        </Alert>
      ) : null}

      {!running && postprocError ? (
        <StepFailure
          headline={
            'Postprocessing failed — the runs finished; retrigger postprocessing from the ' +
            'actions menu. The full output is in the campaign log below.'
          }
          error={postprocError}
          defaultOpen={newest}
        />
      ) : null}

      {!running && shareError ? (
        <StepFailure
          headline="Upload-to-share failed — retrigger it from the actions menu."
          error={shareError}
          defaultOpen={newest}
        />
      ) : null}

      {status.isError ? (
        <Stack direction="row" spacing={1} alignItems="flex-start">
          <PhaseChip phase={phase} issue={stepIssue} />
          <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'pre-wrap' }}>
            no live status ({(status.error as Error).message})
          </Typography>
        </Stack>
      ) : status.data ? (
        <StatusView
          status={status.data}
          campaignId={id}
          jobs={jobs.data}
          startedAt={summary.started_at}
          liveOnly
          newest={newest}
          showDetails={canExplore}
          quotaCpu={usage.data?.cpu_capacity ?? null}
          postprocessed={!!summary.postprocessed}
          onStopJob={onStopJob}
          stoppingJob={stopJob.isPending ? (stopJob.variables?.jobName ?? null) : null}
        />
      ) : (
        <Stack direction="row" spacing={1} alignItems="center">
          <PhaseChip phase={phase} issue={stepIssue} />
          <Typography variant="caption" color="text.secondary">
            {summary.num_passed}/{summary.num_runs} passed
            {summary.num_failed ? ` · ${summary.num_failed} failed` : ''}
            {/* Search draws that never became a runnable configuration. Shown apart from
                the run tallies: they are absent from num_runs, so without this a campaign
                that could not compose most of what it proposed reads as a smaller one. */}
            {summary.num_composition_failed
              ? ` · ${summary.num_composition_failed} skipped`
              : ''}
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
// source for the list — no polling. useLiveStream owns the recovery: a dropped
// connection, a stream the browser gave up on, and a socket that died silently
// while the tab was in the background all end in a fresh EventSource, which re-sends
// the whole list. `reconnect` is the same path on demand (the Refresh button).
function useCampaignStream() {
  const [data, setData] = useState<ListCampaignsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { state, reconnect } = useLiveStream(robovast.campaignsStreamUrl(), {
    onMessage: (e) => {
      setData(JSON.parse(e.data) as ListCampaignsResponse)
      setError(null)
    },
    events: {
      streamerror: (e) => setError(JSON.parse(e.data)),
    },
  })

  // Anything but `open` means the list on screen may already be behind; keep showing it
  // (it is still the best we have) and say so.
  return { data, error, live: state === 'open', reconnect }
}

export function Monitor() {
  const { data, error, live, reconnect } = useCampaignStream()

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
        <Button size="small" startIcon={<RefreshRoundedIcon />} onClick={reconnect}>
          Refresh
        </Button>
      </Stack>

      {error ? (
        <Alert severity="error">
          Could not reach the service.
          <ErrorText>{error}</ErrorText>
        </Alert>
      ) : !data ? (
        <CircularProgress size={24} />
      ) : !data.campaigns.length ? (
        <Alert severity="info" variant="outlined">
          No campaigns yet — start one from the Launcher.
        </Alert>
      ) : (
        data.campaigns.map((c, i) => (
          <CampaignCard key={c.campaign_id} summary={c} newest={i === 0} />
        ))
      )}
    </Stack>
  )
}
