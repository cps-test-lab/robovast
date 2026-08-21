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
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import ClearRoundedIcon from '@mui/icons-material/ClearRounded'
import SearchRoundedIcon from '@mui/icons-material/SearchRounded'
import { useTheme } from '@mui/material/styles'
import { robovast, hasRecordedRuns, type CampaignSummary } from '@/lib/robovastClient'
import { formatDataFetchLabel, progressPercent } from '@/lib/format'
import {
  resolveSelection,
  selectionNodeId,
  selectionOf,
  type ResultsTreeItem,
} from '@/lib/resultsTree'
import { LOG_TAB_SLUG, type ResultsSel } from '@/lib/hashNav'
import { openResultsView } from '@/lib/nav'
import { RunViewIcon } from '@/components/viewIcons'
import { RunLogTab, type LogTabScope } from '@/components/runLog/RunLogTab'
import { ResultsTree, runsQuery } from './ResultsTree'
import { RefreshResultsButton, type ResultsRefresh } from './RefreshResultsButton'

// Results → Explorer: a campaign → config → run tree with green/red status, all from the DB. The
// tree is the campaign selector; clicking a node renders its evaluation.visualization notebooks.
export function ExplorerView({
  active,
  campaignId,
  campaigns,
  sel,
  tab,
  onResultsChange,
  refresh,
}: {
  /** This view is the one on screen. Every Results view stays mounted once visited, so a hidden one
   *  must not write the shared selection out from under the visible one. */
  active: boolean
  campaignId: string
  campaigns: CampaignSummary[]
  sel: ResultsSel
  tab: string
  onResultsChange: (campaignId: string, sel: ResultsSel, tab: string) => void
  refresh: ResultsRefresh
}) {
  const [filter, setFilter] = useState('')

  // Both the campaign *and* the node below it are in the URL, and shared with the Run view — so a
  // link addresses a result rather than the campaign that produced it, and picking a run here is
  // the run the Run view replays. This view holds no selection of its own; it reads the one it is
  // given and reports what was clicked.
  const commit = (nextSel: ResultsSel, nextTab: string) => {
    if (active) onResultsChange(campaignId, nextSel, nextTab)
  }

  // Whether this campaign actually has the config/run the URL names, and which round proposed it.
  // The rows are the tree's own query (same key, so this is served from its cache), and a finished
  // campaign's are fixed — so this is a derivation, not something to keep watching.
  const campaign = campaigns.find((c) => c.campaign_id === campaignId)
  const rows = useQuery({ ...runsQuery(campaignId), enabled: !!campaignId })
  const resolved = useMemo(
    () => resolveSelection(rows.data?.rows ?? [], campaign?.mode === 'search', sel),
    [rows.data, campaign?.mode, sel],
  )
  // A URL naming a config or run this campaign does not have is a wrong link, not a stale one:
  // there is nothing to wait for, so it falls back to the campaign node once the rows are in.
  useEffect(() => {
    if (rows.data && resolved.sel !== sel) commit(resolved.sel, tab)
  }, [rows.data, resolved.sel, active]) // eslint-disable-line react-hooks/exhaustive-deps

  // A filter left over from an earlier search would hide the campaign that was just asked for,
  // leaving "No campaign matches …" where the selected node should be.
  useEffect(() => {
    if (!campaignId) return
    setFilter((f) => (campaignId.toLowerCase().includes(f.trim().toLowerCase()) ? f : ''))
  }, [campaignId])

  const select = (item: ResultsTreeItem) => {
    // A click in another campaign's subtree moves both at once: the campaign and the node under it
    // are one selection, and setting them in two steps would blank the node in between.
    onResultsChange(item.campaignId, selectionOf(item), item.campaignId === campaignId ? tab : '')
  }

  // Campaign ids only: configs and runs are lazy-loaded per expanded campaign, so matching them
  // would hide branches whose children simply have not been fetched yet.
  const needle = filter.trim().toLowerCase()
  const shown = useMemo(
    () => (needle ? campaigns.filter((c) => c.campaign_id.toLowerCase().includes(needle)) : campaigns),
    [campaigns, needle],
  )

  return (
    <Stack
      spacing={2}
      // `gap`, not Stack's margin spacing, for the same reason as the Run view: the margin rule's
      // specificity beats a child's own sx.
      useFlexGap
      // 48px is App's `p: 3` on the main Box, top + bottom, so the view fills the window exactly —
      // same measure as the Run view. The heading keeps its natural height and the grid below takes
      // the rest, rather than the whole block guessing at a fixed offset.
      sx={{ height: 'calc(100vh - 48px)' }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ flexShrink: 0 }}>
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
            // Take whatever the heading leaves so both columns run to the bottom of the window;
            // each column scrolls internally rather than growing the page.
            flex: 1,
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
                  selectedId={campaignId ? selectionNodeId(campaignId, resolved.sel, resolved.batch) : ''}
                  onSelect={select}
                />
              ) : (
                <Typography variant="caption" color="text.secondary" sx={{ pl: 1 }}>
                  No campaign matches “{filter}”.
                </Typography>
              )}
            </Box>
          </Paper>

          <SelectionDetail
            campaignId={campaignId}
            sel={resolved.sel}
            tab={tab}
            onTab={(next) => commit(resolved.sel, next)}
            canReplay={!!campaign && hasRecordedRuns(campaign)}
          />
        </Box>
      )}
    </Stack>
  )
}

