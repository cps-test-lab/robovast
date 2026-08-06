// Results → Run view: a run-focused, time-driven dashboard. Pick one run of a postprocessed campaign
// and replay it through the panels its .vast declares (visualization.panels) over the rosbag timeline.
// This component is the glue: it resolves the run, builds the shared PlaybackClock and DataProvider for
// it, discovers the timeline range, and hands the parsed panel specs to the PanelHost. The panels
// themselves (playback bar, costmaps, scenario tree) are independent plugins.
//
// Two "dropdown dialogs" drive it: a Run picker (the shared Explorer campaign→config→run tree) and an
// Edit-visualization editor (Monaco, same style as the config editor) that saves the campaign's
// `visualization:` block as a .vast override and reloads the panels.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Paper from '@mui/material/Paper'
import Popover from '@mui/material/Popover'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import ArrowDropDownRoundedIcon from '@mui/icons-material/ArrowDropDownRounded'
import EditRoundedIcon from '@mui/icons-material/EditRounded'
import { robovast, type CampaignSummary } from '@/lib/robovastClient'
import type { ResultsTreeItem } from '@/lib/resultsTree'
import { PlaybackClock } from '@/lib/dashboard/clock'
import { dbDataProvider } from '@/lib/dashboard/dataProvider'
import { parseVastPanels } from '@/lib/dashboard/parseVastPanels'
import { PanelHost } from '@/lib/dashboard/PanelHost'
import { ResultsTree } from './ResultsTree'
import '@/panels' // registers the built-in panels

// Tables whose timestamp column can define the run's timeline; the union of their ranges is used.
// The fallback for a campaign whose timeline comes from postprocessed rosbag tables.
const TIME_TABLES = ['poses', 'behaviors', 'scenario_timestamps']

/** The `capture.path` a panel declares, if any -- the run's own time base, needing no `data.db`.
 *
 *  Read straight from the panel specs rather than plumbed up from the panel: the manifest is a small
 *  JSON at a URL the panel is about to fetch anyway, so the browser serves the second read from cache.
 */
function capturePathOf(panels: { type: string; config: Record<string, unknown> }[]): string | null {
  for (const panel of panels) {
    const capture = panel.config?.capture as { path?: unknown } | undefined
    if (capture) return String(capture.path ?? 'capture/capture.json')
  }
  return null
}

interface RunKey {
  config_name: string
  run_id: string
}

