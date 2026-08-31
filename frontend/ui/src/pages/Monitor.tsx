import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { lazyView } from '@/lib/lazyView'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Collapse from '@mui/material/Collapse'
import Divider from '@mui/material/Divider'
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
import MenuRoundedIcon from '@mui/icons-material/MenuRounded'
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
  type ShareArchive,
  type Status,
} from '@/lib/robovastClient'
import { ConfigIcon, ExplorerIcon, RunViewIcon } from '@/components/viewIcons'
import { useCampaignStream } from '@/components/CampaignStreamProvider'
import { useToasts } from '@/components/ToastProvider'
import { ShareImportDialog } from './ShareImportDialog'
import { campaignLink, openCampaignConfig, openResultsView } from '@/lib/nav'
import { preferredArchive } from '@/lib/shareArchives'
import { formatAge, formatLocalClock, formatLocalTime } from '@/lib/time'
import { formatDuration } from '@/lib/format'
import { campaignEtaSeconds } from '@/lib/eta'
import { stallVerdict } from '@/lib/stall'
import { runsFromSummary } from '@/lib/runMeter'
import { useActiveView } from '@/lib/activeView'
import { ErrorText, MiniRunMeter, StatusView } from '@/components/StatusView'
import { CampaignOrigin } from '@/components/CampaignOrigin'
import { HoverFacts } from '@/components/HoverFacts'
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

// The campaign id's column, fixed so a page of collapsed cards reads down its columns instead of
// zig-zagging. Sized against the ids campaigns actually get, measured rather than guessed: the
// controller appends `-YYYY-MM-DD-HHMMSS` plus two hundredths (see `_campaign_id_lock`), which is
// 20 characters of stamp before the project name, so a name as ordinary as `nav-search-halton`
// reaches 37. Sized short at first and it cut the stamp off exactly those ids -- and the stamp is
// the END of the string, the only part that tells two runs of the same project apart, which is
// the one thing this column must never be the reason a reader cannot see.
const ID_COLUMN = 360

// The trailing controls' column: the actions menu, the fold, and on a live card the Stop button.
// Reserved rather than shrink-to-fit because not every card has every control — a campaign still
// building has no actions menu — and without a fixed width those rows pull their meter and age
// sideways out of line with every other row.
//
// Sized for the WIDEST set (three small icon buttons and the two gaps between them), not the
// common one. A minimum that the busiest row exceeds is not a reserved column at all: the stack
// then sizes to its content, and anything that comes and goes inside it moves the whole flexible
// span to its left. That is what a per-poll fetch spinner did here — it appeared and vanished
// every 1.5 s on a running card and walked the age back and forth beside it.
const CONTROLS_COLUMN = 108

// The age column. Fixed and right-aligned so the ages read down the page as one column; wide
// enough for the longest string formatAge produces.
const AGE_COLUMN = 78

/** How long a finished campaign took, or null while it is still running / was never recorded.
 *  Derivable from the listing alone, and shown nowhere else — the card had a start time and no
 *  duration, which is the half of the pair a reader of a FINISHED campaign wants. */
