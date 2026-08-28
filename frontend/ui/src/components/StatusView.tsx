import { useState, type ReactNode } from 'react'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import {
  robovast,
  isTerminalPhase,
  type JobCounts,
  type JobSummary,
  type ListJobsResponse,
  readUploadProgress,
  type Status,
  type UploadProgress,
} from '@/lib/robovastClient'
import {
  batchesBudget,
  estimateBatchesEtaSeconds,
  isBatchesBudget,
  estimateEtaSeconds,
  finishedRuns,
  noResultRuns,
} from '@/lib/eta'
import { formatBytes, formatDuration } from '@/lib/format'
import { useQuery } from '@tanstack/react-query'
import { formatLocalClock } from '@/lib/time'
import { BatchObjectiveChart } from './BatchObjectiveChart'
import { CollapsibleBox } from './CollapsibleBox'
import { DetailsBox } from './DetailsBox'
import { LogPanel } from './LogPanel'
import { MeterBar } from './MeterBar'

// The upload-to-share bar, shown only while the campaign is in the `sharing` phase.
//
// The bar measures the CAMPAIGN bytes fed into the archive, not the bytes on the wire:
// the archive is gzipped on the fly, so its compressed length is unknown until the last
// byte and there is no wire denominator to divide by. `sent` is reported beside it as
// text, which is also why the two numbers disagree — that difference is the compression
// ratio, not an error.
//
// With no total (a provider or lane that cannot say), the bar goes indeterminate rather
// than showing a made-up 0%: a bar pinned at zero through a multi-hour upload is the
// exact failure this replaced.
function UploadSection({ upload }: { upload: UploadProgress }) {
  const { percent, sourceDone, sourceTotal, sent, rate } = upload
  const meta = [
    sourceTotal > 0 ? `${formatBytes(sourceDone)} / ${formatBytes(sourceTotal)}` : null,
    `${formatBytes(sent)} sent`,
    rate != null && rate > 0 ? `${formatBytes(rate)}/s` : null,
    percent != null && rate != null && rate > 0 && sourceTotal > sourceDone
      ? `~${formatDuration(estimateUploadEtaSeconds(upload))} left`
      : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <Box>
      <Stack direction="row" justifyContent="space-between">
        <Typography variant="caption" color="text.secondary">
          upload to share
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {meta}
        </Typography>
      </Stack>
      {percent == null ? (
        <MeterBar segments={[{ fraction: 1, color: 'info.main', striped: true }]} />
      ) : (
        <MeterBar fraction={percent / 100} color="info.main" text={`${percent.toFixed(1)}%`} />
      )}
    </Box>
  )
}

// Time left on the upload, from the rate the wire is actually moving at. The remaining
// SOURCE bytes are scaled by the compression ratio observed so far (sent/done), because
// `rate` counts compressed bytes and the remainder is counted uncompressed — dividing one
// by the other directly would over-estimate the wait by exactly that ratio.
function estimateUploadEtaSeconds(upload: UploadProgress): number {
  const { sourceDone, sourceTotal, sent, rate } = upload
  const ratio = sourceDone > 0 ? sent / sourceDone : 1
  return Math.max(0, ((sourceTotal - sourceDone) * ratio) / (rate ?? 1))
}

// Job states that mean the job is over, so the live view stops showing a row for it. A
// *failed* job is deliberately not here: a failure is the thing the reader came to look at.
const DONE_JOB_STATUSES: ReadonlySet<string> = new Set(['completed', 'killed'])

