import { useEffect, useMemo, useState } from 'react'
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
import LinkRoundedIcon from '@mui/icons-material/LinkRounded'
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
import CloudDownloadRoundedIcon from '@mui/icons-material/CloudDownloadRounded'
import UploadFileRoundedIcon from '@mui/icons-material/UploadFileRounded'
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
  type ShareArchive,
  type Status,
} from '@/lib/robovastClient'
import { ConfigIcon, ExplorerIcon, RunViewIcon } from '@/components/viewIcons'
import { ShareImportDialog } from './ShareImportDialog'
import { openCampaignConfig, openResultsView } from '@/lib/nav'
import { preferredArchive } from '@/lib/shareArchives'
import { formatLocalTime } from '@/lib/time'
import { formatDuration } from '@/lib/format'
import { useLiveStream } from '@/lib/liveStream'
import { ErrorText, StatusView } from '@/components/StatusView'
import { CampaignOrigin } from '@/components/CampaignOrigin'
import { LaunchedBy } from '@/components/LaunchedBy'
import { PhaseChip, PhaseDot } from '@/components/PhaseChip'
import { useDialogs } from '@/components/DialogProvider'
import { useCampaignImport } from './ImportCampaignButton'
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
// phase. `newest` is the top card — the campaign the user is here to watch. The service leads with
// the live campaigns and orders by recency within each group, so that is a running campaign when
// there is one and the newest finished one otherwise. It is the only card whose post-run failures
// open by themselves, and those render only when the campaign is at rest (see StepFailure), so a
// live campaign at the top simply means nothing auto-expands.
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
  // Whether this campaign was ALREADY over when the card first rendered — not whether it is
  // over now, which is a different question with a different answer.
  //
  // A campaign at rest has no live jobs to list, so the query carries an `enabled` gate: without
  // one every card issues the request anyway, and a page of a hundred finished campaigns fires a
  // hundred `listJobs` calls before the first status reply can turn polling off. On the
  // cluster lane each of those is a Kubernetes API call, and they all leave at once — the page
  // is served over HTTP/2, so nothing throttles the burst the way a connection limit would.
  // Nothing is lost by skipping them: this view is `liveOnly`, so it already hides the
  // completed jobs such a listing would return.
  //
  // Frozen at first render rather than tracking `summary.phase`, because a campaign that
  // finishes while being watched still needs the final read below — gating on the live phase
  // would disable the query at exactly the moment that read is due, leaving the last poll's
  // `running` rows on screen forever.
  const [bornAtRest] = useState(() => isTerminalPhase(summary.phase))
  const jobs = useQuery({
    queryKey: ['jobs', id],
    queryFn: () => robovast.listJobs(id),
    enabled: !bornAtRest,
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
  // Once running, the *phase* age is noise, and so is the bare progress age: a duration
  // next to the campaign id says nothing a reader can act on. The **verdict** is what
  // matters — this run is wedged, not merely slow — so the clock is kept only to assert
  // it. `stalled` is asserted against the declared per-run budget alone; a project that
  // declares none gets no verdict here and is told so once, by `validate_project`,
  // where the missing budget is still fixable.
  //
  // Only in `running`, matching `stall_report` in the status contract — the budget is
  // per-*run*, and `progress_since` can only advance while runs complete. Every other live
  // phase restarts that clock when it begins and then has nothing to move it, so measuring
  // one against the per-run budget says only that it outlasted a single run: postprocessing
  // a large campaign's rosbags always does, and this painted a red "stalled" on it for the
  // half-hour it spent working correctly. Excluding just the pre-run phases was the same
  // rule half-applied.
  const progressSince = status.data?.progress_since
  const progressDeadline = status.data?.progress_deadline_s
  const progressAgeS =
    progressSince && phase === 'running'
      ? Math.max(0, Date.now() / 1000 - progressSince)
      : null
  // Tri-state, matching the status contract: true / false / null ("no declared
  // execution.timeout, or no runs in this phase, so no verdict"). Only `true` renders —
  // rendering null as "not stalled" would put a reassuring label on a run that may already
  // be dead.
  const stalled =
    progressAgeS === null || !progressDeadline ? null : progressAgeS > progressDeadline
  // The live step marker, for the phases that have no progress bar of their own. Postprocessing
  // is the one that needed it: the run counters are frozen and `progress` is pinned, so a
  // campaign converting a large run's rosbags for half an hour looked exactly like a stuck one.
  // The backend publishes each step's own line here (`stage_output_callback`), and `set_phase`
  // clears it on every phase change, so a marker can never describe a phase already left.
  // Rendered for whatever phase set one rather than for postprocessing specifically — the field
  // is the general "what is this phase doing right now".
  const stage = running ? (status.data?.stage ?? '').trim() : ''
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

  // Every lane serves the archive now: the cluster streams it from the object store, and a
  // local service tars its own results directory (`campaign_tar_stream` is on the interface,
  // implemented by both). So the only thing gating the download is whether the campaign is
  // still being written to, not which backend it ran on.
  const canDownload = !running

  // One listing for the whole page: every card asks under the same react-query key, so
  // they collapse into a single request, and the share answers for itself rather than
  // anything being cached onto a campaign. A share that is unconfigured or unreachable
  // simply leaves every card with a plain icon button.
  const shareArchives = useQuery({
    queryKey: ['shareArchives'],
    queryFn: () => robovast.listShareArchives(),
    staleTime: 60_000,
    retry: false,
  })
  // Reduced with the same preference the import dialog applies, not `find`: a campaign the
  // share holds twice -- raw from the campaign-end upload, postprocessed from a later export
  // -- would otherwise give this card whichever the provider happened to list first, so the
  // link it copies and the archive that dialog imports could be two different files.
  const shareCopy =
    shareArchives.data?.archives
      .filter((a) => a.campaign_id === id)
      .reduce<ShareArchive | null>((best, a) => (best ? preferredArchive(best, a) : a), null)
    ?? null
  const [downloadAnchor, setDownloadAnchor] = useState<HTMLElement | null>(null)

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
        {stalled ? (
          <Typography
            variant="caption"
            color="error.main"
            title={
              `No run has completed for ${formatDuration(progressAgeS!)}, past the ` +
              `${progressDeadline}s expected per run — the run is not merely slow. Read ` +
              `what it is repeating in the log panel below.`
            }
          >
            stalled {formatDuration(progressAgeS!)}
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
        {stage ? (
          // After the spacer on purpose: the marker changes every few seconds, and ahead of it
          // every re-render would shift the campaign id and the start time sideways. Capped and
          // `noWrap` so a long step line truncates here instead of pushing the buttons off —
          // the full text is on hover, and the log panel below has the rest.
          <Typography
            variant="caption"
            color="text.secondary"
            noWrap
            title={stage}
            sx={{ minWidth: 0, maxWidth: '40%', fontFamily: 'monospace' }}
          >
            {stage}
          </Typography>
        ) : null}
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
              {/* Named, because which variant lands is not a choice here and never was:
                  `campaign_variant` reads it off the campaign directory, and once
                  postprocessing has written into that tree the raw campaign no longer exists
                  to export. Saying which one this will write is the whole of what the reader
                  could not otherwise know. */}
              <MenuItem onClick={onShare} disabled={share.isPending}>
                <ListItemIcon>
                  <CloudUploadRoundedIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>
                  {`Export to share (${summary.postprocessed ? 'postprocessed' : 'raw'})`}
                </ListItemText>
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
          shareCopy ? (
            // A share copy exists, so download is one of two things you might want and the
            // icon becomes a menu. With no copy there is no menu and no second click --
            // the common case stays one press.
            <>
              <Tooltip title="Download">
                <IconButton
                  size="small"
                  onClick={(e) => setDownloadAnchor(e.currentTarget)}
                  aria-label="download options"
                >
                  <DownloadRoundedIcon fontSize="small" />
                </IconButton>
              </Tooltip>
              <Menu
                anchorEl={downloadAnchor}
                open={Boolean(downloadAnchor)}
                onClose={() => setDownloadAnchor(null)}
              >
                <MenuItem
                  component="a"
                  href={robovast.archiveUrl(id)}
                  download={`${id}.tar.gz`}
                  onClick={() => setDownloadAnchor(null)}
                >
                  <ListItemIcon>
                    <DownloadRoundedIcon fontSize="small" />
                  </ListItemIcon>
                  <ListItemText>Download</ListItemText>
                </MenuItem>
                {/* Omitted where the provider has no openable link -- sftp never has one,
                    and a webdav URL often needs credentials the recipient lacks. */}
                {shareCopy.url ? (
                  <MenuItem
                    onClick={() => {
                      void navigator.clipboard?.writeText(shareCopy.url as string)
                      setDownloadAnchor(null)
                    }}
                  >
                    <ListItemIcon>
                      <LinkRoundedIcon fontSize="small" />
                    </ListItemIcon>
                    <ListItemText>Copy share link</ListItemText>
                  </MenuItem>
                ) : null}
              </Menu>
            </>
          ) : (
            <Tooltip title="Download">
              <IconButton
                size="small"
                component="a"
                href={robovast.archiveUrl(id)}
                download={`${id}.tar.gz`}
                aria-label="download"
              >
                <DownloadRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          )
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

      {/* The new campaign's card appears at the top of the list — it is the live one — which is
          not the same as knowing WHICH id is yours, so the id is named here. Its own description
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

      {/* Accepting the export says nothing this page does not already show: the service only
          acknowledges that the background upload started, and its phase is live in the chip
          below. Only a refusal (ok=false, e.g. the busy guard) or a failed request needs
          saying. */}
      {share.isError ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          Upload-to-share failed.
          <ErrorText>{(share.error as Error).message}</ErrorText>
        </Alert>
      ) : share.data && !share.data.ok ? (
        <Alert severity="warning" sx={{ mb: 1 }}>
          <ErrorText>{share.data.message ?? 'Upload-to-share had no effect.'}</ErrorText>
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

export function Monitor({
  shareImport,
  onShareImportConsumed,
}: {
  /** A search string from a `#/execution?import=` link: open the share dialog on it. */
  shareImport?: string
  /** Called once that request has been taken, so the URL stops carrying a spent one. */
  onShareImportConsumed?: () => void
}) {
  const { data, error, live, reconnect } = useCampaignStream()
  const [importAnchor, setImportAnchor] = useState<HTMLElement | null>(null)
  const [shareOpen, setShareOpen] = useState<string | null>(null)
  // The list is handed to the importer because it is how the import reports itself: the campaign
  // appears at phase `importing` and its stage report is read once it settles. Reconnecting on
  // start is belt-and-braces for a stream that has silently died meanwhile.
  const importer = useCampaignImport(data?.campaigns, reconnect,
                                     () => setImportAnchor(null))

  // Whether this deployment has a share at all -- the one thing that greys the menu entry out.
  // Same react-query key every campaign card uses, so the page still makes one request.
  const shareListing = useQuery({
    queryKey: ['shareArchives'],
    queryFn: () => robovast.listShareArchives(),
    staleTime: 60_000,
    retry: false,
  })
  const noShare = shareListing.data?.configured === false

  // Held stable across renders because the dialog memoises its rows on it: a fresh Set every
  // render would recompute them on every campaign the stream pushes, which is a memo that
  // never hits and reads as though it does.
  const presentIds = useMemo(
    () => new Set((data?.campaigns ?? []).map((c) => c.campaign_id)),
    [data?.campaigns],
  )

  // A link somebody was handed. Taken once and cleared: delivering the request is what spends
  // it, so the address stops claiming a dialog after it is closed and pasting the same link
  // again is a real hash change that opens it afresh.
  useEffect(() => {
    if (!shareImport) return
    setShareOpen(shareImport)
    onShareImportConsumed?.()
  }, [shareImport, onShareImportConsumed])

  return (
    // `cursor` is inherited, so this one rule covers the whole view: nothing here is
    // editable, and a text caret over a campaign's name, its phase or its timestamp offers
    // an edit the card does not have. The exceptions set their own and are unaffected --
    // MUI gives inputs `text` and buttons `pointer`, and the Details hovers ask for `help`.
    // Selection still works everywhere; only the invitation is withdrawn.
    <Stack spacing={2} sx={{ cursor: 'default' }}>
      <LaunchBar />

      {/* Refresh sits beside the title, as it does in the Explorer and the run view: it acts on
          the list this heading names, and a control next to what it governs needs no label. The
          import menu is the one thing here pushed right — it adds to the list rather than
          reloading it, and belongs at the end of the row for the same reason. */}
      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="h6">Campaigns</Typography>
        <Tooltip title="Reload the campaign list">
          <IconButton
            size="small"
            aria-label="Reload campaign list"
            onClick={reconnect}
            sx={{ color: 'common.white' }}
          >
            <RefreshRoundedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        {data && !live ? (
          <Typography variant="caption" color="text.secondary">
            reconnecting…
          </Typography>
        ) : null}
        <Box flexGrow={1} />
        {/* A menu rather than a bare button, like both of a campaign card's own controls: a
            campaign can come from a file on your machine or off the configured share, and
            those are two transfers, not one with a setting. */}
        <Tooltip title={importer.busy ? 'Uploading…' : 'Bring a campaign in'}>
          <IconButton
            size="small"
            aria-label="import a campaign"
            onClick={(e) => setImportAnchor(e.currentTarget)}
            sx={{ color: 'common.white' }}
          >
            {/* The upload's only sign while it runs: picking a file closes this menu, so the
                item's own spinner is off screen for the whole transfer. */}
            {importer.busy ? (
              <CircularProgress size={18} />
            ) : (
              <UploadFileRoundedIcon fontSize="small" />
            )}
          </IconButton>
        </Tooltip>
        <Menu
          anchorEl={importAnchor}
          open={Boolean(importAnchor)}
          onClose={() => setImportAnchor(null)}
        >
          {importer.menuItem}
          {/* Disabled only for a deployment that HAS no share. A listing still in flight, or
              one that failed because the share is unreachable, leaves it enabled — the dialog
              says which of those happened, and greying it out would report a network problem
              as a deployment without a share.
              The tooltip appears only in that disabled case, and only then is the item wrapped
              in a span: a disabled item fires no events for a tooltip to listen to, but a
              wrapper around an ENABLED one hides it from MenuList's keyboard navigation. */}
          {noShare ? (
            <Tooltip title="No share is configured for this deployment">
              <span>
                <MenuItem disabled>
                  <ListItemIcon>
                    <CloudDownloadRoundedIcon fontSize="small" />
                  </ListItemIcon>
                  <ListItemText>Import from Share</ListItemText>
                </MenuItem>
              </span>
            </Tooltip>
          ) : (
            <MenuItem
              onClick={() => {
                setImportAnchor(null)
                setShareOpen('')
              }}
            >
              <ListItemIcon>
                <CloudDownloadRoundedIcon fontSize="small" />
              </ListItemIcon>
              <ListItemText>Import from Share</ListItemText>
            </MenuItem>
          )}
        </Menu>
      </Stack>

      {/* Under the header row, not in it: an import's report is four stage lines, which inside
          that flex row would push the heading and its controls around. */}
      {importer.panel}

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
      {/* Mounted only while open, so its search box and its list start fresh each visit. Not
          `lazyView`: unlike the postprocessing dialog it drags in no editor, and a chunk
          fetched on click would only add a spinner between the menu and the list. */}
      {shareOpen !== null ? (
        <ShareImportDialog
          open
          initialSearch={shareOpen}
          presentIds={presentIds}
          onClose={() => setShareOpen(null)}
        />
      ) : null}
    </Stack>
  )
}
