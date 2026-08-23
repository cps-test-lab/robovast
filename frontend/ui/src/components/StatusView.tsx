import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import ArrowDownwardRoundedIcon from '@mui/icons-material/ArrowDownwardRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Fab from '@mui/material/Fab'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import {
  robovast,
  isTerminalPhase,
  type JobSummary,
  type ListJobsResponse,
  type Status,
} from '@/lib/robovastClient'
import {
  estimateBatchesEtaSeconds,
  estimateEtaSeconds,
  finishedRuns,
  noResultRuns,
} from '@/lib/eta'
import { formatDuration } from '@/lib/format'
import { useLiveStream } from '@/lib/liveStream'
import { useQuery } from '@tanstack/react-query'
import { formatLocalClock } from '@/lib/time'
import { BatchObjectiveChart } from './BatchObjectiveChart'
import { CollapsibleBox } from './CollapsibleBox'
import { containerColorer } from './containerColor'
import { DetailsBox } from './DetailsBox'
import { MeterBar } from './MeterBar'

// Job states that mean the job is over, so the live view stops showing a row for it. A
// *failed* job is deliberately not here: a failure is the thing the reader came to look at.
const DONE_JOB_STATUSES: ReadonlySet<string> = new Set(['completed', 'killed'])

// Renders one campaign's live Status — the browser analog of what `vast exec cluster monitor` prints:
// phase, run-level progress within the current batch, batch counter, and each budget/stopping
// criterion. Purely presentational; the caller supplies the (polled) Status and, optionally, the
// (polled) live jobs listing.