// Right pane: the selected node's evaluation.visualization notebooks, executed for this node and
// shown as HTML — the web equivalent of the desktop `vast eval gui`.
function SelectionDetail(props: NodeProps & { tab: string; onTab: (tab: string) => void; canReplay: boolean }) {
  if (!props.campaignId) {
    return (
      <Alert severity="info" variant="outlined">
        Select a campaign, batch, config, or run to see its visualizations.
      </Alert>
    )
  }
  return (
    <Paper
      sx={{ p: 2, minHeight: 0, overflow: 'auto', display: 'flex', flexDirection: 'column' }}
    >
      <NotebookPanel {...props} />
    </Paper>
  )
}

/** The selected node, as the right pane needs it: every field of it comes from the URL, so the
 *  notebooks and the log render without waiting for the tree to load its rows. */
interface NodeProps {
  campaignId: string
  sel: ResultsSel
}

/** The node components a notebook request (and its cache key) is made of. A level's absent fields
 *  are not part of its address, so they go as `undefined` rather than as an invented empty value —
 *  and the union is what guarantees they *are* absent rather than merely unused. */
function nodeParams(sel: ResultsSel) {
  return {
    level: sel.level,
    configName: sel.level === 'config' || sel.level === 'run' ? sel.configName : undefined,
    runId: sel.level === 'run' ? sel.runId : undefined,
    batch: sel.level === 'batch' ? sel.batch : undefined,
  }
}

