import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import FormControlLabel from '@mui/material/FormControlLabel'
import Stack from '@mui/material/Stack'
import Switch from '@mui/material/Switch'
import Typography from '@mui/material/Typography'

import { MeterBar } from '@/components/MeterBar'
import { robovast, type McpCall, type McpToolStat } from '@/lib/robovastClient'
import { barShares, formatDurationMs, maxCalls, rankTools, retentionNote } from './mcpToolStats'

// Which MCP tools agents reach for, and what happened when they did.
//
// Two views of one record, which is why they share a panel: the ranking is an aggregate over
// the very rows the log below it lists, so the two can never disagree the way a counter kept
// beside a log eventually does. Clicking a tool filters the log to it — the ranking says which
// tool is worth asking about, the log answers what it was actually asked.

function ToolRow({ stat, most, selected, onSelect }: {
  stat: McpToolStat
  most: number
  selected: boolean
  onSelect: () => void
}) {
  const shares = barShares(stat, most)
  return (
    <Box
      onClick={onSelect}
      sx={{
        py: 0.5, cursor: 'pointer', borderBottom: 1, borderColor: 'divider',
        bgcolor: selected ? 'action.selected' : undefined,
        '&:hover': { bgcolor: 'action.hover' },
      }}
    >
      <Stack direction="row" spacing={1} alignItems="baseline">
        <Typography variant="body2" sx={{ fontFamily: 'monospace', flex: '1 1 auto' }}>
          {stat.tool}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {stat.calls === 0 ? 'never called' : `${stat.calls}×`}
        </Typography>
        {stat.calls > 0 ? (
          <Typography variant="caption" color="text.secondary" sx={{ minWidth: 120,
            textAlign: 'right' }}>
            {formatDurationMs(stat.mean_ms)} avg · {formatDurationMs(stat.max_ms)} max
          </Typography>
        ) : null}
      </Stack>
      {/* Green for what succeeded, red for what did not, laid end to end: the bar's total
          length still compares call counts across tools, and the red tail is the failure
          share of that same length. No separate error chip — the bar carries it. */}
      <MeterBar
        height={8}
        segments={[
          { fraction: shares.ok, color: 'success.main' },
          { fraction: shares.failed, color: 'error.main' },
        ]}
      />
    </Box>
  )
}

// Epoch seconds here, as the log records them — the same treatment ServiceEventsPanel gives
// its own `at`.
function when(at: number): string {
  return new Date(at * 1000).toLocaleString()
}

function CallRow({ call }: { call: McpCall }) {
  const payload = (label: string, text: string) => (
    <Box
      component="span"
      sx={{
        display: 'block', mt: 0.25, fontFamily: 'monospace', fontSize: '0.75rem',
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        color: label === 'answer' && !call.ok ? 'error.main' : 'text.secondary',
      }}
    >
      {label}: {text}
    </Box>
  )
  return (
    <Box sx={{ py: 0.75, borderBottom: 1, borderColor: 'divider' }}>
      <Stack direction="row" spacing={1} alignItems="baseline" sx={{ flexWrap: 'wrap' }}>
        <Chip size="small" variant="outlined" color={call.ok ? 'success' : 'error'}
              label={call.ok ? 'ok' : 'failed'} />
        <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>{call.tool}</Typography>
        <Typography variant="caption" color="text.secondary">
          {when(call.at)} · {formatDurationMs(call.duration_ms)}
        </Typography>
        {call.actor ? (
          <Typography variant="caption" color="text.secondary">· {call.actor}</Typography>
        ) : null}
      </Stack>
      {call.args ? payload('args', call.args) : null}
      {call.answer ? payload('answer', call.answer) : null}
    </Box>
  )
}

export function McpToolsPanel({ active }: { active: boolean }) {
  const [tool, setTool] = useState('')
  const [failedOnly, setFailedOnly] = useState(false)
  const [limit, setLimit] = useState(50)

  const stats = useQuery({
    queryKey: ['mcp-tool-stats'],
    queryFn: robovast.mcpToolStats,
    enabled: active,
  })
  const calls = useQuery({
    queryKey: ['mcp-calls', tool, failedOnly, limit],
    queryFn: () => robovast.mcpCalls(limit, tool, failedOnly),
    enabled: active,
  })

  if (stats.isPending) return <CircularProgress size={20} />
  if (stats.isError) return <Alert severity="error">{(stats.error as Error).message}</Alert>

  // The two ways this can be empty are not the same answer, and the panel must not draw the
  // second as the first: an unreachable index means the record is unknown, not that no tool
  // has been called. See `mcp_server/tool_stats.py` — the log lives in the central index.
  if (stats.data.status !== 'ok') {
    return (
      <Alert severity="warning">
        The call record is unavailable: {stats.data.detail || stats.data.status}
      </Alert>
    )
  }

  const ranked = rankTools(stats.data.tools)
  const most = maxCalls(ranked)
  const rows = calls.data?.calls ?? []

  return (
    <Stack spacing={1.5}>
      {most === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No MCP tool has been called yet. The tools below are registered and waiting.
        </Typography>
      ) : null}

      <Box>
        {ranked.map((stat) => (
          <ToolRow
            key={stat.tool}
            stat={stat}
            most={most}
            selected={tool === stat.tool}
            onSelect={() => setTool((current) => (current === stat.tool ? '' : stat.tool))}
          />
        ))}
      </Box>

      <Stack direction="row" spacing={2} alignItems="center" sx={{ flexWrap: 'wrap' }}>
        <Typography variant="subtitle2">
          {tool ? `Calls to ${tool}` : 'Recent calls'}
        </Typography>
        {tool ? (
          <Button size="small" onClick={() => setTool('')}>Show all tools</Button>
        ) : null}
        <FormControlLabel
          control={<Switch size="small" checked={failedOnly}
                           onChange={(e) => setFailedOnly(e.target.checked)} />}
          label={<Typography variant="caption">Failed only</Typography>}
        />
        <Button size="small" component="a" href={robovast.mcpCallsCsvUrl(tool, failedOnly)}>
          Export CSV
        </Button>
      </Stack>

      {/* What the record covers, said rather than left to be inferred from a short list —
          and the same sentence governs the export button above, which is this record and
          not all history. */}
      <Typography variant="caption" color="text.secondary">
        Kept in the central index: {retentionNote(stats.data.max_age_s, stats.data.max_rows)}.
        Arguments and answers are truncated to a few lines where they are recorded.
      </Typography>

      {calls.isPending ? <CircularProgress size={20} /> : null}
      {calls.isError ? (
        <Alert severity="error">{(calls.error as Error).message}</Alert>
      ) : null}
      <Box>
        {rows.map((call, i) => <CallRow key={`${call.at}-${call.tool}-${i}`} call={call} />)}
      </Box>
      {/* Only offered when the page is full: a shorter answer is the whole record. */}
      {rows.length >= limit ? (
        <Button size="small" sx={{ alignSelf: 'flex-start' }}
                onClick={() => setLimit((n) => n + 200)}>
          Show more
        </Button>
      ) : null}
    </Stack>
  )
}
