import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Collapse from '@mui/material/Collapse'
import IconButton from '@mui/material/IconButton'
import KeyboardArrowDownRoundedIcon from '@mui/icons-material/KeyboardArrowDownRounded'
import KeyboardArrowUpRoundedIcon from '@mui/icons-material/KeyboardArrowUpRounded'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import {
  robovast,
  isTerminalPhase,
  type JobSummary,
  type ListJobsResponse,
  type Status,
} from '@/lib/robovastClient'
import { formatDuration } from '@/lib/format'
import { useLiveStream } from '@/lib/liveStream'
import { formatLocalClock } from '@/lib/time'
import { containerColorer } from './containerColor'
import { MeterBar } from './MeterBar'

// Renders one campaign's live Status — the browser analog of what `vast exec cluster monitor` prints:
// phase, run-level progress within the current batch, batch counter, and each budget/stopping
// criterion. Purely presentational; the caller supplies the (polled) Status and, optionally, the
// (polled) live jobs listing.

// Estimated seconds until the current batch completes, or null when it can't be
// stated soundly. `runs` counts only the current batch and resets each batch, and
// the campaign's `started_at` equals the batch start only for batch 0 — so we
// estimate exactly when: not terminal, on the first batch, and ≥1 run has finished
// (needed to have any per-run rate). No fabricated number otherwise.
function estimateEtaSeconds(
  status: Status,
  startedAt: string | null | undefined,
  terminal: boolean,
): number | null {
  const { runs } = status
  if (terminal || !startedAt || status.batch !== 0) return null
  if (runs.total <= 0 || runs.completed <= 0) return null
  const start = Date.parse(startedAt)
  if (Number.isNaN(start)) return null
  const elapsed = (Date.now() - start) / 1000
  if (!(elapsed > 0)) return null
  const perRun = elapsed / runs.completed
  return (runs.total - runs.completed) * perRun
}

export function StatusView({
  status,
  campaignId,
  jobs,
  startedAt,
  hideLog = false,
  liveOnly = false,
  newest = true,
}: {
  status: Status
  // The campaign this status belongs to. Passed in because the caller already knows it
  // and `status.campaign_id` does not: the controller fills that field, and a campaign
  // waiting for its image build has no controller yet — which used to leave the log
  // button off exactly the card that had nothing else to show. Falls back to the status
  // for any caller that only holds one.
  campaignId?: string
  jobs?: ListJobsResponse
  // Campaign start time (CampaignSummary.started_at), used to estimate the ETA.
  // Optional — the ETA simply isn't shown when it's absent.
  startedAt?: string | null
  // The Launcher hides the campaign log — it's a launch confirmation, not a viewer;
  // the full log lives in Monitor.
  hideLog?: boolean
  // Monitor cares only about jobs still meaningful right now: it drops completed ones
  // from both the count summary and the jobs list (the Launcher lists everything).
  liveOnly?: boolean
  // The top card in Monitor's newest-first campaign list — see FailureBox. Defaults to
  // true so the Launcher and other single-campaign callers keep the box open.
  newest?: boolean
}) {
  const { runs, budget } = status
  const cid = campaignId ?? status.campaign_id
  const terminal = isTerminalPhase(status.phase)
  const counts = jobs?.counts
  const running = counts?.running ?? 0
  // Runs that delivered nothing, for the bar's dim red segment: prefer the live job
  // count so a failure shows in real time (the status counters only settle once the
  // batch reaches a terminal state). runs.no_result is the fallback.
  const noResult = counts?.failed ?? runs.no_result
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
  const shownJobs = jobs?.jobs.filter(
    (j) =>
      j.status !== 'waiting' &&
      (!liveOnly || j.status !== 'completed' || expandedJobs.has(j.job_name)),
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
  // delivered none — both have reached a terminal state. Count the resultless ones
  // toward the numerator (and the bar) so `done/total` reaches total when the batch is
  // over. Runs that produced a *failing* result are already in `completed`.
  const done = runs.completed + runs.no_result
  const etaSeconds = estimateEtaSeconds(status, startedAt, terminal)
  // Only a search campaign has multiple batches; a plain batch run always has one,
  // so the "batch N" counter is noise there and is shown for search only.
  const isSearch = status.mode === 'search'
  return (
    <Stack spacing={1.5}>
      <Box>
        <Stack direction="row" justifyContent="space-between">
          <Typography variant="caption" color="text.secondary">
            runs
            {isSearch
              ? ` · batch ${status.batch}${status.batches_done ? ` (${status.batches_done} done)` : ''}`
              : ''}
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

      {budget.map((b) => (
        <Box key={b.label}>
          <Stack direction="row" justifyContent="space-between">
            <Typography variant="caption" color="text.secondary">
              {b.label}
              {b.done ? ' ✓' : ''}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {b.current == null ? '—' : b.current} / {b.limit}
            </Typography>
          </Stack>
          <MeterBar
            height={10}
            fraction={b.current == null || b.limit <= 0 ? 0 : b.current / b.limit}
            color="secondary.main"
          />
        </Box>
      ))}

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
        />
      ) : null}
      {cid && !hideLog ? <CampaignLog campaignId={cid} /> : null}
    </Stack>
  )
}