// The selected node's tabs: one per evaluation.visualization workload that declares a notebook
// for this node's level (campaign/batch/config/run), then the built-in Log. The active notebook
// is executed server-side and rendered as HTML.
//
//: The **Log** tab is appended after whatever the campaign declared. Not a workload and not
//: declarable: a run always has a log, so there is nothing for a `.vast` to decide. It needs no
//: disambiguation in the URL, where it is spelled `LOG_TAB_SLUG`, because a workload may not be
//: *named* `log`: two tabs reading the same is refused where notebooks are declared
//: (`ExplorerConfig`), which is where the mistake is, rather than guessed at here.
//:
//: **Run level only.** A config or campaign node has no single log -- what it has is a *search*
//: across its runs, which is a different view with different controls, and offering the same tab
//: for both made the tab mean two things. The cross-run question is answered by
//: `search_run_logs` (MCP) and by SQL over `run_log` in the Data browser, where a query that
//: spans runs belongs.
function NotebookPanel({
  campaignId,
  sel,
  tab,
  onTab,
  canReplay,
}: NodeProps & { tab: string; onTab: (tab: string) => void; canReplay: boolean }) {
  // The selection's levels are the backend's level names, so this needs no translation — a
  // 'batch' node asks for the campaign's `batch:` notebook.
  const { level, configName, runId } = nodeParams(sel)

  const vis = useQuery({
    queryKey: ['visualizations', campaignId],
    queryFn: () => robovast.listCampaignVisualizations(campaignId),
    retry: false,
    staleTime: 60_000,
  })

  const workloads = (vis.data?.workloads ?? []).filter((w) => w.levels.includes(level))
  const names = workloads.map((w) => w.name).join(',')

  // Only a run has one log to show, and only a run is replayable.
  const showLog = sel.level === 'run'

  // Keep the open tab valid as the selection (and thus the applicable workloads) changes. The Log
  // tab is always valid at run level, so it is also the fallback when a node declares no notebook --
  // a node with nothing to show is a worse answer than its log. A finished campaign's workloads do
  // not change, so this settles once per selection rather than watching anything.
  useEffect(() => {
    if (tab === LOG_TAB_SLUG && showLog) return
    if (!workloads.some((w) => w.name === tab))
      onTab(workloads[0]?.name ?? (showLog ? LOG_TAB_SLUG : ''))
  }, [names, tab, showLog]) // eslint-disable-line react-hooks/exhaustive-deps

  const logScope: LogTabScope = { campaignId, level, configName, runId }

  // A failed workload list is reported *beside* the tabs rather than instead of them: the log
  // does not depend on it, and hiding a working view because an unrelated request failed is
  // worse than saying both.
  return (
    <Stack spacing={1} sx={{ flex: 1, minHeight: 0 }}>
      {vis.isError ? (
        <Alert severity="warning" variant="outlined" sx={{ py: 0 }}>
          {(vis.error as Error).message}
        </Alert>
      ) : null}
      {/* The tabs scroll; the jump to the Run view is pinned to the right of them, so it stays
          reachable however many workloads a campaign declares. Same icon and gate as the campaign
          card's shortcut, because it is the same destination. */}
      <Stack direction="row" alignItems="center" sx={{ flexShrink: 0 }}>
        <Tabs
          value={tab}
          onChange={(_e, v) => onTab(v)}
          variant="scrollable"
          scrollButtons="auto"
          sx={{ minHeight: 36, flex: 1, minWidth: 0 }}
        >
          {workloads.map((w) => (
            <Tab key={w.name} value={w.name} label={w.name} sx={{ minHeight: 36, py: 0 }} />
          ))}
          {showLog ? <Tab value={LOG_TAB_SLUG} label="Log" sx={{ minHeight: 36, py: 0 }} /> : null}
        </Tabs>
        {sel.level === 'run' && canReplay ? (
          <Tooltip title="Replay this run in the Run view">
            <IconButton
              size="small"
              aria-label="open run view"
              sx={{ flexShrink: 0 }}
              onClick={() => openResultsView('run', campaignId, sel)}
            >
              <RunViewIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null}
      </Stack>
      {vis.isPending && tab !== LOG_TAB_SLUG ? <CircularProgress size={18} /> : null}
      {!workloads.length && !showLog && !vis.isPending ? (
        <Typography variant="caption" color="text.secondary">
          No notebook visualizations declared for this {level}. Select a run to read its log.
        </Typography>
      ) : null}
      {tab === LOG_TAB_SLUG && showLog ? (
        <RunLogTab scope={logScope} />
      ) : tab ? (
        <NotebookFrame campaignId={campaignId} sel={sel} workload={tab} />
      ) : null}
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
function NotebookFrame({ campaignId, sel, workload }: NodeProps & { workload: string }) {
  // Render the notebook in the app's colour scheme so the iframe doesn't glare white in the dark UI.
  const mode = useTheme().palette.mode
  const node = nodeParams(sel)
  const html = useQuery({
    // `batch` is part of the key, not just the request: two batch nodes of one campaign agree on
    // every other component (a batch has no config or run of its own), so without it they would
    // share one cache entry and every round would show the first one's notebook.
    queryKey: [
      'notebook', campaignId, workload, node.level, node.configName ?? '', node.runId ?? '',
      node.batch ?? '', mode,
    ],
    queryFn: () =>
      robovast.fetchNotebookHtml(campaignId, { workload, ...node, theme: mode }),
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

  if (html.isPending) return <NotebookWait campaignId={campaignId} />
  if (html.isError)
    return (
      <Alert severity="error" variant="outlined">
        {(html.error as Error).message}
      </Alert>
    )

  return (
    <Box
      component="iframe"
      title={`${workload} — ${node.level}`}
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
