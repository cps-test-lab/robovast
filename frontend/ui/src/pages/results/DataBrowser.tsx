import { useEffect, useRef, useState } from 'react'
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
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import {
  DataGrid,
  GridToolbarContainer,
  GridToolbarColumnsButton,
  GridToolbarFilterButton,
  GridToolbarExport,
  GridToolbarQuickFilter,
  type GridColDef,
} from '@mui/x-data-grid'
import {
  robovast,
  hasResults,
  type CampaignSummary,
  type DataQueryResult,
  type PlotSpec,
} from '@/lib/robovastClient'
import { FailureBox } from '@/components/StatusView'
import { useToasts } from '@/components/ToastProvider'
import { formatDataFetchLabel } from '@/lib/format'
import { VegaLiteChart } from '@/components/VegaLiteChart'
import { RefreshResultsButton, type ResultsRefresh } from './RefreshResultsButton'
import '@/lib/monaco' // configures the Monaco loader + workers (SQL editor below)

const DEFAULT_SQL = 'SELECT * FROM runs LIMIT 500'
const NONE = '(none)'

// A deep-link from the Explorer: run this SQL against the (already-selected) campaign. `nonce`
// makes repeated identical requests re-apply.
export interface SqlRequest {
  sql?: string
  nonce: number
}

// DataGrid toolbar without the density selector (Columns / Filters / Export / quick filter only).
function DataGridToolbar() {
  return (
    <GridToolbarContainer>
      <GridToolbarColumnsButton />
      <GridToolbarFilterButton />
      <GridToolbarExport />
      <GridToolbarQuickFilter />
    </GridToolbarContainer>
  )
}

