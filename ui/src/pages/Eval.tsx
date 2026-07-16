import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import Stack from '@mui/material/Stack'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { robovast, campaignsNewestFirst, type DataQueryResult, type PlotSpec } from '@/lib/robovastClient'
import { FailureBox } from '@/components/StatusView'
import { VegaLiteChart } from '@/preview/VegaLiteChart'
import '@/lib/monaco' // configures the Monaco loader + workers (SQL editor below)

const DEFAULT_SQL = 'SELECT * FROM runs LIMIT 500'
const NONE = '(none)'

// Web equivalent of `vast eval gui`: pick a campaign, browse its data.db schema + runs matrix,
// run read-only SQL, and chart the result with Vega-Lite. Notebook analysis stays in the desktop GUI.
export function Eval() {
  const qc = useQueryClient()
  const [campaignId, setCampaignId] = useState('')
  const [sqlBuffer, setSqlBuffer] = useState(DEFAULT_SQL)
  const [activeSql, setActiveSql] = useState(DEFAULT_SQL)
  const [x, setX] = useState('')
  const [y, setY] = useState('')
  const [color, setColor] = useState(NONE)
  const [mark, setMark] = useState<'point' | 'line' | 'bar' | 'boxplot'>('point')

  const campaigns = useQuery({ queryKey: ['campaigns'], queryFn: () => robovast.listCampaigns(200, 0) })
  const describe = useQuery({
    queryKey: ['describe', campaignId],
    queryFn: () => robovast.describeCampaignData(campaignId),
    enabled: !!campaignId,
    retry: false,
  })
  const result = useQuery<DataQueryResult>({
    queryKey: ['query', campaignId, activeSql],
    queryFn: () => robovast.queryCampaignDataSql(campaignId, activeSql),
    enabled: !!campaignId && !!activeSql,
    retry: false,
  })
  const plots = useQuery({
    queryKey: ['plots', campaignId],
    queryFn: () => robovast.listCampaignPlots(campaignId),
    enabled: !!campaignId,
    retry: false,
  })
  // A failed campaign never produces data.db — surface *why* it failed (the same
  // reason the Monitor shows) instead of an endless "run postprocessing" prompt.
  const status = useQuery({
    queryKey: ['status', campaignId],
    queryFn: () => robovast.getStatus(campaignId),
    enabled: !!campaignId,
    retry: false,
  })
  const failure = status.data?.phase === 'failed' ? status.data.error : null

  // A campaign has no queryable data until analysis postprocessing has run (it
  // generates data.db). Offer to run it right here when that's the case.
  const noData = /data\.db/i.test(
    (describe.error as Error | null)?.message ?? (result.error as Error | null)?.message ?? '',
  )
  const postprocess = useMutation({
    mutationFn: () => robovast.runPostprocessing(campaignId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['describe', campaignId] })
      qc.invalidateQueries({ queryKey: ['query', campaignId] })
      qc.invalidateQueries({ queryKey: ['plots', campaignId] })
    },
  })

  // Seed the chart axes from the result columns.
  const cols = result.data?.columns ?? []
  useEffect(() => {
    if (cols.length) {
      setX((cur) => (cur && cols.includes(cur) ? cur : cols[0]))
      setY((cur) => (cur && cols.includes(cur) ? cur : cols[Math.min(1, cols.length - 1)]))
    }
  }, [cols.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  const chartSpec =
    x && y
      ? {
          mark: mark === 'boxplot' ? { type: 'boxplot' as const } : { type: mark, tooltip: true },
          encoding: {
            x: { field: x, type: 'nominal' as const },
            y: { field: y, type: 'quantitative' as const },
            ...(color !== NONE ? { color: { field: color, type: 'nominal' as const } } : {}),
          },
        }
      : null

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Results</Typography>
        <TextField
          select={!!campaigns.data?.campaigns.length}
          size="small"
          label="Campaign"
          value={campaignId}
          onChange={(e) => {
            setCampaignId(e.target.value)
            setSqlBuffer(DEFAULT_SQL)
            setActiveSql(DEFAULT_SQL)
          }}
          sx={{ minWidth: 340 }}
        >
          {campaignsNewestFirst(campaigns.data?.campaigns ?? []).map((c) => (
            <MenuItem key={c.campaign_id} value={c.campaign_id}>
              {c.campaign_id}
            </MenuItem>
          ))}
        </TextField>
      </Stack>

      {!campaignId ? (
        <Alert severity="info" variant="outlined">
          Pick a campaign to browse its results.
        </Alert>
      ) : failure ? (
        <Stack spacing={1}>
          <Alert severity="error" variant="outlined">
            This campaign failed, so it produced no results to query.
          </Alert>
          <FailureBox error={failure} />
        </Stack>
      ) : noData ? (
        <Alert
          severity="info"
          action={
            <Button
              color="inherit"
              size="small"
              startIcon={<PlayArrowRoundedIcon />}
              disabled={postprocess.isPending}
              onClick={() => postprocess.mutate()}
            >
              {postprocess.isPending ? 'Postprocessing…' : 'Run postprocessing'}
            </Button>
          }
        >
          {postprocess.isError
            ? `Postprocessing failed: ${(postprocess.error as Error).message}`
            : 'This campaign has no queryable data yet — run analysis postprocessing to generate data.db.'}
        </Alert>
      ) : (
        <Box sx={{ display: 'grid', gridTemplateColumns: '260px 1fr', gap: 2 }}>
          {/* Schema panel */}
          <Paper sx={{ p: 1.5, overflow: 'auto', maxHeight: 560 }}>
            <Typography variant="subtitle2" mb={1}>
              Tables
            </Typography>
            {describe.isError ? (
              <Alert severity="warning" variant="outlined" sx={{ py: 0 }}>
                {(describe.error as Error).message}
              </Alert>
            ) : (
              (describe.data?.tables ?? []).map((t) => (
                <Box key={`${t.schema}.${t.table}`} mb={1}>
                  <Typography
                    variant="caption"
                    sx={{ fontFamily: 'monospace', cursor: 'pointer' }}
                    onClick={() => {
                      const q = `SELECT * FROM ${t.schema === 'main' ? '' : t.schema + '.'}${t.table} LIMIT 500`
                      setSqlBuffer(q)
                      setActiveSql(q)
                    }}
                  >
                    {t.schema === 'main' ? '' : `${t.schema}.`}
                    {t.table}
                    {t.rows != null ? ` (${t.rows})` : ''}
                  </Typography>
                  <Box sx={{ pl: 1, color: 'text.secondary', fontSize: 11 }}>
                    {t.columns.join(', ')}
                  </Box>
                </Box>
              ))
            )}
          </Paper>

          {/* Query + result + chart */}
          <Stack spacing={1.5} sx={{ minWidth: 0 }}>
            <Paper sx={{ height: 130, overflow: 'hidden' }}>
              <Editor
                height="130px"
                language="sql"
                value={sqlBuffer}
                onChange={(v) => setSqlBuffer(v ?? '')}
                theme="vs-dark"
                options={{ minimap: { enabled: false }, fontSize: 13, lineNumbers: 'off' }}
              />
            </Paper>
            <Stack direction="row" spacing={1} alignItems="center">
              <Button
                size="small"
                variant="contained"
                startIcon={<PlayArrowRoundedIcon />}
                onClick={() => setActiveSql(sqlBuffer)}
                disabled={result.isFetching}
              >
                Run
              </Button>
              {result.data ? (
                <Typography variant="caption" color="text.secondary">
                  {result.data.row_count} rows{result.data.truncated ? ' (truncated)' : ''}
                </Typography>
              ) : null}
            </Stack>

            {result.isError ? (
              <Alert severity="error">{(result.error as Error).message}</Alert>
            ) : null}

            {/* Chart builder */}
            {cols.length ? (
              <Paper sx={{ p: 1.5 }}>
                <Stack direction="row" spacing={1} flexWrap="wrap" alignItems="center" mb={1}>
                  <Chip size="small" label="chart" />
                  <AxisSelect label="x" value={x} onChange={setX} options={cols} />
                  <AxisSelect label="y" value={y} onChange={setY} options={cols} />
                  <AxisSelect label="color" value={color} onChange={setColor} options={[NONE, ...cols]} />
                  <TextField
                    select
                    size="small"
                    label="mark"
                    value={mark}
                    onChange={(e) => setMark(e.target.value as typeof mark)}
                    sx={{ width: 120 }}
                  >
                    {['point', 'line', 'bar', 'boxplot'].map((m) => (
                      <MenuItem key={m} value={m}>
                        {m}
                      </MenuItem>
                    ))}
                  </TextField>
                </Stack>
                {chartSpec ? <VegaLiteChart spec={chartSpec} rows={result.data!.rows} /> : null}
              </Paper>
            ) : null}

            {/* Result table */}
            {cols.length ? (
              <Paper sx={{ overflow: 'auto', maxHeight: 320 }}>
                <Table size="small" stickyHeader>
                  <TableHead>
                    <TableRow>
                      {cols.map((c) => (
                        <TableCell key={c} sx={{ fontWeight: 700, whiteSpace: 'nowrap' }}>
                          {c}
                        </TableCell>
                      ))}
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {(result.data?.rows ?? []).map((row, i) => (
                      <TableRow key={i}>
                        {cols.map((c) => (
                          <TableCell key={c} sx={{ whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: 12 }}>
                            {formatCell(row[c])}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Paper>
            ) : null}

            {/* User-declared plots (evaluation.plots in the .vast) */}
            {plots.data?.plots.length ? (
              <Stack spacing={1}>
                <Typography variant="subtitle2">Declared plots</Typography>
                {plots.data.plots.map((p, i) => (
                  <DeclaredPlot key={i} campaignId={campaignId} plot={p} />
                ))}
              </Stack>
            ) : null}
          </Stack>
        </Box>
      )}
    </Stack>
  )
}

function AxisSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: string[]
}) {
  return (
    <TextField
      select
      size="small"
      label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      sx={{ width: 160 }}
    >
      {options.map((o) => (
        <MenuItem key={o} value={o}>
          {o}
        </MenuItem>
      ))}
    </TextField>
  )
}

function formatCell(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// A user-declared plot: run its SQL, bind the rows into its Vega-Lite spec.
function DeclaredPlot({ campaignId, plot }: { campaignId: string; plot: PlotSpec }) {
  const q = useQuery({
    queryKey: ['plot', campaignId, plot.query],
    queryFn: () => robovast.queryCampaignDataSql(campaignId, plot.query),
    enabled: !!campaignId,
    retry: false,
  })
  return (
    <Paper sx={{ p: 1.5 }}>
      <Typography variant="body2" sx={{ fontWeight: 700 }} mb={0.5}>
        {plot.title || '(untitled plot)'}
      </Typography>
      {q.isError ? (
        <Alert severity="warning" variant="outlined" sx={{ py: 0 }}>
          {(q.error as Error).message}
        </Alert>
      ) : q.data ? (
        <VegaLiteChart spec={plot.vega_lite} rows={q.data.rows} />
      ) : (
        <Typography variant="caption" color="text.secondary">
          loading…
        </Typography>
      )}
    </Paper>
  )
}