// -- jobs (live) ------------------------------------------------------------

const JOB_STATUS_COLOR: Record<string, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  running: 'info',
  pending: 'warning',
  completed: 'success',
  failed: 'error',
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
}) {
  const shown = jobs.slice(0, JOBS_RENDER_CAP)
  return (
    <Box>
      <Button size="small" variant="text" onClick={onToggleOpen}>
        {open ? 'Hide jobs' : `Show jobs (${jobs.length})`}
      </Button>
      {open ? (
        <Stack spacing={0.5} sx={{ mt: 0.5 }}>
          {shown.map((job) => (
            <JobRow
              key={job.job_name}
              campaignId={campaignId}
              job={job}
              open={expanded.has(job.job_name)}
              onToggle={() => onToggle(job.job_name)}
            />
          ))}
          {jobs.length > shown.length ? (
            <Typography variant="caption" color="text.secondary">
              … {jobs.length - shown.length} more not shown
            </Typography>
          ) : null}
        </Stack>
      ) : null}
    </Box>
  )
}

function JobRow({
  campaignId,
  job,
  open,
  onToggle,
}: {
  campaignId: string
  job: JobSummary
  open: boolean
  onToggle: () => void
}) {
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center">
        <Chip
          label={job.status}
          size="small"
          color={JOB_STATUS_COLOR[job.status] ?? 'default'}
          variant="outlined"
        />
        <Button
          size="small"
          variant="text"
          onClick={onToggle}
          sx={{ textTransform: 'none', justifyContent: 'flex-start', minWidth: 0 }}
        >
          {job.display_name || job.job_name}
        </Button>
      </Stack>
      {/* Why a job is stuck — e.g. a Kubernetes ImagePullBackOff reason + message —
          so a job that can never start is legible instead of silently pending. */}
      {job.detail ? (
        <Typography
          variant="caption"
          sx={{
            display: 'block',
            color: 'error.main',
            pl: 0.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {job.detail}
        </Typography>
      ) : null}
      {open ? (
        <LogPanel
          resetKey={`${campaignId}/${job.job_name}`}
          streamUrl={robovast.jobLogStreamUrl(campaignId, job.job_name)}
        />
      ) : null}
    </Box>
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
  }, [generation, resetKey, streamUrl])

  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight
  }, [text])

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

  return (
    <Box
      component="pre"
      ref={preRef}
      sx={{
        m: 0,
        px: 1,
        py: 0.75,
        bgcolor: 'background.paper',
        color: 'text.primary',
        border: 1,
        borderColor: 'divider',
        borderRadius: 1,
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
  )
}

// Live unified infrastructure log for one campaign (variation + run + postprocessing
// phases, divider-separated), streamed over SSE. Collapsed by default.
export function CampaignLog({ campaignId }: { campaignId: string }) {
  const [open, setOpen] = useState(false)
  return (
    <Box>
      <Button size="small" variant="text" onClick={() => setOpen((o) => !o)}>
        {open ? 'Hide log' : 'Show log'}
      </Button>
      {open ? (
        <LogPanel resetKey={campaignId} streamUrl={robovast.campaignLogStreamUrl(campaignId)} />
      ) : null}
    </Box>
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
    <Box
      sx={{
        border: 1,
        borderColor: 'error.main',
        borderRadius: 1,
        bgcolor: 'error.main',
        color: 'error.contrastText',
        cursor: open ? 'default' : 'pointer',
      }}
      onClick={() => !open && setOpen(true)}
    >
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Typography
          variant="caption"
          component="span"
          onClick={(e) => {
            e.stopPropagation()
            setOpen((o) => !o)
          }}
          sx={{ px: 1, py: 0.5, fontWeight: 600, cursor: 'pointer', userSelect: 'none' }}
        >
          failure
        </Typography>
        <IconButton
          size="small"
          aria-label={open ? 'Hide failure details' : 'Show failure details'}
          aria-expanded={open}
          onClick={(e) => {
            e.stopPropagation()
            setOpen((o) => !o)
          }}
          sx={{ color: 'inherit', mr: 0.5 }}
        >
          {open ? (
            <KeyboardArrowUpRoundedIcon fontSize="small" />
          ) : (
            <KeyboardArrowDownRoundedIcon fontSize="small" />
          )}
        </IconButton>
      </Stack>
      <Collapse in={open} unmountOnExit>
        <Box
          component="pre"
          sx={{
            m: 0,
            px: 1,
            py: 0.75,
            bgcolor: 'background.paper',
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
      </Collapse>
    </Box>
  )
}
