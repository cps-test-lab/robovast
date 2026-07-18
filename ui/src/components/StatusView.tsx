import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import LinearProgress from '@mui/material/LinearProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import {
  robovast,
  type JobSummary,
  type ListJobsResponse,
  type LogChunk,
  type Status,
} from '@/lib/robovastClient'
import { PhaseChip } from './PhaseChip'

// Renders one campaign's live Status — the browser analog of what `vast exec cluster monitor` prints:
// phase, run-level progress within the current batch, batch counter, and each budget/stopping
// criterion. Purely presentational; the caller supplies the (polled) Status and, optionally, the
// (polled) live jobs listing.

function pct(done: number, total: number): number {
  return total > 0 ? Math.min(100, (100 * done) / total) : 0
}

export function StatusView({ status, jobs }: { status: Status; jobs?: ListJobsResponse }) {
  const { runs, budget } = status
  const terminal = ['finished', 'failed', 'stopped', 'error'].includes(status.phase)
  const counts = jobs?.counts
  const running = counts?.running ?? 0
  // Buffer variant: the solid bar is finished runs, the lighter segment on top is
  // what's currently running (with the usual animated dots for the rest).
  const showRunning = runs.total > 0 && running > 0
  return (
    <Stack spacing={1.5}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
        <PhaseChip phase={status.phase} />
        {status.stage ? (
          <Typography variant="caption" color="text.secondary">
            {status.stage}
          </Typography>
        ) : null}
        <Box flexGrow={1} />
        <Typography variant="caption" color="text.secondary">
          batch {status.batch}
          {status.batches_done ? ` · ${status.batches_done} done` : ''}
        </Typography>
      </Stack>

      <Box>
        <Stack direction="row" justifyContent="space-between">
          <Typography variant="caption" color="text.secondary">
            runs (this batch)
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {counts && (counts.running > 0 || counts.pending > 0)
              ? `running ${counts.running} · pending ${counts.pending} · `
              : ''}
            {runs.completed}/{runs.total}
          </Typography>
        </Stack>
        <LinearProgress
          variant={
            showRunning ? 'buffer' : runs.total > 0 || terminal ? 'determinate' : 'indeterminate'
          }
          value={pct(runs.completed, runs.total)}
          valueBuffer={pct(runs.completed + running, runs.total)}
          sx={{ height: 8, borderRadius: 1 }}
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
          <LinearProgress
            variant="determinate"
            color="secondary"
            value={b.current == null ? 0 : pct(b.current, b.limit)}
            sx={{ height: 6, borderRadius: 1 }}
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
      {status.campaign_id && jobs && jobs.jobs.length > 0 ? (
        <JobsSection campaignId={status.campaign_id} jobs={jobs.jobs} />
      ) : null}
      {status.campaign_id ? (
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
  const terminal = job.status === 'completed' || job.status === 'failed'
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
      {text || (eof ? '(no log)' : 'loading…')}
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
