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
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useToasts } from '@/components/ToastProvider'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Divider from '@mui/material/Divider'
import Paper from '@mui/material/Paper'
import Popover from '@mui/material/Popover'
import IconButton from '@mui/material/IconButton'
import ListItemIcon from '@mui/material/ListItemIcon'
import ListItemText from '@mui/material/ListItemText'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import ArrowDropDownRoundedIcon from '@mui/icons-material/ArrowDropDownRounded'
import CenterFocusStrongRoundedIcon from '@mui/icons-material/CenterFocusStrongRounded'
import EditRoundedIcon from '@mui/icons-material/EditRounded'
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded'
import { robovast, hasRecordedRuns, isPreviewable, type CampaignSummary } from '@/lib/robovastClient'
import { declaresScene3d } from '@/lib/previewRuns'
import {
  firstRunSelection,
  resolveSelection,
  selectionNodeId,
  selectionOf,
  type ResultsTreeItem,
} from '@/lib/resultsTree'
import { CAMPAIGN_SEL, type ResultsSel } from '@/lib/hashNav'
import { openResultsView } from '@/lib/nav'
import { ExplorerIcon } from '@/components/viewIcons'
import { PlaybackClock, useClock } from '@robovast/panel-kit'
import { dbDataProvider } from '@/lib/panels/dataProvider'
import { parsePanels } from '@/lib/panels/parsePanels'
import { PanelHost } from '@/lib/panels/PanelHost'
import { ResultsTree, runsQuery } from './ResultsTree'
import { RefreshResultsButton, type ResultsRefresh } from './RefreshResultsButton'
import { DEFAULT_CAPTURE_PATH } from '@/panels/run_view/Scene3DPanel'
import { resetSceneViews, useSceneResetAvailable } from '@/panels/run_view/sceneReset'
import '@/panels/run_view' // registers the built-in panels

// Tables whose timestamp column can define the run's timeline; the union of their ranges is used.
// The fallback for a campaign whose timeline comes from postprocessed rosbag tables.
const TIME_TABLES = ['poses', 'behaviors', 'scenario_timestamps']

// What a PREVIEW mounts: the panels that read a run's own artifacts rather than the index. A running
// campaign has no rows there, so every other panel would issue a query per run change that cannot
// answer -- and on the cluster the first of them fetches databases from the object store inside the
// request, against the very service that is driving the campaign. They are left out rather than
// mounted empty: a wall of identical errors says less than the one sentence on the preview chip.
//
// `scene3d` replays from `capture/capture.json`, which the simulator writes at the run's clean stop.
// `playback` is the transport: not content, but the clock the other panels follow, so dropping it
// would leave the scene with nothing driving it. It is the whole of the service's ALWAYS_ON_PANELS
// -- a second always-on panel added there belongs here too, or it would be contributed to every run
// view and then silently missing from this one.
const PREVIEW_PANELS: ReadonlySet<string> = new Set(['scene3d', 'playback'])

/** The run view's settings menu: does the run end at its scenario's verdict or run on through the
 *  teardown, and -- when a 3D view is mounted -- put its camera back where the scene opened.
 *
 *  The setting lives here rather than in the playback bar or the log panel because it is not
 *  either panel's -- it says what "this run" means, and both of them follow. The state rides on
 *  the clock because that is the only object every panel already receives, and because the
 *  question is a time one: the timeline ends at the verdict unless the shutdown phase is shown.
 *
 *  The reset is here for the mirror-image reason: the camera *is* one panel's, but the panel is
 *  frameless by design (it is the full-bleed base layer, so it carries no header to hang a button
 *  in), and a control floating over the world would sit in front of the thing it acts on. It
 *  reaches the viewport through the small registry in panels/run_view/sceneReset.ts.
 *
 *  A gear rather than the setting's own icon, matching the campaign row's menu: the header is a
 *  row of labelled controls, and each further view-wide setting would otherwise add another bare
 *  icon to decode. The menu names them in words instead, and grows without widening the header.
 *
 *  The entry names the span it adds -- the shutdown phase, the word the playback bar and the docs
 *  already use for it -- and is ticked while that span is included, so the label says what a click
 *  does and the tick says where it stands, rather than a title that flips between two sentences
 *  and can only be found by hovering. */
