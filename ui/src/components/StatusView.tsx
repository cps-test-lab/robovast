import Box from '@mui/material/Box'
import LinearProgress from '@mui/material/LinearProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { Status } from '@/lib/robovastClient'
import { PhaseChip } from './PhaseChip'

// Renders one campaign's live Status — the browser analog of what `vast exec cluster monitor` prints:
// phase, run-level progress within the current batch, batch counter, and each budget/stopping
// criterion. Purely presentational; the caller supplies the (polled) Status.

function pct(done: number, total: number): number {
  return total > 0 ? Math.min(100, (100 * done) / total) : 0
}

export function StatusView({ status }: { status: Status }) {
  const { runs, budget } = status
  const terminal = ['finished', 'failed', 'stopped', 'error'].includes(status.phase)
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
            {runs.completed}/{runs.total}
          </Typography>
        </Stack>
        <LinearProgress
          variant={runs.total > 0 ? 'determinate' : terminal ? 'determinate' : 'indeterminate'}
          value={pct(runs.completed, runs.total)}
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
    </Stack>
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