// Results → Data browser: the web equivalent of `vast eval gui`. Browse a campaign's results
// schema, run read-only SQL, chart it with Vega-Lite, and page through rows in a DataGrid. The
// selected campaign is owned by the parent (shared with the Explorer).
export function DataBrowser({
  campaignId,
  campaigns,
  onCampaignChange,
  refresh,
  sqlRequest,
}: {
  campaignId: string
  campaigns: CampaignSummary[]
  onCampaignChange: (campaignId: string) => void
  refresh: ResultsRefresh
  sqlRequest?: SqlRequest
}) {
  const qc = useQueryClient()
  const { notify } = useToasts()
  const [sqlBuffer, setSqlBuffer] = useState(DEFAULT_SQL)
  const [activeSql, setActiveSql] = useState(DEFAULT_SQL)
  const [x, setX] = useState('')
  const [y, setY] = useState('')
  const [color, setColor] = useState(NONE)
  const [mark, setMark] = useState<'point' | 'line' | 'bar' | 'boxplot'>('point')

  // Only finished+postprocessed campaigns have the derived data this viewer queries.
  // (The Results container already filters to these; kept defensive since `campaigns` is a prop.)
  // Newest-first is the service's order; filtering preserves it.
  const evalCampaigns = campaigns.filter(hasResults)

  // Apply an Explorer deep-link: set the editor + run its query once per nonce.
  const lastNonce = useRef<number>(-1)
  useEffect(() => {
    if (!sqlRequest || sqlRequest.nonce === lastNonce.current) return
    lastNonce.current = sqlRequest.nonce
    const sql = sqlRequest.sql ?? DEFAULT_SQL
    setSqlBuffer(sql)
    setActiveSql(sql)
  }, [sqlRequest])

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
  // Why a query may be slow, asked alongside it: on a cluster campaign the first one fetches
  // the databases from the object store inside the request. Cheap and advisory — a failure
  // (an older service has no such route) just means no label.
  const dataStatus = useQuery({
    queryKey: ['data-status', campaignId],
    queryFn: () => robovast.campaignDataStatus(campaignId),
    enabled: !!campaignId,
    retry: false,
    staleTime: 60_000,
    // Poll only while a query is outstanding: the label carries live transfer counts, so a
    // single fetch would freeze it at whatever the first sample said.
    // `isFetching`, not `isPending`: a disabled query (no SQL entered yet) stays pending
    // forever, which would poll an idle tab for the life of the session.
    refetchInterval: describe.isFetching || result.isFetching ? 1000 : false,
  })
  const fetchLabel = formatDataFetchLabel(dataStatus.data)
  // A failed campaign never produces derived data — surface *why* it failed (the same
  // reason the Monitor shows) instead of an endless "run postprocessing" prompt.
  const status = useQuery({
    queryKey: ['status', campaignId],
    queryFn: () => robovast.getStatus(campaignId),
    enabled: !!campaignId,
    retry: false,
  })
  const failure = status.data?.phase === 'failed' ? status.data.error : null

  // A campaign has no queryable data until analysis postprocessing has run (it ingests the
  // campaign into the central index). Offer to run it right here when that's the case.
  //
  // Matched on the index's own "not in the index" wording, from
  // `results_processing.index_query.missing_campaign_note`. This used to test for `data.db`,
  // the per-campaign file postprocessing wrote before the index — a predicate that stayed
  // true-looking while being permanently false, so the offer silently stopped appearing.
  const noData = /not in the index/i.test(
    (describe.error as Error | null)?.message ?? (result.error as Error | null)?.message ?? '',
  )
  const postprocess = useMutation({
    mutationFn: () => robovast.runPostprocessing(campaignId),
    onSuccess: (res) => {
      // `ok: false` is the busy guard refusing because something is already running. It used to
      // land here and be discarded, which left the button looking like it had worked.
      if (!res.ok) {
        notify({
          severity: 'warning',
          message: 'Postprocessing was not started',
          note: res.message || 'Another operation is already running on this campaign.',
        })
        return
      }
      qc.invalidateQueries({ queryKey: ['describe', campaignId] })
      qc.invalidateQueries({ queryKey: ['query', campaignId] })
      qc.invalidateQueries({ queryKey: ['plots', campaignId] })
      notify({
        severity: 'info',
        message: 'Postprocessing started',
        note: 'These results refresh when it finishes.',
      })
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

  // DataGrid needs stable synthetic field ids (query columns can contain dots/spaces) and a row id.
  const gridColumns: GridColDef[] = cols.map((name, i) => ({
    field: `c${i}`,
    headerName: name,
    flex: 1,
    minWidth: 120,
  }))
  const gridRows = (result.data?.rows ?? []).map((row, i) => {
    const r: Record<string, unknown> = { id: i }
    cols.forEach((name, j) => {
      r[`c${j}`] = toCell(row[name])
    })
    return r
  })

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Data browser</Typography>
        <TextField
          select
          size="small"
          label="Campaign"
          // Never show — or let someone type — an id that is not in the list: this is a picker over
          // what the service has, not a free-text address bar.
          value={evalCampaigns.some((c) => c.campaign_id === campaignId) ? campaignId : ''}
          disabled={!evalCampaigns.length}
          helperText={
            evalCampaigns.length ? undefined : 'no finished, postprocessed campaign yet'
          }
          onChange={(e) => {
            onCampaignChange(e.target.value)
            setSqlBuffer(DEFAULT_SQL)
            setActiveSql(DEFAULT_SQL)
          }}
          sx={{ minWidth: 340 }}
        >
          {/* Gives the empty selection an option to match, so MUI does not warn about an
              out-of-range value, and names the empty case instead of showing a blank box. */}
          <MenuItem value="" disabled>
            {evalCampaigns.length ? 'Select a campaign' : 'No campaigns with results'}
          </MenuItem>
          {evalCampaigns.map((c) => (
            <MenuItem key={c.campaign_id} value={c.campaign_id}>
              {c.campaign_id}
            </MenuItem>
          ))}
        </TextField>
        {/* Beside the selector it feeds: the reload is what puts a newly finished campaign into
            that list. */}
        <RefreshResultsButton state={refresh} />
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
            : 'This campaign has no queryable data yet — run analysis postprocessing to index it.'}
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
              {result.data && !result.isFetching ? (
                // The service explains a truncation it did not make for the row cap; "(truncated)"
                // alone sends the reader to `LIMIT`, which is the fix for only one of the causes.
                <Typography
                  variant="caption"
                  color="text.secondary"
                  title={result.data.note ?? undefined}
                >
                  {result.data.row_count} rows{result.data.truncated ? ' (truncated)' : ''}
                </Typography>
              ) : null}
              {/* While a query runs, say if the wait is an object-store fetch rather than
                  the query itself — otherwise a multi-minute first load looks like a hang. */}
              {result.isFetching && fetchLabel ? (
                <Typography variant="caption" color="text.secondary">
                  {fetchLabel}
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
              <Paper sx={{ height: 420 }}>
                <DataGrid
                  rows={gridRows}
                  columns={gridColumns}
                  density="compact"
                  disableRowSelectionOnClick
                  slots={{ toolbar: DataGridToolbar }}
                  initialState={{ pagination: { paginationModel: { pageSize: 100 } } }}
                  pageSizeOptions={[25, 100, 500]}
                  sx={{ border: 0, fontFamily: 'monospace', fontSize: 12 }}
                />
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

// Coerce a query cell for the DataGrid: keep primitives (so numbers sort numerically), JSON any
// object/array so it still renders.
function toCell(v: unknown): string | number | boolean | null {
  if (v == null) return null
  if (typeof v === 'object') return JSON.stringify(v)
  return v as string | number | boolean
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