function RunSettingsMenu({ clock }: { clock: PlaybackClock }) {
  const { verdict, hideShutdown } = useClock(clock)
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const canResetView = useSceneResetAvailable()
  const noVerdict = verdict == null
  const reason = noVerdict
    ? 'This run recorded no scenario verdict, so there is no shutdown to separate.'
    : 'The timeline and the log run to the end of the recording rather than stopping at the '
      + 'scenario\'s verdict.'
  return (
    <>
      <Tooltip title="Run view settings">
        <IconButton
          size="small"
          aria-label="run view settings"
          onClick={(e) => setAnchor(e.currentTarget)}
        >
          <SettingsRoundedIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
        <Tooltip title={reason} placement="left">
          {/* A disabled item fires no events, so the tooltip needs a wrapper that does --
              which is exactly the case where the reason matters most. */}
          <span>
            <MenuItem
              disabled={noVerdict}
              onClick={() => {
                clock.setHideShutdown(!hideShutdown)
                setAnchor(null)
              }}
            >
              {/* A checkbox rather than MUI's `selected` tint. This is a setting, not an
                  action like the campaign menu's entries, and the tint is a background shade
                  a reader has to already know the meaning of -- the only other row here is an
                  action, so there is no second setting to compare a shade against. An empty box
                  says both that the entry toggles and that it is currently off, before anything
                  is clicked. */}
              <ListItemIcon>
                <Checkbox
                  size="small"
                  checked={!noVerdict && !hideShutdown}
                  disabled={noVerdict}
                  disableRipple
                  tabIndex={-1}      /* the MenuItem itself takes the focus and the click */
                  sx={{ p: 0 }}      /* no edge offset: ListItemIcon already sets the gutter */
                />
              </ListItemIcon>
              <ListItemText>Include shutdown phase</ListItemText>
            </MenuItem>
          </span>
        </Tooltip>
        {/* An action below the setting, with the divider saying which is which: the entry above
            leaves a tick behind, this one happens and is over. The two share the icon gutter, so a
            glyph where the neighbour has a checkbox is itself the difference between them -- no
            second reading needed once you know which row you are on. */}
        <Divider />
        <Tooltip
          title={
            canResetView
              ? 'Put the 3D camera back where the scene opened.'
              : 'This view has no 3D scene panel, so there is no camera to re-frame.'
          }
          placement="left"
        >
          <span>
            <MenuItem
              disabled={!canResetView}
              onClick={() => {
                resetSceneViews()
                setAnchor(null)
              }}
            >
              <ListItemIcon>
                <CenterFocusStrongRoundedIcon fontSize="small" />
              </ListItemIcon>
              <ListItemText>Reset 3D view</ListItemText>
            </MenuItem>
          </span>
        </Tooltip>
      </Menu>
    </>
  )
}

/** The run capture a panel replays, if any -- the run's own time base, needing no postprocessed data.
 *
 *  Read straight from the panel specs rather than plumbed up from the panel: the manifest is a small
 *  JSON at a URL the panel is about to fetch anyway, so the browser serves the second read from cache.
 *
 *  A `scene3d` panel counts even when it declares no `capture:` block, which is the documented complete
 *  form of it -- geometry resolves from the world the capture names, so there is nothing to bind. Keying
 *  this on a declared block alone meant the canonical `- scene3d:` fell through to the postprocessed
 *  tables below, and a campaign with no postprocessed tables -- the case the capture time base exists
 *  for -- got no
 *  range at all and never animated.
 */
function capturePathOf(panels: { type: string; config: Record<string, unknown> }[]): string | null {
  for (const panel of panels) {
    const capture = panel.config?.capture as { path?: unknown } | undefined
    if (capture || panel.type === 'scene3d') {
      return String(capture?.path ?? DEFAULT_CAPTURE_PATH)
    }
  }
  return null
}

