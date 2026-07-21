import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import {
  robovast,
  type JobSummary,
  type ListJobsResponse,
  type LogChunk,
  type Status,
} from '@/lib/robovastClient'
import { formatDuration } from '@/lib/format'
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
  jobs,
  startedAt,
  hideLog = false,
  liveOnly = false,
}: {
  status: Status
  jobs?: ListJobsResponse
  // Campaign start time (CampaignSummary.started_at), used to estimate the ETA.
  // Optional — the ETA simply isn't shown when it's absent.
  startedAt?: string | null
  // The Launcher hides the campaign log — it's a launch confirmation, not a viewer;
  // the full log lives in Monitor.
  hideLog?: boolean
  // Monitor cares only about jobs still meaningful right now: show running, failed and
  // blocked jobs (failed ones linger in kubernetes only briefly; blocked ones can't
  // start), dropping pending and completed from both the count summary and jobs list.
  liveOnly?: boolean
}) {
  const { runs, budget } = status
  const terminal = ['finished', 'failed', 'stopped', 'error'].includes(status.phase)
  const counts = jobs?.counts
  const running = counts?.running ?? 0
  const shownJobs = liveOnly
    ? jobs?.jobs.filter(
        (j) => j.status === 'running' || j.status === 'failed' || j.status === 'blocked',
      )
    : jobs?.jobs
  // Live-view count summary: running + failed + blocked, whichever are present.
  const liveCountText = [
    counts && counts.running > 0 ? `running ${counts.running}` : null,
    counts && counts.failed > 0 ? `failed ${counts.failed}` : null,
    counts && counts.blocked > 0 ? `blocked ${counts.blocked}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  // This is a progress view, so a run is "done" whether it produced a result or
  // failed to — both have reached a terminal state. Count failed runs toward the
  // numerator (and the bar) so `done/total` reaches total when the batch is over;
  // the failure count is still called out separately below.
  const done = runs.completed + runs.failed
  const etaSeconds = estimateEtaSeconds(status, startedAt, terminal)
  // Only a search campaign has multiple batches; a plain batch run always has one,
  // so the "batch N" counter is noise there and is shown for search only.
  const isSearch = status.mode === 'search'
  // The lighter buffer segment is what's currently running, layered over the solid
  // segment of finished runs.
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
              : counts && (counts.running > 0 || counts.pending > 0)
                ? `running ${counts.running} · pending ${counts.pending} · `
                : ''}
            {done}/{runs.total}
            {runs.failed > 0 ? (
              <Box component="span" sx={{ color: 'error.main' }}>
                {' · '}
                {runs.failed} failed
              </Box>
            ) : null}
            {etaSeconds != null ? ` · ~${formatDuration(etaSeconds)} left` : ''}
          </Typography>
        </Stack>
        <MeterBar
          segments={
            runs.total > 0
              ? [
                  { fraction: runs.completed / runs.total, color: 'success.main' },
                  { fraction: runs.failed / runs.total, color: 'error.main' },
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
      {status.error ? <FailureBox error={status.error} /> : null}
      {status.campaign_id && shownJobs && shownJobs.length > 0 ? (
        <JobsSection campaignId={status.campaign_id} jobs={shownJobs} />
      ) : null}
      {status.campaign_id && !hideLog ? (
        <CampaignLog campaignId={status.campaign_id} terminal={terminal} />
      ) : null}
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

function JobsSection({ campaignId, jobs }: { campaignId: string; jobs: JobSummary[] }) {
  const [open, setOpen] = useState(false)
  const shown = jobs.slice(0, JOBS_RENDER_CAP)
  return (
    <Box>
      <Button size="small" variant="text" onClick={() => setOpen((o) => !o)}>
        {open ? 'Hide jobs' : `Show jobs (${jobs.length})`}
      </Button>
      {open ? (
        <Stack spacing={0.5} sx={{ mt: 0.5 }}>
          {shown.map((job) => (
            <JobRow key={job.job_name} campaignId={campaignId} job={job} />
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

function JobRow({ campaignId, job }: { campaignId: string; job: JobSummary }) {
  const [open, setOpen] = useState(false)
  // A blocked job has no running pod, so its log never streams — treat it as terminal
  // (stop polling) like completed/failed; the reason lives in job.detail below.
  const terminal =
    job.status === 'completed' || job.status === 'failed' || job.status === 'blocked'
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
          onClick={() => setOpen((o) => !o)}
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
          sx={{ display: 'block', color: 'error.main', pl: 0.5, wordBreak: 'break-word' }}
        >
          {job.detail}
        </Typography>
      ) : null}
      {open ? (
        <LogPanel
          resetKey={`${campaignId}/${job.job_name}`}
          terminal={terminal}
          fetchChunk={(offset) => robovast.getJobLog(campaignId, job.job_name, offset)}
        />
      ) : null}
    </Box>
  )
}

// A stable, legible color per container name, hashed into a fixed palette. The
// palette avoids very light/very dark hues so lines stay readable on the log
// panel's `background.paper` in both light and dark themes.
const CONTAINER_COLORS = [
  '#2e9599', // teal
  '#c9611e', // orange
  '#8250df', // purple
  '#2f7d31', // green
  '#1f6feb', // blue
  '#c2185b', // pink
  '#8a6d1a', // olive
  '#5a6b7a', // slate
]
function colorForContainer(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0
  return CONTAINER_COLORS[Math.abs(h) % CONTAINER_COLORS.length]
}

// Multi-container job logs arrive with each line tagged `[container] …` (merged
// server-side). Color only the `[container]` prefix per container; the rest of the
// line keeps the default text color. Lines without a tag render unchanged.
function renderLogLines(text: string) {
  return text.split('\n').map((line, i) => {
    const m = /^(\[[^\]]+\]) ?/.exec(line)
    const nl = i > 0 ? '\n' : ''
    if (!m) return <span key={i}>{nl + line}</span>
    const prefix = m[0]
    const rest = line.slice(prefix.length)
    return (
      <span key={i}>
        {nl}
        <span style={{ color: colorForContainer(m[1].slice(1, -1)) }}>{prefix}</span>
        {rest}
      </span>
    )
  })
}

// Streams a log incrementally: polls `fetchChunk` from a byte offset and appends
// the returned slice — the same offset protocol the CLI/MCP use — autoscrolling to
// the newest line. `resetKey` restarts the stream when the source changes; `terminal`
// lets it stop after one final empty read. Always streams while mounted (callers gate
// visibility by mounting/unmounting it).
function LogPanel({
  resetKey,
  terminal,
  fetchChunk,
}: {
  resetKey: string
  terminal: boolean
  fetchChunk: (offset: number) => Promise<LogChunk>
}) {
  const [text, setText] = useState('')
  const [eof, setEof] = useState(false)
  const offset = useRef(0)
  const preRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    offset.current = 0
    setText('')
    setEof(false)
  }, [resetKey])

  useQuery({
    queryKey: ['logpanel', resetKey],
    enabled: !eof,
    queryFn: async () => {
      const chunk = await fetchChunk(offset.current)
      if (chunk.text) {
        offset.current = chunk.next_offset
        setText((t) => t + chunk.text)
      }
      if (chunk.eof || (terminal && !chunk.text)) setEof(true)
      return chunk
    },
    refetchInterval: () => (eof ? false : 1500),
  })

  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight
  }, [text])

  const lines = useMemo(() => (text ? renderLogLines(text) : null), [text])

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
      {lines || (eof ? '(no log)' : 'loading…')}
    </Box>
  )
}

// Live unified infrastructure log for one campaign (variation + run + postprocessing
// phases, divider-separated) via getCampaignLogs. Collapsed by default.
export function CampaignLog({ campaignId, terminal }: { campaignId: string; terminal: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <Box>
      <Button size="small" variant="text" onClick={() => setOpen((o) => !o)}>
        {open ? 'Hide log' : 'Show log'}
      </Button>
      {open ? (
        <LogPanel
          resetKey={campaignId}
          terminal={terminal}
          fetchChunk={(offset) => robovast.getCampaignLogs(campaignId, offset)}
        />
      ) : null}
    </Box>
  )
}

// The controller's failure reason (message + traceback tail) — what you'd otherwise
// have to dig out of the pod log. Shown verbatim, monospaced, and scrollable.
export function FailureBox({ error }: { error: string }) {
  return (
    <Box
      sx={{
        border: 1,
        borderColor: 'error.main',
        borderRadius: 1,
        bgcolor: 'error.main',
        color: 'error.contrastText',
      }}
    >
      <Typography variant="caption" sx={{ display: 'block', px: 1, py: 0.5, fontWeight: 600 }}>
        failure
      </Typography>
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
    </Box>
  )
}
