import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import InputAdornment from '@mui/material/InputAdornment'
import LinearProgress from '@mui/material/LinearProgress'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import ClearRoundedIcon from '@mui/icons-material/ClearRounded'
import SearchRoundedIcon from '@mui/icons-material/SearchRounded'
import { useTheme } from '@mui/material/styles'
import { robovast, type CampaignSummary } from '@/lib/robovastClient'
import { formatDataFetchLabel, progressPercent } from '@/lib/format'
import { campaignItem, type ResultsTreeItem } from '@/lib/resultsTree'
import { ResultsTree } from './ResultsTree'
import { RefreshResultsButton, type ResultsRefresh } from './RefreshResultsButton'

// Results → Explorer: a campaign → config → run tree with green/red status, all from the DB. The
// tree is the campaign selector; clicking a node renders its evaluation.visualization notebooks.
export function ExplorerView({
  campaignId,
  campaigns,
  onCampaignChange,
  refresh,
}: {
  campaignId: string
  campaigns: CampaignSummary[]
  onCampaignChange: (campaignId: string) => void
  refresh: ResultsRefresh
}) {
  const [selected, setSelected] = useState<ResultsTreeItem | undefined>()
  const [filter, setFilter] = useState('')

  // The tree's *campaign* is shared with the other Results views (it is in the URL); which config or
  // run is open below it stays private to this view. So the selection follows the campaign in from
  // outside — a card's shortcut, or the campaign picked in the Run view — and reports a campaign
  // picked here back out. Only a change of campaign moves the selection: re-selecting the campaign
  // node on every render would fight the user clicking a run underneath it.
  useEffect(() => {
    if (!campaignId || selected?.campaignId === campaignId) return
    const c = campaigns.find((x) => x.campaign_id === campaignId)
    if (!c) return
    setSelected(campaignItem(c))
    // A filter left over from an earlier search would hide the campaign that was just asked for,
    // leaving "No campaign matches …" where the selected node should be.
    setFilter((f) => (campaignId.toLowerCase().includes(f.trim().toLowerCase()) ? f : ''))
  }, [campaignId, campaigns]) // eslint-disable-line react-hooks/exhaustive-deps

  const select = (item: ResultsTreeItem) => {
    setSelected(item)
    if (item.campaignId !== campaignId) onCampaignChange(item.campaignId)
  }

  // Campaign ids only: configs and runs are lazy-loaded per expanded campaign, so matching them
  // would hide branches whose children simply have not been fetched yet.
  const needle = filter.trim().toLowerCase()
  const shown = useMemo(
    () => (needle ? campaigns.filter((c) => c.campaign_id.toLowerCase().includes(needle)) : campaigns),
    [campaigns, needle],
  )

  return (
    <Stack spacing={2}>
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="h6">Explorer</Typography>
        <RefreshResultsButton state={refresh} />
      </Stack>

      {!campaigns.length ? (
        <Alert severity="info" variant="outlined">
          No finished campaigns yet — results appear here once a campaign finishes and is
          postprocessed.
        </Alert>
      ) : (
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: '380px 1fr',
            gap: 2,
            // Fill the viewport below the sidebar padding + page heading so both columns run full
            // height; each column scrolls internally rather than growing the page.
            height: 'calc(100vh - 140px)',
            minHeight: 0,
          }}
        >
          {/* Filter above, tree below: the Paper owns the column height and only the tree scrolls,
              so the field stays put while the list is paged through. */}
          <Paper sx={{ p: 1, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
            <TextField
              size="small"
              placeholder="Filter campaigns"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              sx={{ flexShrink: 0 }}
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchRoundedIcon fontSize="small" color="disabled" />
                    </InputAdornment>
                  ),
                  endAdornment: filter ? (
                    <InputAdornment position="end">
                      <IconButton
                        size="small"
                        aria-label="Clear filter"
                        onClick={() => setFilter('')}
                      >
                        <ClearRoundedIcon fontSize="small" />
                      </IconButton>
                    </InputAdornment>
                  ) : null,
                },
              }}
            />
            <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
              {shown.length ? (
                <ResultsTree
                  campaigns={shown}
                  selectedId={selected?.id ?? ''}
                  onSelect={select}
                />
              ) : (
                <Typography variant="caption" color="text.secondary" sx={{ pl: 1 }}>
                  No campaign matches “{filter}”.
                </Typography>
              )}
            </Box>
          </Paper>

          <SelectionDetail item={selected} />
        </Box>
      )}
    </Stack>
  )
}

// Right pane: the selected node's evaluation.visualization notebooks, executed for this node and
// shown as HTML — the web equivalent of the desktop `vast eval gui`.
function SelectionDetail({ item }: { item?: ResultsTreeItem }) {
  if (!item) {
    return (
      <Alert severity="info" variant="outlined">
        Select a campaign, config, or run to see its visualizations.
      </Alert>
    )
  }
  return (
    <Paper
      sx={{ p: 2, minHeight: 0, overflow: 'auto', display: 'flex', flexDirection: 'column' }}
    >
      <NotebookPanel item={item} />
    </Paper>
  )
}