export function RunView({
  active,
  campaignId,
  campaigns,
  sel,
  onResultsChange,
  refresh,
}: {
  /** This view is the one on screen. Every Results view stays mounted once visited, so a hidden one
   *  must not heal its own default over the node the visible one is showing. */
  active: boolean
  campaignId: string
  campaigns: CampaignSummary[]
  /** The node shared with the Explorer (see `Nav.sel`). Only a run is replayable; anything else
   *  arriving from over there is healed onto this campaign's first run below. */
  sel: ResultsSel
  onResultsChange: (campaignId: string, sel: ResultsSel, tab: string) => void
  refresh: ResultsRefresh
}) {
  const queryClient = useQueryClient()

  // Whether the run picker is open. Up here with the queries rather than with the Popover it drives,
  // because a preview's run listing is re-read only while somebody is looking at it.
  const [runAnchor, setRunAnchor] = useState<HTMLElement | null>(null)

  // A campaign is offered as a PREVIEW only if it has a 3D scene to replay. A preview shows nothing
  // else — every other panel needs the index — so a campaign whose simulator records no capture has
  // literally nothing to show until it finishes, and listing it would be an invitation to an empty
  // view. Whether it has one is in its served panel list, so ask for that; the key is the one the
  // panels query below uses, so the selected campaign is fetched once, not twice.
  //
  // Only campaigns that are actually previewable are asked about — a handful at most, since they
  // are the ones running right now.
  const previewCandidates = useMemo(() => campaigns.filter(isPreviewable), [campaigns])
  const previewPanels = useQueries({
    queries: previewCandidates.map((c) => ({
      queryKey: ['panels', c.campaign_id],
      queryFn: () => robovast.listCampaignPanels(c.campaign_id),
      enabled: active,
      retry: false,
      staleTime: 60_000,
    })),
  })
  // Pending counts as "not yet", so a campaign appears once it is known to have a scene rather than
  // appearing and then vanishing.
  const previewable = useMemo(
    () => new Set(previewCandidates
      .filter((_, i) => declaresScene3d(previewPanels[i]?.data?.panels) === true)
      .map((c) => c.campaign_id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [previewCandidates, previewPanels.map((q) => q.status).join(',')],
  )

  // Only campaigns that recorded runs can be replayed, so they are the only ones this view offers —
  // the picker never lists a campaign whose store was never written, and a selection inherited from
  // another Results view (the campaign is shared) is treated as no selection here rather than
  // queried into a "no store to read" error.
  const replayable = useMemo(
    () => campaigns.filter((c) =>
      hasRecordedRuns(c) && (!isPreviewable(c) || previewable.has(c.campaign_id))),
    [campaigns, previewable],
  )
  const available = !!campaignId && replayable.some((c) => c.campaign_id === campaignId)
  const summary = replayable.find((c) => c.campaign_id === campaignId)

  // A campaign that is still RUNNING is shown as a **preview**: its finished runs replay from their
  // own capture files, and nothing that needs the index works, because a running campaign has no rows
  // there at all. Derived from the summary rather than passed in, so nobody has to keep a flag in
  // step with the campaign on screen.
  const preview = !!summary && isPreviewable(summary)

  const panels = useQuery({
    queryKey: ['panels', campaignId],
    queryFn: () => robovast.listCampaignPanels(campaignId),
    // `active` for the same reason as `refetchOnWindowFocus` below, one level in: this view is
    // kept mounted, so arriving at it is the other moment an out-of-band edit should be picked up.
    // The layout is a small read of the campaign's declared panels — unlike the run data beneath
    // it, which is immutable and expensive and is deliberately left ungated.
    enabled: active && available,
    retry: false,
    // Pick up out-of-band edits to the .vast (edited on disk, or via the editor) when the tab
    // regains focus — no manual browser refresh needed.
    refetchOnWindowFocus: true,
  })

  // The same query the picker's tree runs (see `runsQuery`), so both read one set of rows and
  // react-query serves them from a single fetch. Why `run_view` rather than the postprocessed
  // `runs` table is documented on `CAMPAIGN_RUNS_SQL`; for a campaign still running, `runsQuery`
  // answers from its output directories instead, in the same row shape.
  //
  // A preview's rows GROW while it is open, so they are re-read — but only while the picker is
  // actually open, the same "poll while somebody is looking" gate the tree uses for its data-status
  // probe. Growth only ever appends a run, so a refresh cannot move the selection out from under a
  // reader; and once the campaign finishes, the key changes and the indexed rows are fetched.
  const runs = useQuery({
    ...runsQuery(summary),
    enabled: available,
    refetchInterval: preview && !!runAnchor ? 5_000 : false,
  })

  // The batch is only meaningful when the picker's tree groups by it; for a batch-mode campaign
  // it stays null so the tree id built from it is the ungrouped one.
  // Never grouped in preview: a batch is a round the index records, not a directory, so the listing
  // cannot see one and every row carries a null batch.
  const grouped = !preview && summary?.mode === 'search'
  const rows = runs.data?.rows ?? []

  // Only a run the *current* campaign actually has counts as the run on screen. The selection is
  // shared with the Explorer, so switching campaign leaves the previous one in it for a moment —
  // this campaign's rows are a request away — and rendering it meanwhile would show the run someone
  // had just been looking at as though it belonged to the campaign they clicked, with panels
  // quietly querying ids the new campaign does not have. Resolving against the rows answers both
  // questions at once: is it here, and which round is it in.
  const resolved = useMemo(() => resolveSelection(rows, grouped, sel), [rows, grouped, sel])
  const run = resolved.sel.level === 'run' ? resolved.sel : null

  // Default to (and self-heal onto) the first run of the campaign. This view can only replay a run,
  // so a campaign node — or a config or batch handed over from the Explorer — is not something it
  // can show; it picks the first run and says so in the URL rather than sitting empty.
  const firstRun = useMemo(() => firstRunSelection(rows), [rows])
  useEffect(() => {
    if (!active || !runs.data || run) return
    onResultsChange(campaignId, firstRun ?? CAMPAIGN_SEL, '')
  }, [active, runs.data, run, firstRun, campaignId]) // eslint-disable-line react-hooks/exhaustive-deps

  const runKey = run ? `${campaignId}:${run.configName}:${run.runId}` : ''

  // One provider + clock per run. Recreated (and the old clock disposed) when the run changes.
  const provider = useMemo(
    () => (run ? dbDataProvider(campaignId, run.configName, run.runId) : null),
    [campaignId, run],
  )
  const clock = useMemo(() => new PlaybackClock(), [runKey])
  useEffect(() => () => clock.dispose(), [clock])

  const specs = useMemo(
    () => {
      const parsed = panels.data ? parsePanels(panels.data.panels) : []
      return preview ? parsed.filter((p) => PREVIEW_PANELS.has(p.type)) : parsed
    },
    [panels.data, preview],
  )
  // The served list is never empty -- the playback transport is contributed for every campaign --
  // and the transport is not content: it is the clock the other panels follow. So "nothing to look
  // at here" is the service's `transport_only`, asked where the contributed panels are merged in
  // rather than by filtering the served list here, which would mean spelling the contributed types
  // a second time. A capture-recording simulator's scene3d is a real panel, so a roqsim campaign
  // that declares nothing is not bare.
  const bare = !!panels.data?.transport_only

  // Discover the timeline range and set it on the clock, in order of authority:
  //   1. a run capture's own time base -- the run's ground truth, available without postprocessing;
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
        robovast.runFileUrl(campaignId, run.configName, run.runId, capturePath),
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
        // Nothing else to ask in a preview: the tables below live in the index, which holds no rows
        // for a running campaign. The capture above is the whole timeline, and a run whose capture
        // is not written yet has no range rather than a wrong one.
        if (preview) return []
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

    // Where the *trial* ended, which the range above deliberately does not encode: `setRange`
    // is the whole recording, so showing the shutdown phase restores it without re-querying.
    //
    // Asked for separately rather than picked out of the lookups above, because those are
    // skipped entirely when a run capture supplies the range -- and a run replayed from a
    // capture should still stop at its verdict. One extra query, only on a run change.
    // Not in a preview: the verdict is a postprocessed table, so this could only fail, once per run
    // change. The toggle then reports nothing to trim, which is exactly true -- where the trial
    // ended is not knowable until the campaign has been postprocessed.
    if (preview) clock.setVerdict(null)
    else provider
      .timeRange('scenario_timestamps')
      .catch(() => null)
      .then((range) => {
        // `[t, t]`: one row per run. Null for a run that reached no verdict, and for a
        // campaign postprocessed before the verdict was recorded -- the toggle then says
        // there is nothing to trim rather than trimming to an invented moment.
        if (alive) clock.setVerdict(range ? range[1] : null)
      })
    return () => {
      alive = false
    }
  }, [provider, clock, tlTable, tlCol, capturePath, campaignId, run, preview])

  // `run_view` needs only campaign.db, so this means the campaign has neither database. A campaign
  // that never wrote a store is filtered out above; what is left is a store that exists but cannot be
  // read right now (an unreachable object store, a deleted result dir), so it is still worth saying.
  const noData = /campaign\.db/i.test((runs.error as Error | null)?.message ?? '')

  // The two dropdown dialogs are Popovers anchored to their trigger buttons.
  const [editAnchor, setEditAnchor] = useState<HTMLElement | null>(null)

  const pickRun = (item: ResultsTreeItem) => {
    // Only a run leaf resolves to a replayable run; campaigns/batches/configs just expand.
    if (item.kind !== 'run' || item.runId == null) return
    // Campaign and run move together: they are one selection, and setting them in two steps would
    // blank the run in between.
    onResultsChange(item.campaignId, selectionOf(item), '')
    setRunAnchor(null)
  }

  const onSaved = () => {
    setEditAnchor(null)
    // Reload the panels from the new effective .vast, and refresh the editor's cached source so a
    // reopen shows the saved text.
    queryClient.invalidateQueries({ queryKey: ['panels', campaignId] })
    queryClient.invalidateQueries({ queryKey: ['panels-source', campaignId] })
  }

  // The tree's own id builder, so the current run highlights in the picker. Shared rather than
  // spelled again here: a hand-written copy of the id is silently broken by any change to the
  // tree's shape (such as the batch level).
  const selectedTreeId = run
    ? selectionNodeId(campaignId, run, resolved.batch)
    : ''

  return (
    <Stack
      spacing={2}
      // `gap`, not Stack's default margin spacing: that one also emits
      // `& > :not(style):not(style) { margin: 0 }`, whose specificity beats a child's own sx class
      // and silently zeroed the negative margins the panel container below needs.
      useFlexGap
      // 48px is App's `p: 3` on the main Box, top + bottom, so the view fills the window exactly.
      sx={{ height: 'calc(100vh - 48px)' }}
    >
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h6">Run view</Typography>
        {/* The label names every level the picker selected — campaign · [batch ·] config · run — so
            the view says which campaign is on screen without opening the tree. The batch appears
            only for a search campaign, where the config name is a hash and the round is what places
            it. Wide enough for a typical campaign id, with the label itself ellipsized rather than
            wrapping the button. */}
        <Button
          variant="outlined"
          size="small"
          endIcon={<ArrowDropDownRoundedIcon />}
          onClick={(e) => setRunAnchor(e.currentTarget)}
          sx={{
            textTransform: 'none',
            minWidth: 440,
            maxWidth: 'min(720px, 60vw)',
            justifyContent: 'space-between',
          }}
        >
          <Box
            component="span"
            sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          >
            {run
              ? [
                  campaignId,
                  ...(resolved.batch === null ? [] : [`batch ${resolved.batch}`]),
                  run.configName,
                  `run ${run.runId}`,
                ].join(' · ')
              : 'Select run'}
          </Box>
        </Button>
        {/* Beside the picker it feeds: the reload is what puts a newly finished campaign into
            that tree. */}
        <RefreshResultsButton state={refresh} />
        {/* Refused while the campaign runs, and not merely because the preview shows one panel:
            saving writes a .vast override into the campaign's own `_config/`, which the runs that
            have not started yet are configured from. Editing the view would change the experiment.
            A tooltip on a span, since a disabled button fires no events for one to listen to. */}
        <Tooltip
          title={preview
            ? 'Not while the campaign is running — saving edits its configuration, which its '
              + 'remaining runs read.'
            : ''}
        >
          <span>
            <Button
              variant="text"
              size="small"
              startIcon={<EditRoundedIcon />}
              endIcon={<ArrowDropDownRoundedIcon />}
              onClick={(e) => setEditAnchor(e.currentTarget)}
              disabled={!available || preview}
              sx={{ textTransform: 'none' }}
            >
              Edit visualization
            </Button>
          </span>
        </Tooltip>
        {/* Pushed to the far right: these govern the whole view rather than the run picker they
            would otherwise look attached to. */}
        <Box sx={{ flexGrow: 1 }} />
        {/* The mirror of the Explorer's jump into here: same icon as the campaign card's shortcut,
            because it is the same destination, and it carries the run on screen so the tree opens on
            it. Left of the gear -- the gear governs the view, this leaves it. */}
        {run ? (
          <Tooltip title="Open this run in the results Explorer">
            <IconButton
              size="small"
              aria-label="open results explorer"
              onClick={() => openResultsView('explorer', campaignId, run)}
            >
              <ExplorerIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null}
        <RunSettingsMenu clock={clock} />
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
            campaigns={replayable}
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
        {available ? (
          <VisualizationEditor
            campaignId={campaignId}
            onClose={() => setEditAnchor(null)}
            onSaved={onSaved}
          />
        ) : null}
      </Popover>

      {!replayable.length ? (
        <Alert severity="info" variant="outlined">
          No campaign has runs to replay yet — a campaign appears here once it has recorded a run,
          either while it is still running (as a preview of its finished runs) or, with everything
          it measured, once it has finished and been postprocessed.
        </Alert>
      ) : !available ? (
        <Alert severity="info" variant="outlined">
          Pick a run to replay.
        </Alert>
      ) : panels.isPending || runs.isPending ? (
        <CircularProgress size={24} />
      ) : noData ? (
        <Alert severity="info" variant="outlined">
          This campaign has no store to read: no <code>campaign.db</code>, so it either never
          started or ended before recording anything.
        </Alert>
      ) : runs.isError ? (
        // Anything else that went wrong reading the runs, said rather than swallowed. Without this
        // the branch below claims the campaign has nothing to replay, which is a statement about
        // the campaign — when what actually happened is that we could not find out.
        <Alert severity="error" variant="outlined">
          Could not read this campaign&apos;s runs: {(runs.error as Error).message}
        </Alert>
      ) : !provider ? (
        <Alert severity="info" variant="outlined">
          {preview
            // A campaign on its very first run: it has runs, none has finished, and a replay needs a
            // finished one. Said plainly rather than as "no runs to replay", which reads as a
            // campaign that will never have any -- this one resolves itself within a run.
            ? 'No run has finished yet. A run can be replayed once it ends and its recording is written.'
            : 'This campaign has no runs to replay.'}
        </Alert>
      ) : (
        <>
          {/* Said alongside the view rather than instead of it: the transport bar is there for
              every campaign, so replacing the whole host would now hide a working panel to
              explain that there are none. */}
          {bare && !preview && (
            <Alert severity="info" variant="outlined">
              This run view has only the playback transport, which every campaign gets. Declare
              panels under <code>visualization.results.run_view.panels</code> — see{' '}
              <b>Edit visualization</b> — to show anything else.
            </Alert>
          )}
          <Box
            // The panels are the point of this view, so they get the whole window rather than
            // sitting inside the page gutter: the negative margins cancel App's `p: 3` on the main
            // Box on the three sides that touch the window (the header row above keeps its
            // padding), so keep them in step with that padding.
            sx={{ flexGrow: 1, minHeight: 0, mx: -3, mb: -3, position: 'relative' }}
          >
            <PanelHost key={runKey} panels={specs} clock={clock} data={provider} />
            {/* Over the scene rather than above it, because the scene is the full-bleed base layer
                and a banner in the page flow would push it down for a state that is temporary.
                Top-RIGHT: the 3D panel already speaks from top-left, top-centre and bottom-left,
                and this is about the view rather than the scene.

                `pointerEvents: none` on the wrapper with `auto` on the chip: a right-anchored panel
                dock sits exactly here, and an invisible box over its header would swallow the drag
                that moves it. The zIndex clears PanelHost's own stacking -- twice the number of
                visible panels, when one is brought to the front -- while staying below MUI's
                overlays (app bar 1100, drawer 1200, modal 1300), so a dialog covers the chip
                rather than the chip floating over the dialog. */}
            {preview && (
              <Box
                sx={{ position: 'absolute', top: 8, right: 8, pointerEvents: 'none', zIndex: 1000 }}
              >
                <Tooltip
                  title={
                    'This campaign is still running. Its finished runs replay in 3D from their own '
                    + 'recordings; metrics, charts and pass/fail need postprocessing, which happens '
                    + 'when the campaign ends. A run still in progress is listed but has nothing to '
                    + 'replay yet.'
                  }
                >
                  <Chip
                    size="small"
                    color="warning"
                    label="Preview"
                    sx={{ pointerEvents: 'auto' }}
                  />
                </Tooltip>
              </Box>
            )}
          </Box>
        </>
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

  const { notify } = useToasts()

  const save = useMutation({
    mutationFn: () => robovast.updatePanelsSource(campaignId, text ?? ''),
    // The popover closing is the only other sign this worked, and an edit whose effect is not
    // visible in the panels below leaves that ambiguous.
    onSuccess: () => {
      onSaved()
      notify({ severity: 'success', message: 'Visualization saved' })
    },
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