export function StatusView({
  status,
  campaignId,
  jobs,
  hideLog = false,
  liveOnly = false,
  newest = true,
  showDetails = false,
  quotaCpu,
  postprocessed = false,
  onStopJob,
  stoppingJob,
}: {
  status: Status
  // The campaign this status belongs to. Passed in because the caller already knows it
  // and `status.campaign_id` does not: the controller fills that field, and a campaign
  // waiting for its image build has no controller yet — which used to leave the log
  // button off exactly the card that had nothing else to show. Falls back to the status
  // for any caller that only holds one.
  campaignId?: string
  jobs?: ListJobsResponse
  // The Launcher hides the campaign log — it's a launch confirmation, not a viewer;
  // the full log lives in Monitor.
  hideLog?: boolean
  // Monitor cares only about jobs still meaningful right now: it drops completed ones
  // from both the count summary and the jobs list (the Launcher lists everything).
  liveOnly?: boolean
  // The top card in Monitor's newest-first campaign list — see FailureBox. Defaults to
  // true so the Launcher and other single-campaign callers keep the box open.
  newest?: boolean
  // Offer the Details panel (what the campaign cost, how it ran). Off by default because it
  // only means anything once the campaign has finished and been postprocessed — the caller
  // holds the summary that says so (`hasResults`), and this view does not.
  showDetails?: boolean
  // Lane CPU capacity, for Details' "jobs in flight" estimate. Omitted → not shown.
  quotaCpu?: number | null
  // Whether the metric tables exist yet -- the Details panel re-queries when this flips, since a
  // campaign is postprocessed a few minutes after it finishes.
  postprocessed?: boolean
  // Offer each running job a Stop button. Omitted → no buttons, which is what the Launcher
  // wants: this view stays presentational and the caller owns the confirm + the mutation,
  // because it also owns the jobs query that has to be invalidated afterwards.
  onStopJob?: (job: JobSummary) => void
  // The job a stop is currently in flight for, so its button can disable itself rather than
  // inviting a second click at a job that is already going away.
  stoppingJob?: string | null
}) {
  const { runs, budget } = status
  const cid = campaignId ?? status.campaign_id
  const terminal = isTerminalPhase(status.phase)
  const counts = jobs?.counts
  const running = counts?.running ?? 0
  // Runs that delivered nothing, for the bar's dim red segment. See noResultRuns: the
  // live job count is the only source while the batch runs.
  const noResult = noResultRuns(status, counts)
  // Green is *successes*, not "produced a result". `runs.completed` counts every run
  // that delivered a result artifact, including the ones whose own verdict is a
  // failure — so painting `completed` green reported a campaign whose every trial
  // failed as fully passed. The two failure axes the status keeps apart stay apart in
  // the bar too (see RunProgress): a trial that ran and failed is solid red, a run
  // that delivered nothing is the dimmer red.
  const succeeded = Math.max(0, runs.completed - runs.failed)
  // The jobs list's expansion state, kept here rather than in JobsSection / JobRow
  // because both of those unmount underneath the reader: the section whenever the live
  // set momentarily empties (local runs are sequential, so between every pair of runs),
  // a row the instant its job completes and the `liveOnly` filter below drops it —
  // which threw away the log the reader was in the middle of. `expandedJobs` also feeds
  // that filter, so a job whose log is open survives its own completion until collapsed.
  const [jobsOpen, setJobsOpen] = useState(false)
  const [expandedJobs, setExpandedJobs] = useState<Set<string>>(() => new Set())
  const toggleJob = (jobName: string) =>
    setExpandedJobs((prev) => {
      const next = new Set(prev)
      if (!next.delete(jobName)) next.add(jobName)
      return next
    })
  // The jobs list mirrors what actually exists on the cluster — the same set `k9s`
  // shows: jobs that own a pod. A `waiting` job is the un-admitted Kueue backlog: it has
  // no pod, nothing distinguishes one queued job from the next, and there is no log to
  // expand (the row's only affordance). Whole batches sit there at launch, so listing
  // them buries the handful of jobs that are really doing something. The backlog is
  // reported by the `waiting N` counter instead, which is what makes it legible anyway.
  // `killed` is dropped from the live view for the same reason as `completed`: the job is
  // over. It used to linger as `running` — its `test.xml` is the thing that never arrives —
  // so the Jobs list kept a row, and a Stop button, on a job that was already dead.
  const shownJobs = jobs?.jobs.filter(
    (j) =>
      j.status !== 'waiting' &&
      (!liveOnly || !DONE_JOB_STATUSES.has(j.status) || expandedJobs.has(j.job_name)),
  )
  // Live-view count summary: every non-completed state that is present. `waiting` is
  // the only way the queued backlog shows up at all now that it has no rows.
  const liveCountText = [
    counts && counts.running > 0 ? `running ${counts.running}` : null,
    counts && counts.pending > 0 ? `pending ${counts.pending}` : null,
    counts && counts.waiting > 0 ? `waiting ${counts.waiting}` : null,
    counts && counts.failed > 0 ? `failed ${counts.failed}` : null,
    counts && counts.blocked > 0 ? `blocked ${counts.blocked}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  // This is a progress view, so a run is "done" whether it produced a result or
  // delivered none — both have reached a terminal state. Counting the resultless ones
  // toward the numerator is what makes `done/total` reach total when the batch is over,
  // and what keeps this number, the meter and the estimate the same number. Runs that
  // produced a *failing* result are already in `completed`.
  const done = finishedRuns(status, counts)
  const etaSeconds = estimateEtaSeconds(status, counts, terminal)
  return (
    <Stack spacing={1.5}>
      <Box>
        <Stack direction="row" justifyContent="space-between">
          {/* Just "runs". The batch counter that used to ride here -- `batch 2 (3 done)` --
              said what the batches bar directly below already shows, and this label sits
              above a bar measuring RUNS, so a batch number on it invited reading the bar as
              batch progress. */}
          <Typography variant="caption" color="text.secondary">
            runs
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {liveOnly
              ? liveCountText
                ? `${liveCountText} · `
                : ''
              : counts && (counts.running > 0 || counts.pending > 0 || counts.waiting > 0)
                ? `running ${counts.running} · pending ${counts.pending}` +
                  (counts.waiting > 0 ? ` · waiting ${counts.waiting}` : '') +
                  ' · '
                : ''}
            {done}/{runs.total}
            {runs.failed > 0 ? (
              <Box component="span" sx={{ color: 'error.main' }}>
                {' · '}
                {runs.failed} failed
              </Box>
            ) : null}
            {runs.no_result > 0 ? (
              <Box component="span" sx={{ color: 'error.main' }}>
                {' · '}
                {runs.no_result} no result
              </Box>
            ) : null}
            {etaSeconds != null
              ? ` · ~${formatDuration(etaSeconds)} left (≈ ${formatLocalClock(etaSeconds)})`
              : ''}
          </Typography>
        </Stack>
        {/* `running` stays last: MeterBar clamps the running offset but does not rescale,
            so a transient over-100% sum clips the final segment — and what is still
            running is the least final thing to lose. */}
        <MeterBar
          segments={
            runs.total > 0
              ? [
                  { fraction: succeeded / runs.total, color: 'success.main' },
                  { fraction: runs.failed / runs.total, color: 'error.main' },
                  { fraction: noResult / runs.total, color: 'error.main', opacity: 0.45 },
                  { fraction: running / runs.total, color: 'info.main', striped: true },
                ]
              : []
          }
        />
      </Box>

      {budget.map((b) => {
        // Only the batch budget converts into time from what we can observe; every other
        // criterion is measured in units nothing here can turn into a duration.
        const batchesEta = estimateBatchesEtaSeconds(status, counts, b, etaSeconds)
        const meta = (
          <>
            {b.current == null ? '—' : b.current} / {b.limit}
            {batchesEta != null
              ? ` · ~${formatDuration(batchesEta)} left (≈ ${formatLocalClock(batchesEta)})`
              : ''}
          </>
        )
        const bar = (
          <MeterBar
            height={10}
            fraction={b.current == null || b.limit <= 0 ? 0 : b.current / b.limit}
            color="secondary.main"
          />
        )
        // The batches bar, and only it, opens onto the objective's trajectory. Keyed on
        // `kind` rather than on `label`, which is the user's own objective or metric name for
        // every other criterion — a campaign that happens to name a metric "batches" must not
        // grow a chart of a search it is not running.
        if (cid && b.kind === 'batches') {
          return (
            <ObjectiveSection
              key={b.label}
              campaignId={cid}
              label={b.label + (b.done ? ' ✓' : '')}
              meta={meta}
              bar={bar}
              batchesDone={status.batches_done}
            />
          )
        }
        return (
        <Box key={b.label}>
          <Stack direction="row" justifyContent="space-between">
            <Typography variant="caption" color="text.secondary">
              {b.label}
              {b.done ? ' ✓' : ''}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {meta}
            </Typography>
          </Stack>
          {bar}
        </Box>
        )
      })}

      {status.best_objective != null ? (
        <Typography variant="caption" color="text.secondary">
          best objective: {status.best_objective}
        </Typography>
      ) : null}
      {status.stop ? (
        <Typography variant="caption" color="text.secondary">
          stop: {JSON.stringify(status.stop)}
        </Typography>
      ) : null}
      {status.error ? <FailureBox error={status.error} defaultOpen={newest} /> : null}
      {/* Nothing to show means no affordance: a "Show jobs (0)" button opens onto an
          empty list, so it is only noise. The section may therefore come and go as the
          live set empties and refills between runs, which is why neither piece of its
          expansion state lives inside it — see jobsOpen / expandedJobs. */}
      {cid && shownJobs && shownJobs.length > 0 ? (
        <JobsSection
          campaignId={cid}
          jobs={shownJobs}
          open={jobsOpen}
          onToggleOpen={() => setJobsOpen((o) => !o)}
          expanded={expandedJobs}
          onToggle={toggleJob}
          onStopJob={onStopJob}
          stoppingJob={stoppingJob}
        />
      ) : null}
      {cid && showDetails ? (
        <DetailsBox
          campaignId={cid}
          quotaCpu={quotaCpu}
          postprocessed={postprocessed}
        />
      ) : null}
      {cid && !hideLog ? <CampaignLog campaignId={cid} /> : null}
    </Stack>
  )
}

// -- the search's objective over its batches ---------------------------------

/** The batches budget bar, made foldable: the same bar as before, over a chart of how the
 *  objective has moved.
 *
 *  Closed by default and fetched only while open. That gating is the whole reason this can sit on
 *  a campaign card at all: the Monitor renders every campaign in the list, so anything a card does
 *  unconditionally is paid for by the whole page — see `useDetails`, which is closed by default for
 *  exactly this reason.
 *
 *  `batchesDone` is in the query key rather than a refetch interval. It is an integer already on
 *  the polled status, and it is precisely the thing whose change makes this answer stale — so the
 *  series is re-read once per completed batch (minutes apart) instead of on a timer that would
 *  mostly re-fetch an unchanged answer.
 */
function ObjectiveSection({
  campaignId,
  label,
  meta,
  bar,
  batchesDone,
}: {
  campaignId: string
  label: string
  meta: ReactNode
  bar: ReactNode
  batchesDone: number
}) {
  const [open, setOpen] = useState(false)
  const history = useQuery({
    queryKey: ['search-history', campaignId, batchesDone],
    queryFn: () => robovast.getSearchHistory(campaignId),
    enabled: open,
    retry: false,
    // Within one batch count the answer cannot change, so it is read once per round.
    staleTime: Infinity,
  })
  return (
    <CollapsibleBox
      // `row`, not `card`: this bar sits in a stack of budget bars, and giving one of them a
      // border and a tinted header would read as a different KIND of thing rather than as the
      // one that opens. The chevron and the hover tint are the affordance.
      variant="row"
      flush
      title={label}
      meta={meta}
      subheader={bar}
      open={open}
      onToggle={() => setOpen((o) => !o)}
    >
      <Box sx={{ p: 1 }}>
        {history.isLoading ? (
          <Typography variant="caption" color="text.secondary">
            reading the search's rounds…
          </Typography>
        ) : history.isError ? (
          <Typography variant="caption" color="text.secondary">
            no objective history for this campaign ({(history.error as Error)?.message})
          </Typography>
        ) : history.data ? (
          <BatchObjectiveChart history={history.data} />
        ) : null}
      </Box>
    </CollapsibleBox>
  )
}

// -- jobs (live) ------------------------------------------------------------

const JOB_STATUS_COLOR: Record<string, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  running: 'info',
  pending: 'warning',
  completed: 'success',
  failed: 'error',
  // Neutral, not red: somebody chose this. Painting it as an error would put a deliberate
  // intervention next to the trials that actually failed.
  killed: 'default',
  blocked: 'error',
}

// The campaign's current-batch jobs. Collapsed by default; each job row expands its
// own live log (running pod on the cluster / live system.log locally). Capped so a
// huge fan-out stays responsive.
const JOBS_RENDER_CAP = 100

function JobsSection({
  campaignId,
  jobs,
  open,
  onToggleOpen,
  expanded,
  onToggle,
  onStopJob,
  stoppingJob,
}: {
  campaignId: string
  jobs: JobSummary[]
  // Both halves of the expansion state are owned by StatusView, because this section is
  // unmounted whenever the live set empties: `open` is whether the list is unfolded,
  // `expanded` the job names whose log is unfolded within it.
  open: boolean
  onToggleOpen: () => void
  expanded: Set<string>
  onToggle: (jobName: string) => void
  onStopJob?: (job: JobSummary) => void
  stoppingJob?: string | null
}) {
  const shown = jobs.slice(0, JOBS_RENDER_CAP)
  return (
    <CollapsibleBox title="Jobs" meta={jobs.length} open={open} onToggle={onToggleOpen}>
      {/* Flat rows separated by hairlines rather than a bordered card each: the section is
          already a card, and a box per job turned a 20-job batch into 20 nested frames. */}
      <Stack divider={<Box sx={{ borderTop: 1, borderColor: 'divider' }} />}>
        {shown.map((job) => (
          <JobRow
            key={job.job_name}
            campaignId={campaignId}
            job={job}
            open={expanded.has(job.job_name)}
            onToggle={() => onToggle(job.job_name)}
            onStopJob={onStopJob}
            stopping={stoppingJob === job.job_name}
          />
        ))}
        {jobs.length > shown.length ? (
          <Typography variant="caption" color="text.secondary" sx={{ px: 1, py: 0.5 }}>
            … {jobs.length - shown.length} more not shown
          </Typography>
        ) : null}
      </Stack>
    </CollapsibleBox>
  )
}

function JobRow({
  campaignId,
  job,
  open,
  onToggle,
  onStopJob,
  stopping,
}: {
  campaignId: string
  job: JobSummary
  open: boolean
  onToggle: () => void
  onStopJob?: (job: JobSummary) => void
  stopping?: boolean
}) {
  // Offered only on a `running` job — the same rule the service enforces, so the UI never
  // shows a button the server would refuse. A pending or queued job has not started, and a
  // blocked one has a cause that deleting it does not fix.
  const canStop = Boolean(onStopJob) && job.status === 'running'
  return (
    <CollapsibleBox
      variant="row"
      open={open}
      onToggle={onToggle}
      actions={
        canStop ? (
          <Tooltip title="Stop this job. The campaign continues; this run is recorded as killed.">
            <IconButton
              size="small"
              color="error"
              aria-label={`Stop job ${job.display_name || job.job_name}`}
              disabled={stopping}
              onClick={() => onStopJob?.(job)}
              sx={{ p: 0.25 }}
            >
              <StopRoundedIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null
      }
      leading={
        <Chip
          label={job.status}
          size="small"
          color={JOB_STATUS_COLOR[job.status] ?? 'default'}
          variant="outlined"
          sx={{ height: 18, '& .MuiChip-label': { px: 0.75, fontSize: '0.65rem' } }}
        />
      }
      title={job.display_name || job.job_name}
      // Why a job is stuck — e.g. a Kubernetes ImagePullBackOff reason + message — so a job
      // that can never start is legible without opening its (empty) log.
      note={
        job.detail ? (
          <Typography
            variant="caption"
            sx={{
              display: 'block',
              color: 'error.main',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {job.detail}
          </Typography>
        ) : null
      }
    >
      <LogPanel
        resetKey={`${campaignId}/${job.job_name}`}
        streamUrl={robovast.jobLogStreamUrl(campaignId, job.job_name)}
      />
    </CollapsibleBox>
  )
}

// Multi-container job logs arrive with each line tagged `[container] …` (merged
// server-side). Color only the `[container]` prefix per container; the rest of the
// line keeps the default text color. Lines without a tag render unchanged.
//
// The colours come from the container names this text actually holds, so two of them never
// share one (see containerColorer) -- which the bare hash did not guarantee.
function renderLogLines(text: string) {
  const lines = text.split('\n')
  const tags = lines.map((line) => /^(\[[^\]]+\]) ?/.exec(line))
  const color = containerColorer(tags.flatMap((m) => (m ? [m[1].slice(1, -1)] : [])))
  return lines.map((line, i) => {
    const m = tags[i]
    const nl = i > 0 ? '\n' : ''
    if (!m) return <span key={i}>{nl + line}</span>
    const prefix = m[0]
    const rest = line.slice(prefix.length)
    return (
      <span key={i}>
        {nl}
        <span style={{ color: color(m[1].slice(1, -1)) }}>{prefix}</span>
        {rest}
      </span>
    )
  })
}

/** How the *server* ended the stream, as opposed to how the transport is doing. */
type LogEnd = 'eof' | 'error' | null

// How far from the bottom still counts as "at the bottom", in px. Not zero: a sub-pixel
// scroll height (fractional line metrics, a zoomed browser) leaves a fraction of a pixel of
// slack that would read as the reader having deliberately scrolled away.
const BOTTOM_SLACK_PX = 24

// Streams a log live over Server-Sent Events (see robovast.*StreamUrl). Each delta is
// appended; the browser's own reconnect resends Last-Event-ID so the server resumes from
// the exact byte offset (no gap, no dupe), and useLiveStream covers what that reconnect
// does not — a stream the browser gave up on, and a socket that died without saying so
// while the tab was in the background. So the panel is never a silently-frozen tail: a
// blip shows `reconnecting…` and heals itself; a server-side application error (e.g. pod
// gone, no durable copy) shows verbatim; a terminal log ends cleanly on `eof`. `resetKey`
// restarts the stream when the source changes; callers gate visibility by mounting.
function LogPanel({ resetKey, streamUrl }: { resetKey: string; streamUrl: string }) {
  const [text, setText] = useState('')
  const [end, setEnd] = useState<LogEnd>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const preRef = useRef<HTMLPreElement>(null)
  // The tail follows the newest line only while the reader is *at* the newest line. Starts true
  // so an opening panel shows the end of the log, which is what a tail is for.
  const [following, setFollowing] = useState(true)

  const { state, finish, generation } = useLiveStream(streamUrl, {
    resetKey,
    onMessage: (e) => {
      try {
        const delta = JSON.parse(e.data) as string
        if (delta) setText((t) => t + delta)
      } catch {
        /* malformed frame — ignore rather than break the tail */
      }
    },
    events: {
      // Application error the server chose to surface (pod gone, upload missing, …).
      streamerror: (e) => {
        try {
          setErrorMsg(JSON.parse(e.data) as string)
        } catch {
          setErrorMsg('log stream error')
        }
        setEnd('error')
        finish()
      },
      // Terminal log — nothing more will ever be written. Closing it deliberately also
      // tells the watchdog not to treat the silence that follows as a fault.
      eof: () => {
        setEnd((e) => (e === 'error' ? e : 'eof'))
        finish()
      },
    },
  })

  // A connection this component opened starts the log from byte zero (Last-Event-ID is the
  // browser's to send, not ours), so the text it is about to re-send has to go first — or
  // the whole log would appear twice. The browser's own reconnect does not bump the
  // generation and correctly keeps what is on screen.
  useEffect(() => {
    setText('')
    setEnd(null)
    setErrorMsg(null)
    // A different log (or one restarted from byte zero) is a new thing to read from its end.
    setFollowing(true)
  }, [generation, resetKey, streamUrl])

  const lines = useMemo(() => (text ? renderLogLines(text) : null), [text])

  // Footer status shown under the log body: nothing while healthily streaming, an
  // explicit note while reconnecting / errored / (when empty) connecting or ended.
  // An open stream with nothing in it is not loading — the connection is up and the
  // source has produced no bytes (a job whose containers have not started writing yet;
  // PodLogTail swallows the API's 400 for a container with no log). Saying `loading…`
  // there promised output that nothing was on its way to deliver.
  const footer =
    end === 'error'
      ? `stream error: ${errorMsg ?? 'unknown'}`
      : end !== 'eof' && (state === 'reconnecting' || state === 'closed')
        ? 'reconnecting…'
        : !lines
          ? end === 'eof'
            ? '(no log)'
            : state === 'open'
              ? '(no output yet)'
              : 'loading…'
          : null

  // Stick to the bottom only while following, and never out from under a selection: a scroll
  // mid-drag loses the anchor, which is what made a live log impossible to copy from. Holding a
  // selection does not clear `following` either, so dropping it resumes the tail by itself —
  // same contract as RunLogView's follow mode.
  //
  // `footer` is a dependency as much as the text is: `reconnecting…` appearing under the last
  // line grows the body exactly like one more log line.
  useEffect(() => {
    const el = preRef.current
    if (!el || !following) return
    const sel = window.getSelection()
    const held = !!sel && !sel.isCollapsed && !!sel.anchorNode && el.contains(sel.anchorNode)
    if (held) return
    el.scrollTop = el.scrollHeight
  }, [text, footer, following])

  // Appending below the viewport moves nothing and fires no scroll event, so every scroll that
  // arrives here is the reader's — or this panel's own jump, which lands at the bottom and so
  // correctly re-arms following.
  const onScroll = () => {
    const el = preRef.current
    if (!el) return
    setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK_PX)
  }

  return (
    <Box sx={{ position: 'relative' }}>
      <Box
        component="pre"
        ref={preRef}
        onScroll={onScroll}
        sx={{
          m: 0,
          px: 1,
          py: 0.75,
          // Darker than the card it sits in, and with no border of its own: it is always
          // mounted inside a CollapsibleBox body, which supplies the frame.
          bgcolor: 'background.default',
          color: 'text.primary',
          fontFamily: 'monospace',
          fontSize: '0.72rem',
          whiteSpace: 'pre-wrap',
          overflowX: 'auto',
          maxHeight: 320,
          overflowY: 'auto',
        }}
      >
        {lines}
        {footer ? (
          <Box
            component="span"
            sx={{
              display: 'block',
              color: end === 'error' ? 'error.main' : 'text.secondary',
              opacity: 0.85,
            }}
          >
            {lines ? '\n' : ''}
            {footer}
          </Box>
        ) : null}
      </Box>

      {/* The only visible sign that the tail was paused, and the way back. Shown only once the
          reader has actually left the bottom of a log that has something in it — a panel that
          fits its log entirely never scrolls, so it never detaches and never grows a button. */}
      {!following && lines ? (
        <Tooltip title="Jump to the latest line and resume following">
          <Fab
            size="small"
            color="primary"
            aria-label="jump to the latest log line"
            onClick={() => {
              const el = preRef.current
              if (el) el.scrollTop = el.scrollHeight
              setFollowing(true)
            }}
            sx={{ position: 'absolute', right: 14, bottom: 10, zIndex: 2 }}
          >
            <ArrowDownwardRoundedIcon fontSize="small" />
          </Fab>
        </Tooltip>
      ) : null}
    </Box>
  )
}

// Live unified infrastructure log for one campaign (variation + run + postprocessing
// phases, divider-separated), streamed over SSE. Collapsed by default.
export function CampaignLog({ campaignId }: { campaignId: string }) {
  const [open, setOpen] = useState(false)
  return (
    <CollapsibleBox title="Log" open={open} onToggle={() => setOpen((o) => !o)}>
      <LogPanel resetKey={campaignId} streamUrl={robovast.campaignLogStreamUrl(campaignId)} />
    </CollapsibleBox>
  )
}

// One backend error string, shown verbatim. These are multi-line — an exception message plus a
// traceback tail — and inline in an Alert or a Typography the browser collapses every newline,
// running the whole trace into one paragraph. Anywhere such a string is printed goes through here.
export function ErrorText({ children }: { children: ReactNode }) {
  return (
    <Box
      component="span"
      sx={{
        display: 'block',
        mt: 0.5,
        fontFamily: 'monospace',
        fontSize: '0.75rem',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        // A long trace should scroll inside the box rather than push the campaign card open.
        maxHeight: 200,
        overflowY: 'auto',
      }}
    >
      {children}
    </Box>
  )
}

// The controller's failure reason (message + traceback tail) — what you'd otherwise have to dig
// out of the pod log. The header stays visible; the traceback below it opens by itself only for
// `defaultOpen` callers (the newest campaign) — same collapse rule as StepFailure in Monitor.tsx,
// so an older, finished campaign doesn't add its own wall of stack frames to the list.
export function FailureBox({ error, defaultOpen = true }: { error: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <CollapsibleBox
      title="Failure"
      tone="error"
      open={open}
      onToggle={() => setOpen((o) => !o)}
    >
      <Box
        component="pre"
        sx={{
          m: 0,
          px: 1,
          py: 0.75,
          bgcolor: 'background.default',
          color: 'text.primary',
          fontFamily: 'monospace',
          fontSize: '0.75rem',
          whiteSpace: 'pre-wrap',
          overflowX: 'auto',
          maxHeight: 240,
          overflowY: 'auto',
        }}
      >
        {error}
      </Box>
    </CollapsibleBox>
  )
}