// The notebook-visualization tabs for the selected node: one tab per workload that declares a
// notebook for this node's level (campaign/config/run). The active tab's notebook is executed
// server-side and rendered as HTML.
function NotebookPanel({ item }: { item: ResultsTreeItem }) {
  const campaignId = item.campaignId
  const level = item.kind // 'campaign' | 'config' | 'run' — matches the backend level names

  const vis = useQuery({
    queryKey: ['visualizations', campaignId],
    queryFn: () => robovast.listCampaignVisualizations(campaignId),
    retry: false,
    staleTime: 60_000,
  })

  const workloads = (vis.data?.workloads ?? []).filter((w) => w.levels.includes(level))
  const names = workloads.map((w) => w.name).join(',')

  const [active, setActive] = useState('')
  // Keep the active tab valid as the selection (and thus the applicable workloads) changes.
  useEffect(() => {
    if (!workloads.length) setActive('')
    else if (!workloads.some((w) => w.name === active)) setActive(workloads[0].name)
  }, [names, active]) // eslint-disable-line react-hooks/exhaustive-deps

  if (vis.isPending) return <CircularProgress size={18} />
  if (vis.isError)
    return (
      <Alert severity="warning" variant="outlined" sx={{ py: 0 }}>
        {(vis.error as Error).message}
      </Alert>
    )
  if (!workloads.length)
    return (
      <Typography variant="caption" color="text.secondary">
        No notebook visualizations declared for this {level}.
      </Typography>
    )

  return (
    <Stack spacing={1} sx={{ flex: 1, minHeight: 0 }}>
      <Tabs
        value={active}
        onChange={(_e, v) => setActive(v)}
        variant="scrollable"
        scrollButtons="auto"
        sx={{ minHeight: 36, flexShrink: 0 }}
      >
        {workloads.map((w) => (
          <Tab key={w.name} value={w.name} label={w.name} sx={{ minHeight: 36, py: 0 }} />
        ))}
      </Tabs>
      {active ? <NotebookFrame item={item} workload={active} level={level} /> : null}
    </Stack>
  )
}

// What a click on a cluster campaign is actually waiting for. The request that renders a notebook
// first pulls the whole campaign out of the object store — GBs over a port-forward — and only then
// executes the cells, so a bare spinner covered a wait that runs into minutes and reported nothing.
// The service publishes live counts on its data-status, which is cheap to poll *because* it answers
// from memory while busy; this asks once a second for exactly as long as the render is outstanding.
function NotebookWait({ campaignId }: { campaignId: string }) {
  const status = useQuery({
    queryKey: ['data-status', campaignId],
    queryFn: () => robovast.campaignDataStatus(campaignId),
    refetchInterval: 1000,
    // A campaign whose progress cannot be read (an older service has no such route) is not a
    // reason to fail the view being waited on — the label just falls back to the generic one.
    retry: false,
  })
  const progress = status.data?.progress ?? null
  const label = formatDataFetchLabel(status.data) ?? 'Running notebook…'
  const percent = progressPercent(progress)

  return (
    // The bar is pinned to the box's own edges rather than laid out inside the message, so its
    // full length reads as the full transfer — an inset bar makes "done" land short of the box
    // and understates itself at every value.
    <Alert
      severity="info"
      icon={<CircularProgress size={16} />}
      sx={{ maxWidth: 340, position: 'relative', pb: 1.5, overflow: 'hidden' }}
    >
      <Typography variant="body2">{label}</Typography>
      <LinearProgress
        {...(percent === null
          ? { variant: 'indeterminate' as const }
          : { variant: 'determinate' as const, value: percent })}
        sx={{ position: 'absolute', left: 0, right: 0, bottom: 0 }}
      />
    </Alert>
  )
}

// Fetches the executed notebook HTML for (node, workload) and renders it in an iframe via a Blob
// URL — a real navigation, so the nbconvert HTML's require.js / MathJax / inline plot scripts run
// (feeding a huge string through `srcdoc` would re-parse it every render).
function NotebookFrame({
  item,
  workload,
  level,
}: {
  item: ResultsTreeItem
  workload: string
  level: string
}) {
  // Render the notebook in the app's colour scheme so the iframe doesn't glare white in the dark UI.
  const mode = useTheme().palette.mode
  const html = useQuery({
    queryKey: [
      'notebook', item.campaignId, workload, level, item.configName ?? '', item.runId ?? '', mode,
    ],
    queryFn: () =>
      robovast.fetchNotebookHtml(item.campaignId, {
        workload,
        level,
        configName: item.configName,
        runId: item.runId,
        theme: mode,
      }),
    retry: false,
    staleTime: 5 * 60_000,
  })

  const [blobUrl, setBlobUrl] = useState('')
  useEffect(() => {
    if (!html.data) {
      setBlobUrl('')
      return
    }
    const url = URL.createObjectURL(new Blob([html.data], { type: 'text/html' }))
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [html.data])

  if (html.isPending) return <NotebookWait campaignId={item.campaignId} />
  if (html.isError)
    return (
      <Alert severity="error" variant="outlined">
        {(html.error as Error).message}
      </Alert>
    )

  return (
    <Box
      component="iframe"
      title={`${workload} — ${level}`}
      src={blobUrl}
      sx={{
        width: '100%',
        // Grow to fill the detail pane's remaining height (below the tab bar) instead of a fixed box.
        flex: 1,
        minHeight: 320,
        border: 0,
        borderRadius: 1,
        // Match the rendered notebook's own background so there's no light/dark flash on load.
        bgcolor: mode === 'dark' ? '#111111' : '#ffffff',
      }}
    />
  )
}