// Renders one campaign's live Status — the browser analog of what `vast cluster monitor` prints:
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
  // waiting for its image build has no controller yet — which would otherwise leave the log
  // button off exactly the card that has nothing else to show. Falls back to the status
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
  // shows: jobs that own a pod. A `waiting` job is the un-admitted backlog: it has
  // no pod, nothing distinguishes one queued job from the next, and there is no log to
  // expand (the row's only affordance). Whole batches sit there at launch, so listing
  // them buries the handful of jobs that are really doing something. The backlog is
  // reported by the `waiting N` counter instead, which is what makes it legible anyway.
  // `killed` is dropped from the live view for the same reason as `completed`: the job is
  // over. Kept, it would show as `running` — its `test.xml` never arrives —
  // so the Jobs list would carry a row, and a Stop button, on a job that is already dead.
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
  // Upload-to-share is the one phase with real progress that the run meter cannot show:
  // the runs are over and their bar is frozen, while gigabytes move to somebody else's
  // storage. Rendered first because during `sharing` it is the only thing happening.
  const upload = status.phase === 'sharing' ? readUploadProgress(status) : null
  return (
    <Stack spacing={1.5}>
      {upload ? <UploadSection upload={upload} /> : null}
      <Box>
        <Stack direction="row" justifyContent="space-between">
          {/* Just "runs", and no batch counter -- `batch 2 (3 done)` -- riding along: it
              says what the batches bar directly below already shows, and this label sits
              above a bar measuring RUNS, so a batch number on it invites reading the bar as
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

      {/* The rounds a search has run, and the objective they moved. Rendered for every
          search rather than off a budget row: a `batches` criterion BOUNDS the rounds, it
          does not create them, so a search bounded by runs or time has both just the same.
          Hanging this on the budget row is what left those campaigns with no batch counter,
          no estimate and no objective chart at all. It sits above the criteria because
          rounds are not one of them. */}
      {cid && status.mode === 'search' ? (
        <ObjectiveSection
          campaignId={cid}
          status={status}
          counts={counts}
          runsEta={etaSeconds}
        />
      ) : null}

      {budget.filter((b) => !isBatchesBudget(b)).map((b) => (
        // Every criterion except the batch counter, which the rounds section above owns.
        // These are measured in units nothing here can turn into a duration, so no estimate.
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

/** A search's rounds, foldable over a chart of how its objective has moved.
 *
 *  Owns the whole presentation of "rounds", because how they read depends on whether anything
 *  bounds them and that is one decision, not two: with a `batches` criterion this is the bar it
 *  always was — `3 / 6`, a meter, and an estimate — and without one it is the same row carrying
 *  only the count, no meter and no estimate. Not a fallback: an unbounded search HAS no limit,
 *  and drawing a meter would need a denominator the campaign never declared.
 *
 *  Closed by default and fetched only while open. That gating is the whole reason this can sit on
 *  a campaign card at all: the Monitor renders every campaign in the list, so anything a card does
 *  unconditionally is paid for by the whole page — see `useDetails`, which is closed by default for
 *  exactly this reason.
 *
 *  `batches_done` is in the query key rather than a refetch interval. It is an integer already on
 *  the polled status, and it is precisely the thing whose change makes this answer stale — so the
 *  series is re-read once per completed batch (minutes apart) instead of on a timer that would
 *  mostly re-fetch an unchanged answer.
 */
function ObjectiveSection({
  campaignId,
  status,
  counts,
  runsEta,
}: {
  campaignId: string
  status: Status
  counts?: JobCounts
  runsEta: number | null
}) {
  const batchesDone = status.batches_done
  // The criterion bounding the rounds, when one was declared. Only the batches budget converts
  // into time from what we can observe, which is why the estimate lives on this row alone.
  const bound = batchesBudget(status)
  const batchesEta = bound ? estimateBatchesEtaSeconds(status, counts, bound, runsEta) : null
  const label = bound ? bound.label + (bound.done ? ' ✓' : '') : 'batches'
  const meta = bound ? (
    <>
      {bound.current == null ? '—' : bound.current} / {bound.limit}
      {batchesEta != null
        ? ` · ~${formatDuration(batchesEta)} left (≈ ${formatLocalClock(batchesEta)})`
        : ''}
    </>
  ) : (
    // The count alone, and said as a count: `4 done` cannot be misread as progress toward a
    // total the way a bare `4` sitting where `4 / 6` usually sits could be.
    `${batchesDone} done`
  )
  const bar = bound ? (
    <MeterBar
      height={10}
      fraction={bound.current == null || bound.limit <= 0 ? 0 : bound.current / bound.limit}
      color="secondary.main"
    />
  ) : null
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
