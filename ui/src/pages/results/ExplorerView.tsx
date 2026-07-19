import { useEffect, useMemo, useState, type SyntheticEvent } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import Typography from '@mui/material/Typography'
import { useTheme } from '@mui/material/styles'
import { RichTreeView } from '@mui/x-tree-view/RichTreeView'
import { robovast, campaignsNewestFirst, type CampaignSummary } from '@/lib/robovastClient'
import {
  buildCampaignChildren,
  campaignItem,
  indexById,
  placeholderChild,
  type ResultsTreeItem,
} from '@/lib/resultsTree'
import { StatusTreeItem } from './StatusTreeItem'

// The per-run breakdown lives only in data.db; pull the pass/fail matrix in one query per campaign.
const RUNS_SQL = 'SELECT config_name, run_id, status, passed FROM runs ORDER BY config_name, run_id'

// Results → Explorer: a campaign → config → run tree with green/red status, all from the DB. The
// tree is the campaign selector; clicking a node renders its evaluation.visualization notebooks.
export function ExplorerView({ campaigns }: { campaigns: CampaignSummary[] }) {
  const sorted = useMemo(() => campaignsNewestFirst(campaigns), [campaigns])
  const byId = useMemo(
    () => new Map(sorted.map((c) => [c.campaign_id, c])),
    [sorted],
  )

  const [expandedItems, setExpandedItems] = useState<string[]>([])
  const [selectedId, setSelectedId] = useState<string>('')

  // Lazy-load each expanded campaign's runs (config ids also land in expandedItems, so filter to
  // real, postprocessed campaigns). A single query per campaign feeds its whole subtree.
  const expandedCampaigns = expandedItems.filter((id) => byId.get(id)?.postprocessed)
  const runQueries = useQueries({
    queries: expandedCampaigns.map((id) => ({
      queryKey: ['runs', id],
      queryFn: () => robovast.queryCampaignDataSql(id, RUNS_SQL),
      retry: false,
      staleTime: 60_000,
    })),
  })
  const runByCampaign = new Map(expandedCampaigns.map((id, i) => [id, runQueries[i]]))

  // Every campaign carries children so its expand arrow shows; the children are the real subtree
  // once loaded, otherwise a single placeholder (loading / no-data hint).
  const items: ResultsTreeItem[] = sorted.map((c) => {
    const base = campaignItem(c)
    let children: ResultsTreeItem[]
    if (!c.postprocessed) {
      children = [placeholderChild(c.campaign_id, 'No results yet — run postprocessing')]
    } else {
      const q = runByCampaign.get(c.campaign_id)
      if (!q || q.isPending) {
        children = [placeholderChild(c.campaign_id, 'Loading…')]
      } else if (q.isError) {
        const msg = (q.error as Error).message
        children = [
          placeholderChild(
            c.campaign_id,
            /data\.db/i.test(msg) ? 'No results yet — run postprocessing' : msg,
          ),
        ]
      } else {
        const built = buildCampaignChildren(c.campaign_id, q.data.rows)
        children = built.length ? built : [placeholderChild(c.campaign_id, 'No runs recorded')]
      }
    }
    return { ...base, children }
  })

  const itemsById = useMemo(() => indexById(items), [items])
  const selected = selectedId ? itemsById.get(selectedId) : undefined

  const handleItemClick = (_e: SyntheticEvent, itemId: string) => {
    const item = itemsById.get(itemId)
    if (item && item.kind !== 'placeholder') setSelectedId(itemId)
  }

  return (
    <Stack spacing={2}>
      <Typography variant="h6">Explorer</Typography>

      {!sorted.length ? (
        <Alert severity="info" variant="outlined">
          No campaigns yet — launch one from the Launcher.
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
          <Paper sx={{ p: 1, overflow: 'auto', minHeight: 0 }}>
            <RichTreeView
              items={items}
              slots={{ item: StatusTreeItem }}
              expandedItems={expandedItems}
              onExpandedItemsChange={(_e, ids) => setExpandedItems(ids)}
              selectedItems={selectedId || null}
              onItemClick={handleItemClick}
            />
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

  if (html.isPending)
    return (
      <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 2 }}>
        <CircularProgress size={18} />
        <Typography variant="caption" color="text.secondary">
          Running notebook…
        </Typography>
      </Stack>
    )
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