function campaignDuration(summary: CampaignSummary): string | null {
  if (!summary.started_at || !summary.finished_at) return null
  const from = new Date(summary.started_at).getTime()
  const to = new Date(summary.finished_at).getTime()
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return null
  return formatDuration((to - from) / 1000)
}

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
function CampaignCard({ summary, newest, openedByLink }: {
  summary: CampaignSummary; newest: boolean; openedByLink?: boolean
}) {
  const qc = useQueryClient()
  const id = summary.campaign_id
  const active = useActiveView()

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
    // And the same again for switching *pages* rather than tabs, which this page is kept
    // mounted across: the poll stops while Campaigns is not the view on screen, and returning
    // re-reads. See lib/activeView.tsx.
    enabled: active,
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
    // `active` for the reason the status above carries it, and with more at stake: on the
    // cluster lane this is a Kubernetes API call per live campaign every two seconds, and it
    // was being made for a page nobody was looking at.
    enabled: active && !bornAtRest,
    refetchInterval: () => (terminal ? false : 2000),
    refetchOnWindowFocus: true,
  })

  // Stopping the poll on its own leaves the last in-flight listing on screen forever —
  // jobs that were still `running` up to one poll before the campaign ended keep their
  // rows, so the live view never empties out. Read once more after the phase turns
  // terminal to pick up their final state.
  //
  // If the campaign ends while this page is not the one on screen, `invalidateQueries` reaches a
  // disabled observer and refetches nothing — it marks the listing stale, and the read lands when
  // the page is next entered. Deferred, not lost, and deferred to the moment someone can see it.
  useEffect(() => {
    if (terminal) qc.invalidateQueries({ queryKey: ['jobs', id] })
  }, [terminal, id, qc])

  // Declared above the mutations because their onError/onSuccess handlers report through it.
  const { confirm, prompt } = useDialogs()
  const { notify } = useToasts()

  // Every action below reports its own outcome. A failure goes to a STICKY toast rather than an
  // Alert on this card: the card's Alert had nothing that ever cleared it -- no mutation is
  // reset and the card does not unmount -- so a refusal sat there until the tab was reloaded,
  // outliving the thing it was about. A sticky toast still waits for the reader; it just does
  // not become part of the campaign.
  const failed = (what: string, key: string) => (e: unknown) => notify({
    severity: 'error', key, message: what, note: (e as Error).message,
  })

  const stop = useMutation({
    mutationFn: () => robovast.stop(id),
    onError: failed('Stop failed.', `stop:${id}`),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
      // A refusal the service returns rather than raises (the busy guard, mostly). Kept a
      // warning, as it was on the card: it is an expected answer, not a fault.
      if (res && !res.ok) {
        notify({ severity: 'warning', key: `stop:${id}`, message: 'Stop had no effect.',
                 note: res.message || undefined })
      }
    },
  })

  // One job, killed by hand; the campaign keeps running. Invalidates the jobs query (the row's
  // status changes) and the status one (its `runs.killed` counter moves) — which is also why
  // this lives here rather than in StatusView: those queries are owned by this card.
  const stopJob = useMutation({
    mutationFn: ({ jobName, reason }: { jobName: string; reason?: string }) =>
      robovast.stopJob(id, jobName, reason),
    // A warning, not an error, and therefore not sticky: this refusal is the EXPECTED outcome
    // when the job finished between the poll that drew the button and the click, and the
    // server's message (which names the phase) is the whole explanation. That judgement was
    // already in the card it is replacing; only the place it appears has changed.
    onError: (e: unknown) => notify({
      severity: 'warning', key: `stopjob:${id}`, message: 'Could not stop that job.',
      note: (e as Error).message,
    }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['jobs', id] })
      qc.invalidateQueries({ queryKey: ['status', id] })
      if (res && !res.ok) {
        notify({ severity: 'warning', key: `stopjob:${id}`,
                 message: 'Stopping the job had no effect.', note: res.message || undefined })
      }
    },
  })


  // Stopping a campaign is asked about, because it is not undoable and not recoverable: there is
  // no resume anywhere in the service, so the only way back is Retrigger, which is a NEW campaign
  // starting from run zero. The button also sits in a row of small icons a click can land on by
  // accident, and it is the only one there that destroys work in progress.
  //
  // The count is the point of asking rather than the wording: what a reader needs before pressing
  // this is how much is actually in flight right now, which nothing else on a folded card says.
  // Omitted rather than guessed at when the jobs listing has not answered — a confident "0 runs"
  // over a campaign with six running would be worse than no number at all.
  const onStop = async () => {
    const inFlight = jobs.data?.counts?.running
    const ok = await confirm({
      title: 'Stop this campaign?',
      message: (
        <>
          <code>{id}</code> ends now — not at the end of the current batch.{' '}
          {inFlight
            ? `The ${inFlight} run${inFlight === 1 ? '' : 's'} still executing ${
                inFlight === 1 ? 'is' : 'are'} terminated and deliver${inFlight === 1 ? 's' : ''} no result.`
            : 'Any run still executing is terminated and delivers no result.'}{' '}
          What has already been recorded is kept. A stopped campaign cannot be resumed — Retrigger
          campaign, in the actions menu, starts a new one from this one's configuration.
        </>
      ),
      confirmLabel: 'Stop campaign',
      danger: true,
    })
    if (ok) stop.mutate()
  }

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

  // Wraps a control that sits inside the row-wide fold target: the row folds on any click the
  // controls do not claim, so a control must claim its own or it both acts and folds the card
  // under itself. The chevron needs it too — folding twice is folding not at all.
  const claim = (fn: () => void) => (e: MouseEvent) => {
    e.stopPropagation()
    fn()
  }
  const [ppOpen, setPpOpen] = useState(false)

  const del = useMutation({
    mutationFn: () => robovast.deleteCampaign(id),
    onError: failed('Delete failed.', `delete:${id}`),
    // The row (and every cached query for this campaign) is gone on success — which is also why
    // the toast is the only thing left that can say what went.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['campaigns'] })
      notify({ severity: 'success', message: `Deleted ${id}` })
    },
  })

  const onReprocess = () => {
    closeMenu()
    setPpOpen(true)
  }

  // Unlike the two entries below it, this one produces a DIFFERENT campaign — so it
  // invalidates the listing (where the new card appears) and nothing about this one.
  const retrigger = useMutation({
    mutationFn: () => robovast.retriggerCampaign(id),
    // The same key its success uses: retrying replaces the refusal in place rather than leaving
    // a stale one above the notice that supersedes it.
    onError: failed('Retrigger failed — this campaign was not modified.', `retrigger:${id}`),
    onSuccess: (ref) => {
      qc.invalidateQueries({ queryKey: ['campaigns'] })
      // The new campaign's card appears at the top of the list — it is the live one — which is
      // not the same as knowing WHICH id is yours, so the id is named here. Keyed on this
      // campaign so leaning on the menu entry refreshes one notice instead of stacking them.
      notify({
        severity: 'success',
        key: `retrigger:${id}`,
        message: `Retriggered as ${ref.campaign_id}`,
        note: ref.note || undefined,
      })
    },
  })

  const onRetrigger = () => {
    closeMenu()
    retrigger.mutate()
  }

  const share = useMutation({
    mutationFn: () => robovast.runShare(id),
    onError: failed('Upload-to-share failed.', `share:${id}`),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['status', id] })
      qc.invalidateQueries({ queryKey: ['campaigns'] })
      // Accepting the export says nothing this page does not already show — the phase chip is
      // live — so only a refusal is worth saying.
      if (res && !res.ok) {
        notify({ severity: 'warning', key: `share:${id}`,
                 message: 'Upload-to-share had no effect.', note: res.message || undefined })
      }
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
  // it. A project that declares no budget gets no verdict, and is told so once by
  // `validate_project`, where the missing budget is still fixable.
  //
  // Derived in `lib/stall.ts`, which mirrors `stall_report` in the status contract gate for
  // gate and carries a test per gate. It used to be derived inline here, and drifted from
  // the contract three times: a phase that executes no runs judged against a per-run budget,
  // then that rule half-applied to only the pre-run phases, then a batch queued for cluster
  // capacity called stalled while the MCP and CLI both refused to.
  //
  // Tri-state, so only `true` may render: showing `null` as "not stalled" would put a
  // reassuring label on a run that may already be dead.
  const { stalled, ageS: progressAgeS } = stallVerdict(status.data)
  const progressDeadline = status.data?.progress_deadline_s
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
    // Another system's state, read with the service's credentials: worth re-reading on arrival,
    // and worth the 60s floor that bounds how often arriving can cost that.
    enabled: active,
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

  // Folded shut, the card is its header row: the run meter shrinks into that row and the jobs
  // list, the Details panel and the log are not mounted at all. A page of finished campaigns is
  // otherwise metres of scroll over sections nobody opened.
  //
  // Every card starts folded now, running ones included — one rule, no exceptions, nothing that
  // opens itself. A running campaign's folded row carries its live meter, its failure counts and
  // its finishing estimate, which is the glance; opening it is the deliberate step to investigate.
  //
  // The cost of that rule, recorded because it is real: the campaign a reader came to watch is
  // shut when they arrive, and costs a click every visit. It buys little height (the usual page
  // is one running campaign above many finished ones); what it buys is predictability.
  const [collapsed, setCollapsed] = useState(!openedByLink)
  // A link that names this campaign opens it and brings it into view. Keyed on the flag rather
  // than run once, so following a second link while the page is already open works too — the
  // page stays mounted under KeepAlive, so "on arrival" is not a mount.
  const cardRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!openedByLink) return
    setCollapsed(false)
    cardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [openedByLink])
  // A click that ends a drag-selection in the header row was a copy, not a fold. The id and the
  // folded description are selectable (see below) and folding the card as the text is lifted
  // takes it back — worse here than in a CollapsibleBox, because shutting the card also moves
  // the description out of the row it was selected in. A plain click cannot trip this: its own
  // mousedown collapses any earlier selection before the click event fires.
  const toggle = () => {
    if (window.getSelection()?.toString()) return
    setCollapsed((c) => !c)
  }
  // What the compact meter draws before this card's own `getStatus` answers. The listing arrives
  // for the whole page in one stream while the statuses are one request each, so without it every
  // meter paints empty and fills in one by one — a page that looks like it is still loading long
  // after it is readable. Superseded the moment the real status lands; see runsFromSummary.
  // The finishing estimate for the row, campaign-level or absent — see campaignEtaSeconds. Read
  // off the live status and the live job counts, both of which this card already holds.
  const eta = status.data ? campaignEtaSeconds(status.data, jobs.data?.counts, !running) : null
  const meterStatus =
    status.data ??
    // `mode` comes from the listing too, so a search campaign's ring is offered from the first
    // paint rather than appearing a beat later; its rounds are not on the listing, so it draws
    // its empty state until the status lands.
    ({ phase, mode: summary.mode, runs: runsFromSummary(summary), budget: [] } as unknown as Status)

  // The actions menu's entries, built rather than written inline so the button can ask whether it
  // has anything to offer. A `Divider` between groups only where the group before it is present —
  // a menu opening on a rule, or carrying two in a row, reads as a rendering fault.
  const openItems = [
    // Unconditional: every campaign in this list has a card, which is what the link addresses.
    // Nothing about it depends on the campaign having got far enough to freeze a config or
    // produce results.
    <MenuItem
      key="cardlink"
      onClick={() => {
        void navigator.clipboard?.writeText(campaignLink(id))
        closeMenu()
        notify({
          severity: 'success', key: 'campaign-link', message: 'Link to this campaign copied',
        })
      }}
    >
      <ListItemIcon><LinkRoundedIcon fontSize="small" /></ListItemIcon>
      <ListItemText>Copy link to this campaign</ListItemText>
    </MenuItem>,
    hasConfig ? (
      <MenuItem key="config" onClick={() => { closeMenu(); openCampaignConfig(id) }}>
        <ListItemIcon><ConfigIcon fontSize="small" /></ListItemIcon>
        <ListItemText>Open configuration</ListItemText>
      </MenuItem>
    ) : null,
    canExplore ? (
      <MenuItem key="explorer" onClick={() => { closeMenu(); openResultsView('explorer', id) }}>
        <ListItemIcon><ExplorerIcon fontSize="small" /></ListItemIcon>
        <ListItemText>Open in results Explorer</ListItemText>
      </MenuItem>
    ) : null,
    canReplay ? (
      <MenuItem key="runview" onClick={() => { closeMenu(); openResultsView('run', id) }}>
        <ListItemIcon><RunViewIcon fontSize="small" /></ListItemIcon>
        <ListItemText>Replay runs in the Run view</ListItemText>
      </MenuItem>
    ) : null,
  ].filter(Boolean)

  const takeItems = canDownload
    ? [
        <MenuItem
          key="download"
          component="a"
          href={robovast.archiveUrl(id)}
          download={`${id}.tar.gz`}
          onClick={closeMenu}
        >
          <ListItemIcon><DownloadRoundedIcon fontSize="small" /></ListItemIcon>
          <ListItemText>Download</ListItemText>
        </MenuItem>,
        // Omitted where the provider has no openable link -- sftp never has one, and a webdav
        // URL often needs credentials the recipient lacks.
        shareCopy?.url ? (
          <MenuItem
            key="sharelink"
            onClick={() => {
              void navigator.clipboard?.writeText(shareCopy.url as string)
              closeMenu()
              notify({ severity: 'success', key: 'share-link', message: 'Share link copied' })
            }}
          >
            <ListItemIcon><LinkRoundedIcon fontSize="small" /></ListItemIcon>
            <ListItemText>Copy share link</ListItemText>
          </MenuItem>
        ) : null,
      ].filter(Boolean)
    : []

  // Nothing here while the campaign runs: each one either re-runs a step of it or destroys it.
  const actItems = running
    ? []
    : [
        // The only entry that starts a SEPARATE campaign rather than re-running a step of this one.
        <MenuItem key="retrigger" onClick={onRetrigger} disabled={retrigger.isPending}>
          <ListItemIcon><ReplayRoundedIcon fontSize="small" /></ListItemIcon>
          <ListItemText>Retrigger campaign</ListItemText>
        </MenuItem>,
        <MenuItem key="reprocess" onClick={onReprocess}>
          <ListItemIcon><QueryStatsRoundedIcon fontSize="small" /></ListItemIcon>
          <ListItemText>Retrigger postprocessing</ListItemText>
        </MenuItem>,
        // Named, because which variant lands is not a choice here and never was:
        // `campaign_variant` reads it off the campaign directory, and once postprocessing has
        // written into that tree the raw campaign no longer exists to export. Saying which one
        // this will write is the whole of what the reader could not otherwise know.
        <MenuItem key="share" onClick={onShare} disabled={share.isPending}>
          <ListItemIcon><CloudUploadRoundedIcon fontSize="small" /></ListItemIcon>
          <ListItemText>
            {`Export to share (${summary.postprocessed ? 'postprocessed' : 'raw'})`}
          </ListItemText>
        </MenuItem>,
        <MenuItem key="delete" onClick={onDelete} sx={{ color: 'error.main' }}>
          <ListItemIcon><DeleteOutlineRoundedIcon fontSize="small" color="error" /></ListItemIcon>
          <ListItemText>Delete</ListItemText>
        </MenuItem>,
      ]

  const menuItems = [openItems, takeItems, actItems]
    .filter((group) => group.length)
    .flatMap((group, i) => (i ? [<Divider key={`sep-${i}`} />, ...group] : group))

  return (
    <Paper ref={cardRef} sx={{ px: 2, py: collapsed ? 0.75 : 2 }}>
      {/* The fold's click target is the WHOLE header, chevron row included: a folded card is
          opened by clicking the line it occupies, not by aiming at one of the two spans that
          happen to hold text. It bleeds into the Paper's own padding (negative margin, the same
          padding added back inside) so the strip beside and above the row folds too — an edge
          that looks like part of the row and did not act like it.

          The controls stop the click from propagating; everything else in here is inert. */}
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        onClick={toggle}
        // `userSelect: 'none'` is the row's default, not its rule: dragging across a click
        // target should not paint its phase dot and its age. The text worth copying opts back
        // in individually.
        sx={{
          cursor: 'pointer',
          userSelect: 'none',
          // The Paper's padding, cancelled outside and restored inside, so the padded strip is
          // part of the target and nothing moves. The gap the row used to hold below itself
          // (`mb`) is folded into the bottom margin rather than kept as a second rule.
          mx: -2,
          px: 2,
          mt: collapsed ? -0.75 : -2,
          mb: collapsed ? -0.75 : -0.5,
          py: collapsed ? 0.75 : 2,
        }}
      >
        <Stack
          direction="row"
          spacing={1}
          alignItems="center"
          sx={{ minWidth: 0, flexGrow: 1 }}
        >
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
              noWrap
              title={
                `No run has completed for ${formatDuration(progressAgeS!)}, past the ` +
                `${progressDeadline}s expected per run — the run is not merely slow. Read ` +
                `what it is repeating in the log panel below.`
              }
            >
              stalled {formatDuration(progressAgeS!)}
            </Typography>
          ) : null}
          {/* A fixed column, not a shrink-to-fit label: campaign ids carry a user-supplied name,
              so their widths vary by a factor of three, and without a column the timestamp and
              the meter on every row below would sit at a different x. Applied whether the card is
              open or shut so folding one does not shift the header sideways. */}
          {/* Selectable, against the fold target it sits in: this is the one string on the
              card that gets copied out — into a CLI, a message, an issue — and a header that
              refuses the drag leaves retyping a timestamped id by hand as the only way. The
              cursor stays the row's pointer; an I-beam would suggest a field that takes typing.
              `noWrap` clips the column visually only, so a truncated id still copies whole. */}
          <CampaignOrigin origin={summary.origin}>
            <Typography
              variant="subtitle2"
              noWrap
              title={id}
              sx={{
                fontFamily: 'monospace',
                width: ID_COLUMN,
                flexShrink: 0,
                userSelect: 'text',
              }}
            >
              {id}
            </Typography>
          </CampaignOrigin>
          <LaunchedBy name={summary.created_by} />
          {/* Folded, the description moves up into the row. On its own line it doubled the height
              of every collapsed card, which is most of what the fold was for; here it also lines
              up into a column that can be read down. The full text is on hover, and it returns to
              its own full-width line the moment the card opens. */}
          {collapsed && summary.description ? (
            <Typography
              variant="body2"
              color="text.secondary"
              noWrap
              title={summary.description}
              sx={{ minWidth: 0, flexGrow: 1, userSelect: 'text' }}
            >
              {summary.description}
            </Typography>
          ) : (
            <Box flexGrow={1} />
          )}
          {/* One column, two questions, decided by whether the campaign is still going.
        
              A campaign that is OVER is asked "which of these is recent", and the honest answer
              is when it FINISHED, not when it started: one that ran for eight hours and ended a
              minute ago is the freshest thing on the page and its start time says three days.
              The listing orders the terminal group the same way, so this column and the order
              agree — see `list_campaigns`. It falls back to the start time for a campaign whose
              record carries no terminal outcome, which is also how such a campaign is ordered.
              An age rather than a wall clock, because the id already ends in a
              `-YYYY-MM-DD-HHMMSS` stamp; the exact times and the duration are on the hover.
        
              A campaign that is RUNNING is asked "how much longer": the estimate. It is empty
              rather than dashed when none can be had — nothing has finished yet, or it is a
              search whose rounds nothing bounds (see campaignEtaSeconds) — because an empty cell
              reads as "not known" while a dash reads as a value. */}
          {summary.started_at ? (
            <HoverFacts
              facts={
                running
                  ? [
                      { label: 'started', value: formatLocalTime(summary.started_at) },
                      { label: 'running for', value: formatAge(summary.started_at) },
                      { label: 'expected finish', value: eta != null ? formatLocalClock(eta) : null },
                    ]
                  : [
                      { label: 'started', value: formatLocalTime(summary.started_at) },
                      { label: 'finished', value: formatLocalTime(summary.finished_at) },
                      { label: 'took', value: campaignDuration(summary) },
                    ]
              }
            >
              <Typography
                variant="caption"
                color="text.secondary"
                noWrap
                sx={{ flexShrink: 0, width: AGE_COLUMN, textAlign: 'right', cursor: 'help' }}
              >
                {running
                  ? eta != null
                    ? `~${formatDuration(eta)} left`
                    : ''
                  : formatAge(summary.finished_at ?? summary.started_at)}
              </Typography>
            </HoverFacts>
          ) : null}
          {stage && !collapsed ? (
            // Inside the flexible span and BEFORE the time cell, not after it. The marker's text
            // changes every few seconds, so its width does too; out here it pushed everything to
            // its left, and what sits to its left now is a live estimate that must not jitter.
            // In this position the description/spacer absorbs the change instead. `noWrap` and
            // capped so a long step line truncates rather than shoving the buttons off; the full
            // text is on hover and the Log tab has the rest.
            <Typography
              variant="caption"
              color="text.secondary"
              noWrap
              title={stage}
              sx={{ minWidth: 0, maxWidth: '40%', flexShrink: 1, fontFamily: 'monospace' }}
            >
              {stage}
            </Typography>
          ) : null}
          {collapsed ? (
            <MiniRunMeter
              status={meterStatus}
              campaignId={id}
              counts={jobs.data?.counts}
              started={summary.started_at}
              finished={summary.finished_at}
            />
          ) : null}
        </Stack>
        {/* The controls, as a right-aligned column of their own. A fixed minimum, because which
            of them exist varies per campaign — a stopped one has no Explorer or Run-view
            shortcut — and without a column those two rows pushed their timestamp and meter
            sideways, which is exactly the jitter the fixed id column exists to prevent. A minimum
            rather than a fixed width so a running card's wider Stop button can still grow. */}
        <Stack
          direction="row"
          spacing={1}
          alignItems="center"
          // No handler of its own: the column is mostly empty space on a folded row, and space
          // that looks like the row must fold like the row. Each control claims its own click
          // instead (`claim`), which is the smallest thing that is not the fold.
          sx={{ minWidth: CONTROLS_COLUMN, flexShrink: 0, justifyContent: 'flex-end' }}
        >
        {/* Left of the menu, and an icon like everything else in this column: a labelled button
            here was the one control wide enough to set the column's width, so a live card's
            meter and age sat out of line with every folded row beneath it. The word moves to the
            hover, where it can say what the button actually does — which "Stop" never did: it
            ends the campaign AND kills its in-flight jobs, and is not the per-job stop in the
            Jobs list below. It stays a button of its own rather than a menu entry, though: it is
            the one action on a live campaign that must not be two clicks away. */}
        {running ? (
          <Tooltip
            title={
              stop.isPending
                ? ''
                : 'Stop this campaign — ends it and kills its running jobs. ' +
                  'To stop one job and let the rest carry on, use the Jobs list.'
            }
          >
            <IconButton
              size="small"
              color="error"
              aria-label="stop campaign"
              disabled={stop.isPending}
              onClick={claim(() => void onStop())}
            >
              {stop.isPending ? <CircularProgress size={16} /> : <StopRoundedIcon fontSize="small" />}
            </IconButton>
          </Tooltip>
        ) : null}
        {/* One menu, not a row of shortcuts. Config, Explorer, Run-view, Download and the actions
            were five to seven icon buttons wide depending on the campaign — a bar of small
            same-sized glyphs that has to be learnt before it can be used, on every row of a list
            whose rows are meant to be scanned. In here each one is a named line instead, and the
            row keeps two controls: this, and the fold.

            Ordered by what the reader came for: open something, take something away, re-run
            something, destroy something. Every entry is conditional, so the button is only
            offered when at least one of them exists (a campaign still building has no
            configuration frozen yet and nothing else to act on). */}
        {menuItems.length ? (
          <>
            {/* Empty title while the delete is in flight: the button is disabled then, and a
                disabled button fires no events for the tooltip to listen to (MUI warns about it). */}
            <Tooltip title={del.isPending ? '' : 'Campaign actions'}>
              <IconButton
                size="small"
                aria-label="campaign actions"
                aria-haspopup="menu"
                onClick={(e) => {
                  e.stopPropagation()
                  // Opening the menu is the moment someone is about to try again, so the last
                  // attempt's state stops being about anything. The toast it raised stays --
                  // it is theirs to dismiss — but `isPending`/`isError` here are cleared so a
                  // stale one cannot resurface or disable an entry.
                  for (const m of [stop, stopJob, del, retrigger, share]) {
                    if (!m.isPending) m.reset()   // in flight: its spinner is still true
                  }
                  setMenuAnchor(e.currentTarget)
                }}
                disabled={del.isPending}
              >
                {del.isPending ? (
                  <CircularProgress size={16} />
                ) : (
                  <MenuRoundedIcon fontSize="small" />
                )}
              </IconButton>
            </Tooltip>
            {/* The menu is portalled out of the card but its events still travel this React
                tree, so without this every entry picked from it also folded the card. */}
            <Menu
              anchorEl={menuAnchor}
              open={!!menuAnchor}
              onClose={closeMenu}
              onClick={(e) => e.stopPropagation()}
            >
              {menuItems}
            </Menu>
          </>
        ) : null}
        {/* Last, and offered on every card: a running campaign can be folded away too — a lane
            running six of them is six full cards, and the one being watched is usually one of
            them. Only the DEFAULT differs (see `collapsed`). This is also the keyboard and
            screen-reader control for the fold; the row-wide click target above is the mouse
            affordance, not the announced one. */}
        <Tooltip title={collapsed ? 'Show this campaign' : 'Collapse this campaign'}>
          <IconButton
            size="small"
            aria-label={collapsed ? 'expand campaign' : 'collapse campaign'}
            aria-expanded={!collapsed}
            onClick={claim(toggle)}
          >
            {collapsed ? (
              <KeyboardArrowDownRoundedIcon fontSize="small" />
            ) : (
              <KeyboardArrowUpRoundedIcon fontSize="small" />
            )}
          </IconButton>
        </Tooltip>
        </Stack>
      </Stack>

      {/* Its own full-width line only while the card is open; folded, it rides in the header row
          above. Never both. */}
      {!collapsed && summary.description ? (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {summary.description}
        </Typography>
      ) : null}

      {/* Every action's outcome — refusal included — is reported by its mutation, as a toast.
          Nothing about an action lives on this card: an Alert here had nothing that cleared it,
          so it outlived what it was about by as long as the tab stayed open. */}

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

      {/* Everything below the header, folded away as one unit. `unmountOnExit` is the point,
          not a detail: it is what keeps a collapsed card from mounting DetailsBox and
          CampaignLog at all, so it issues no Details queries and opens no log stream. */}
      <Collapse in={!collapsed} unmountOnExit>
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
      </Collapse>

      {ppOpen && (
        <PostprocessingDialog campaignId={id} open onClose={() => setPpOpen(false)} />
      )}
    </Paper>
  )
}

export function Monitor({
  openCampaign,
  shareImport,
  onShareImportConsumed,
}: {
  /** A search string from a `#/execution?import=` link: open the share dialog on it. */
  shareImport?: string
  /** A campaign to open and scroll to on arrival — `#/execution?campaign=<id>`. An instruction,
   *  not state: the reader folds and unfolds freely afterwards and the link does not argue. */
  openCampaign?: string
  /** Called once that request has been taken, so the URL stops carrying a spent one. */
  onShareImportConsumed?: () => void
}) {
  const { data, error, live, reconnect } = useCampaignStream()
  const active = useActiveView()
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
    // Another system's state, read with the service's credentials: worth re-reading on arrival,
    // and worth the 60s floor that bounds how often arriving can cost that.
    enabled: active,
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
          <CampaignCard
            key={c.campaign_id}
            summary={c}
            newest={i === 0}
            openedByLink={c.campaign_id === openCampaign}
          />
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