export function RunView({
  campaignId,
  campaigns,
  onCampaignChange,
}: {
  campaignId: string
  campaigns: CampaignSummary[]
  onCampaignChange: (campaignId: string) => void
}) {
  const queryClient = useQueryClient()

  const panels = useQuery({
    queryKey: ['panels', campaignId],
    queryFn: () => robovast.listCampaignPanels(campaignId),
    enabled: !!campaignId,
    retry: false,
    // Pick up out-of-band edits to the .vast (edited on disk, or via the editor) when the tab
    // regains focus — no manual browser refresh needed.
    refetchOnWindowFocus: true,
  })

  // `run_view`, not the postprocessed `runs` table: it is a temp view over the live `campaign.db`
  // (written as the campaign runs), so a campaign that produced no rosbags -- and therefore has no
  // `data.db` at all -- still lists its runs and can be replayed. `runs` carries the `param_*` columns
  // and nothing here needs them.
  const runs = useQuery({
    queryKey: ['runs-list', campaignId],
    queryFn: () =>
      robovast.queryCampaignDataSql(
        campaignId,
        'SELECT config_name, run_id, status, passed FROM run_view ORDER BY config_name, run_id',
      ),
    enabled: !!campaignId,
    retry: false,
  })

  const runList: RunKey[] = useMemo(
    () =>
      (runs.data?.rows ?? []).map((r) => ({
        config_name: String(r.config_name ?? ''),
        run_id: String(r.run_id ?? ''),
      })),
    [runs.data],
  )

  const [run, setRun] = useState<RunKey | null>(null)
  // Default to (and self-heal onto) the first run of the current campaign.
  useEffect(() => {
    if (!runList.length) {
      setRun(null)
      return
    }
    if (!runList.some((r) => r.config_name === run?.config_name && r.run_id === run?.run_id)) {
      setRun(runList[0])
    }
  }, [runList]) // eslint-disable-line react-hooks/exhaustive-deps

  const runKey = run ? `${campaignId}:${run.config_name}:${run.run_id}` : ''

  // One provider + clock per run. Recreated (and the old clock disposed) when the run changes.
  const provider = useMemo(
    () => (run ? dbDataProvider(campaignId, run.config_name, run.run_id) : null),
    [campaignId, run],
  )
  const clock = useMemo(() => new PlaybackClock(), [runKey])
  useEffect(() => () => clock.dispose(), [clock])

  const specs = useMemo(
    () => (panels.data ? parseVastPanels(panels.data.panels) : []),
    [panels.data],
  )

  // Discover the timeline range and set it on the clock, in order of authority:
  //   1. a run capture's own time base -- the run's ground truth, and available with no data.db;
  //   2. an explicit `visualization.timeline` (a sim's own table with a `t` column);
  //   3. the union of the standard postprocessed nav time tables.
  // Depend on scalars rather than object identity, which churns on every panels refetch.
  const tlTable = panels.data?.timeline?.table
  const tlCol = panels.data?.timeline?.time_column
  const capturePath = useMemo(() => capturePathOf(specs), [specs])
  useEffect(() => {
    if (!provider || !run) return
    let alive = true

    const fromCapture = async (): Promise<[number, number] | null> => {
      if (!capturePath) return null
      const res = await fetch(
        robovast.runFileUrl(campaignId, run.config_name, run.run_id, capturePath),
      )
      if (!res.ok) return null
      const manifest = (await res.json()) as { time?: { t0?: number; t1?: number } }
      const { t0, t1 } = manifest.time ?? {}
      return typeof t0 === 'number' && typeof t1 === 'number' ? [t0, t1] : null
    }

    fromCapture()
      .catch(() => null)
      .then(async (captured) => {
        if (captured) return [captured]
        const lookups: Promise<[number, number] | null>[] = tlTable
          ? [provider.timeRange(tlTable, tlCol).catch(() => null)]
          : TIME_TABLES.map((t) => provider.timeRange(t).catch(() => null))
        return Promise.all(lookups)
      })
      .then((ranges) => {
        if (!alive) return
        const valid = ranges.filter((r): r is [number, number] => !!r)
        if (!valid.length) return
        const lo = Math.min(...valid.map((r) => r[0]))
        const hi = Math.max(...valid.map((r) => r[1]))
        clock.setRange(lo, hi)
      })
    return () => {
      alive = false
    }
  }, [provider, clock, tlTable, tlCol, capturePath, campaignId, run])

  // `run_view` needs only campaign.db, so this now means the campaign has neither database -- it never
  // started, or died before its store was written. Postprocessing is no longer the thing to suggest.
  const noData = /campaign\.db/i.test((runs.error as Error | null)?.message ?? '')

  // The two dropdown dialogs are Popovers anchored to their trigger buttons.
  const [runAnchor, setRunAnchor] = useState<HTMLElement | null>(null)
  const [editAnchor, setEditAnchor] = useState<HTMLElement | null>(null)

  const pickRun = (item: ResultsTreeItem) => {
    // Only a run leaf resolves to a replayable run; campaigns/configs just expand in the tree.
    if (item.kind !== 'run' || item.runId == null) return
    if (item.campaignId !== campaignId) onCampaignChange(item.campaignId)
    setRun({ config_name: item.configName ?? '', run_id: String(item.runId) })
    setRunAnchor(null)
  }

  const onSaved = () => {
    setEditAnchor(null)
    // Reload the panels from the new effective .vast, and refresh the editor's cached source so a
    // reopen shows the saved text.
    queryClient.invalidateQueries({ queryKey: ['panels', campaignId] })
    queryClient.invalidateQueries({ queryKey: ['panels-source', campaignId] })
  }

  // Mirror buildCampaignChildren's run-node id so the current run highlights in the tree.
  const selectedTreeId = run
    ? `${campaignId}//cfg/${run.config_name}//run/${run.run_id}`
    : ''

  return (
    <Stack spacing={2} sx={{ height: 'calc(100vh - 72px)' }}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Run view</Typography>
        <Button
          variant="outlined"
          size="small"
          endIcon={<ArrowDropDownRoundedIcon />}
          onClick={(e) => setRunAnchor(e.currentTarget)}
          sx={{ textTransform: 'none', minWidth: 260, justifyContent: 'space-between' }}
        >
          {run ? `${run.config_name} · run ${run.run_id}` : 'Select run'}
        </Button>
        <Button
          variant="text"
          size="small"
          startIcon={<EditRoundedIcon />}
          endIcon={<ArrowDropDownRoundedIcon />}
          onClick={(e) => setEditAnchor(e.currentTarget)}
          disabled={!campaignId}
          sx={{ textTransform: 'none' }}
        >
          Edit visualization
        </Button>
      </Stack>

      <Popover
        open={!!runAnchor}
        anchorEl={runAnchor}
        onClose={() => setRunAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
      >
        {/* Sized to the longest campaign id rather than to a fixed 400px, which cut them off.
            Bounded by the viewport, with the tree's own ellipsis as the last resort. */}
        <Box
          sx={{
            width: 'max-content',
            minWidth: 400,
            maxWidth: 'min(900px, 90vw)',
            maxHeight: 460,
            overflow: 'auto',
            p: 1,
          }}
        >
          <ResultsTree
            campaigns={campaigns}
            selectedId={selectedTreeId}
            onSelect={pickRun}
          />
        </Box>
      </Popover>

      <Popover
        open={!!editAnchor}
        anchorEl={editAnchor}
        onClose={() => setEditAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
      >
        {campaignId ? (
          <VisualizationEditor
            campaignId={campaignId}
            onClose={() => setEditAnchor(null)}
            onSaved={onSaved}
          />
        ) : null}
      </Popover>

      {!campaignId ? (
        <Alert severity="info" variant="outlined">
          Pick a run to replay.
        </Alert>
      ) : panels.isPending || runs.isPending ? (
        <CircularProgress size={24} />
      ) : noData ? (
        <Alert severity="info" variant="outlined">
          This campaign has no store to read: no <code>campaign.db</code> and no{' '}
          <code>data.db</code>, so it either never started or ended before recording anything.
        </Alert>
      ) : !specs.length ? (
        <Alert severity="info" variant="outlined">
          This campaign's <code>.vast</code> declares no <code>visualization.panels</code>. Use{' '}
          <b>Edit visualization</b> to add a <code>visualization:</code> block.
        </Alert>
      ) : !provider ? (
        <Alert severity="info" variant="outlined">
          This campaign has no runs to replay.
        </Alert>
      ) : (
        <Box sx={{ flexGrow: 1, minHeight: 0 }}>
          <PanelHost key={runKey} panels={specs} clock={clock} data={provider} />
        </Box>
      )}
    </Stack>
  )
}

// The 'edit visualization' dropdown: loads the campaign's `visualization:` block, edits it in Monaco
// (same style as the config editor), and on Save writes a .vast override, then reloads the panels.
// Save is enabled only when the text actually changed — reloading is otherwise pointless.
function VisualizationEditor({
  campaignId,
  onClose,
  onSaved,
}: {
  campaignId: string
  onClose: () => void
  onSaved: () => void
}) {
  const src = useQuery({
    queryKey: ['panels-source', campaignId],
    queryFn: () => robovast.getPanelsSource(campaignId),
    enabled: !!campaignId,
    retry: false,
  })

  const [text, setText] = useState<string | null>(null)
  // Load the fetched source into the buffer (and reset the buffer on reopen / after a save).
  useEffect(() => {
    if (src.data) setText(src.data.content)
  }, [src.data])

  const original = src.data?.content ?? ''
  const changed = text != null && text !== original

  const save = useMutation({
    mutationFn: () => robovast.updatePanelsSource(campaignId, text ?? ''),
    onSuccess: onSaved,
  })

  return (
    <Stack spacing={1} sx={{ width: 680, p: 1.5 }}>
      <Typography variant="subtitle2">
        Edit visualization
      </Typography>
      {src.isError ? <Alert severity="error">{(src.error as Error).message}</Alert> : null}
      <Paper variant="outlined" sx={{ height: 380, overflow: 'hidden' }}>
        <Editor
          height="380px"
          language="yaml"
          path={`${campaignId}.visualization.vast`}
          value={text ?? ''}
          onChange={(v) => setText(v ?? '')}
          theme="vs-dark"
          options={{
            minimap: { enabled: false },
            fontSize: 13,
            scrollBeyondLastLine: false,
            readOnly: src.isPending || save.isPending,
          }}
        />
      </Paper>
      {save.isError ? <Alert severity="error">{(save.error as Error).message}</Alert> : null}
      <Stack direction="row" spacing={1} justifyContent="flex-end">
        <Button size="small" onClick={onClose} disabled={save.isPending}>
          Cancel
        </Button>
        <Button
          size="small"
          variant="contained"
          onClick={() => save.mutate()}
          disabled={!changed || save.isPending}
        >
          Save
        </Button>
      </Stack>
    </Stack>
  )
}
