// Results → Run view: a run-focused, time-driven dashboard. Pick one run of a campaign and replay
// it through the panels its .vast declares (visualization.panels) over the run's timeline. This
// component is the glue: it resolves the run, builds the shared PlaybackClock and DataProvider for
// it, discovers the timeline range, and hands the parsed panel specs to the PanelHost. The panels
// themselves (playback bar, costmaps, scenario tree) are independent plugins.
//
// A run that is still recording (`run_view.live`) is the same view: its tables are built as its
// recording grows, so every declared panel mounts, the provider follows the run's live stream, the
// clock's range grows with the rows and the clock *follows* it (a Live control in the header says
// so and returns to the edge). When the run finishes the tables are read once more, and the view
// is the finished run's.
//
// Two "dropdown dialogs" drive it: a Run picker (the shared Explorer campaign→config→run tree) and an
// Edit-visualization editor (Monaco, same style as the config editor) that saves the campaign's
// `visualization:` block as a .vast override and reloads the panels.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useToasts } from '@/components/ToastProvider'
import Editor from '@monaco-editor/react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
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
import PodcastsRoundedIcon from '@mui/icons-material/PodcastsRounded'
import SensorsRoundedIcon from '@mui/icons-material/SensorsRounded'
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded'
import { robovast, hasRecordedRuns, isRunning, type CampaignSummary } from '@/lib/robovastClient'
import {
  firstRunSelection,
  resolveSelection,
  selectionNodeId,
  selectionOf,
  type ResultsTreeItem,
} from '@/lib/resultsTree'
import { CAMPAIGN_SEL, type ResultsSel } from '@/lib/hashNav'
import { openCampaignConfig, openResultsView } from '@/lib/nav'
import { mayHaveStagedConfig } from '@/lib/campaignConfig'
import { ConfigIcon, ExplorerIcon } from '@/components/viewIcons'
import { PlaybackClock, useClock, type DataRow } from '@robovast/panel-kit'
import { dbDataProvider, describeQuery } from '@/lib/panels/dataProvider'
import { ANY_TABLE } from '@/lib/panels/liveFeed'
import { parsePanels } from '@/lib/panels/parsePanels'
import { PanelHost } from '@/lib/panels/PanelHost'
import { ResultsTree, runsQuery } from './ResultsTree'
import { panelTopics, useTap } from './tap'
import { jobNameOf } from '@/components/runLog/useJobLogStream'
import { RefreshResultsButton, type ResultsRefresh } from './RefreshResultsButton'
import { resetSceneViews, useSceneResetAvailable } from '@/panels/run_view/sceneReset'
import '@/panels/run_view' // registers the built-in panels

// Tables whose timestamp column can define the run's timeline; the union of their ranges is used.
// The fallback for a campaign that declares no `visualization.timeline`: the simulator's own pose
// table, and the postprocessed rosbag tables.
const TIME_TABLES = ['sim_poses', 'poses', 'behaviors', 'scenario_timestamps']

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

/** The header's Live control, for a run that is still recording: whether the clock is following the
 *  run's edge, and the way back to it once a reader has scrubbed away.
 *
 *  A button rather than a chip, because it does something: following ends the moment the reader
 *  seeks or pauses (the playback bar says nothing about why the cursor stopped tracking), and this
 *  is the one control that resumes it. The label states which of the two the view is in. */
function LiveControl({ clock }: { clock: PlaybackClock }) {
  const { following } = useClock(clock)
  return (
    <Tooltip
      title={following
        ? 'Following the run as it records. Scrubbing or pausing stops following.'
        : 'Return to the edge of the recording and follow it.'}
    >
      <Button
        size="small"
        variant={following ? 'contained' : 'outlined'}
        color={following ? 'error' : 'inherit'}
        startIcon={<SensorsRoundedIcon />}
        onClick={() => clock.follow()}
        sx={{ textTransform: 'none', whiteSpace: 'nowrap' }}
      >
        {following ? 'Live · following' : 'Live · paused'}
      </Button>
    </Tooltip>
  )
}

/** The header's Now control, for a run that is still recording: whether a tap on the run's
 *  simulation container is open, relaying what it publishes at this moment into the tail below.
 *
 *  A toggle, off by default, because a tap is a probe: a process the service starts runs in the
 *  simulator's container while it is on, and the run is recorded as probed. The panels already
 *  follow the recording, which is the untouched view; this is for the moment the recording has not
 *  reached yet, and only on the reader's word. */
function NowControl({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <Tooltip
      title={on
        ? 'Following the simulator directly: a tap is open in the run\'s simulation container, '
          + 'and the run is recorded as probed. Click to close it.'
        : 'Open a tap on the run\'s simulation container and show what it publishes now. '
          + 'Recorded against the run as a probe.'}
    >
      <Button
        size="small"
        variant={on ? 'contained' : 'outlined'}
        color={on ? 'warning' : 'inherit'}
        startIcon={<PodcastsRoundedIcon />}
        onClick={onToggle}
        sx={{ textTransform: 'none', whiteSpace: 'nowrap' }}
      >
        {on ? 'Now · tapping' : 'Now'}
      </Button>
    </Tooltip>
  )
}

/** The tail the Now tap fills: the newest lines the simulator printed, and a note saying what is
 *  followed and how the tap ended. */
function TapTail({ tap }: { tap: ReturnType<typeof useTap> }) {
  const endRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [tap.tail.lines.length])
  return (
    <Paper variant="outlined" sx={{ display: 'flex', flexDirection: 'column', maxHeight: 220 }}>
      <Box
        sx={{
          flexGrow: 1, overflow: 'auto', px: 1.5, py: 1,
          fontFamily: 'monospace', fontSize: 12, whiteSpace: 'pre-wrap',
        }}
      >
        {tap.isPending ? (
          <CircularProgress size={16} />
        ) : (
          tap.tail.lines.map((l, i) => <div key={i}>{l.line}</div>)
        )}
        <div ref={endRef} />
      </Box>
      <Divider />
      <Typography variant="caption" color="text.secondary" sx={{ px: 1.5, py: 0.5 }}>
        {tap.note}
      </Typography>
    </Paper>
  )
}

/** The widest time span the rows of a batch cover in `timeCol`, or null when none carries one. */
function batchSpan(rows: DataRow[], timeCol: string): [number, number] | null {
  let lo = Infinity
  let hi = -Infinity
  for (const row of rows) {
    const t = Number(row[timeCol])
    if (!Number.isFinite(t)) continue
    if (t < lo) lo = t
    if (t > hi) hi = t
  }
  return lo <= hi ? [lo, hi] : null
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

  const [runAnchor, setRunAnchor] = useState<HTMLElement | null>(null)

  // Only campaigns that recorded runs can be replayed, so they are the only ones this view offers —
  // the picker never lists a campaign whose store was never written, and a selection inherited from
  // another Results view (the campaign is shared) is treated as no selection here rather than
  // queried into a "no store to read" error. A campaign still running is offered like any other:
  // its `run_view` answers while it runs, and a run still recording is read live.
  const replayable = useMemo(() => campaigns.filter(hasRecordedRuns), [campaigns])
  const available = !!campaignId && replayable.some((c) => c.campaign_id === campaignId)
  const summary = replayable.find((c) => c.campaign_id === campaignId)
  const running = !!summary && isRunning(summary)

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
  // `runs` table is documented on `CAMPAIGN_RUNS_SQL`.
  //
  // A running campaign's rows GROW, so they are re-read — but only while the picker is actually
  // open, so a run nobody is choosing between costs nothing. Growth only ever appends a run, so a
  // refresh cannot move the selection out from under a reader.
  const runs = useQuery({
    ...runsQuery(summary),
    enabled: available,
    refetchInterval: running && !!runAnchor ? 5_000 : false,
  })

  // The batch is only meaningful when the picker's tree groups by it; for a batch-mode campaign
  // it stays null so the tree id built from it is the ungrouped one.
  const grouped = summary?.mode === 'search'
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

  // Whether the run on screen is still recording: `run_view.live` is true while it has no verdict
  // and its campaign is still running. Read from the same rows the picker draws, so the two agree.
  // The key carries it: a run that finishes is a new provider, the finished run's.
  const live = !!run && rows.some((r) =>
    String(r.config_name) === run.configName && Number(r.run_id) === Number(run.runId) && !!r.live)
  const runKey = run ? `${campaignId}:${run.configName}:${run.runId}:${live ? 'live' : 'done'}` : ''

  // One provider + clock per run. Recreated (and the old clock disposed) when the run changes.
  // `/describe` is the campaign's, so it is not: every provider of this campaign reads one answer
  // through the query cache. Versioned by what moves when the campaign's index is rewritten -- a
  // re-postprocessing ends the campaign again -- so a refreshed summary asks again.
  const describeVersion = `${summary?.finished_at ?? ''}:${summary?.postprocessed ? 1 : 0}`
  const getDescribe = useCallback(
    () => queryClient.fetchQuery(describeQuery(campaignId, describeVersion)),
    [queryClient, campaignId, describeVersion],
  )
  const provider = useMemo(
    () => (run
      ? dbDataProvider(campaignId, run.configName, run.runId, getDescribe, { live })
      : null),
    // `runKey` carries the run and its liveness; `run` itself is an object resolved per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [campaignId, runKey, getDescribe],
  )
  // The live subscription is the provider's; a run switch closes it with the provider.
  useEffect(() => () => provider?.close(), [provider])
  const clock = useMemo(() => new PlaybackClock(), [runKey])
  useEffect(() => () => clock.dispose(), [clock])

  const specs = useMemo(
    () => (panels.data ? parsePanels(panels.data.panels) : []),
    [panels.data],
  )

  // The Now tap: off by default and off again whenever the run on screen changes or stops being
  // live, since a tap is recorded against the run it was opened on and nothing else.
  const [nowOn, setNowOn] = useState(false)
  useEffect(() => setNowOn(false), [runKey])
  const tapJob = run && live ? jobNameOf(run.configName, run.runId) : null
  const topics = useMemo(() => panelTopics(specs), [specs])
  const tap = useTap(nowOn && live, campaignId, tapJob, topics)
  // The served list is never empty -- the playback transport is contributed for every campaign --
  // and the transport is not content: it is the clock the other panels follow. So "nothing to look
  // at here" is the service's `transport_only`, asked where the contributed panels are merged in
  // rather than by filtering the served list here, which would mean spelling the contributed types
  // a second time. A simulator that records its own poses has a real scene3d panel, so a roqsim campaign
  // that declares nothing is not bare.
  const bare = !!panels.data?.transport_only

  // Discover the timeline range and set it on the clock, in order of authority:
  //   1. an explicit `visualization.timeline` (a sim's own table with a `t` column);
  //   2. the union of the standard time tables (the simulator's poses, the postprocessed nav ones).
  // Depend on scalars rather than object identity, which churns on every panels refetch.
  //
  // For a live run the range is read the same way once, then grows with the rows: every batch the
  // provider's stream delivers -- whichever table, since the tables the panels read are the ones
  // the socket carries -- moves `hi` to its latest sample, and the clock follows. When the stream
  // ends the range is read once more (the finalised tables) and following ends there.
  const tlTable = panels.data?.timeline?.table
  const tlCol = panels.data?.timeline?.time_column
  useEffect(() => {
    if (!provider || !run) return
    let alive = true
    const timeCol = tlCol ?? 'timestamp'

    const readRange = async (): Promise<[number, number] | null> => {
      const lookups = tlTable
        ? [provider.timeRange(tlTable, tlCol).catch(() => null)]
        : TIME_TABLES.map((t) => provider.timeRange(t).catch(() => null))
      const valid = (await Promise.all(lookups)).filter((r): r is [number, number] => !!r)
      if (!valid.length) return null
      return [Math.min(...valid.map((r) => r[0])), Math.max(...valid.map((r) => r[1]))]
    }

    let haveRange = false
    readRange().then((range) => {
      if (!alive || !range) return
      haveRange = true
      clock.setRange(range[0], range[1])
      if (provider.live) clock.follow()
    })

    const unsubscribe = provider.live
      ? provider.subscribeLive(ANY_TABLE, (event) => {
          if (!alive) return
          if (event.kind === 'batch') {
            // A table with its own time column is not the timeline's; the declared one, or the
            // default, is what the rows are measured in.
            if (tlTable && event.table !== tlTable) return
            const span = batchSpan(event.rows, timeCol)
            if (!span) return
            const { lo, hi } = clock.getSnapshot()
            if (haveRange) {
              clock.setRange(Math.min(lo, span[0]), Math.max(hi, span[1]))
            } else {
              haveRange = true
              clock.setRange(span[0], span[1])
              clock.follow()
            }
          } else if (event.kind === 'eof') {
            readRange().then((range) => {
              if (!alive) return
              if (range) clock.setRange(range[0], range[1])
              clock.finish()
            })
          }
        })
      : null

    // Where the *trial* ended, which the range above deliberately does not encode: `setRange`
    // is the whole recording, so showing the shutdown phase restores it without re-querying.
    // Not for a live run: it has no verdict yet by definition, and the toggle says there is
    // nothing to trim, which is true until the run ends and the view becomes the finished run's.
    if (provider.live) clock.setVerdict(null)
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
      unsubscribe?.()
    }
  }, [provider, clock, tlTable, tlCol, run])

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
        {/* Refused while the campaign runs: saving writes a .vast override into the campaign's own
            `_config/`, which the runs that have not started yet are configured from. Editing the
            view would change the experiment. A tooltip on a span, since a disabled button fires
            no events for one to listen to. */}
        <Tooltip
          title={running
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
              disabled={!available || running}
              sx={{ textTransform: 'none' }}
            >
              Edit visualization
            </Button>
          </span>
        </Tooltip>
        {/* Beside the transport's owner rather than in the playback bar: following is a state of
            the whole view, and the bar is a panel a campaign may position anywhere. */}
        {provider?.live ? <LiveControl clock={clock} /> : null}
        {provider?.live ? (
          <NowControl on={nowOn} onToggle={() => setNowOn((v) => !v)} />
        ) : null}
        {/* Pushed to the far right: these govern the whole view rather than the run picker they
            would otherwise look attached to. */}
        <Box sx={{ flexGrow: 1 }} />
        {/* The two jumps out of this view, left of the gear -- the gear governs the view, these
            leave it. Same icons as the campaign card's shortcuts, because they are the same
            destinations. The configuration is the campaign's, so it needs no run on screen. */}
        {summary && mayHaveStagedConfig(summary.phase) ? (
          <Tooltip title="Open this campaign's configuration">
            <IconButton
              size="small"
              aria-label="open configuration"
              onClick={() => openCampaignConfig(campaignId)}
            >
              <ConfigIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        ) : null}
        {/* The mirror of the Explorer's jump into here, carrying the run on screen so the tree
            opens on it. */}
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
          while it is still running or after.
        </Alert>
      ) : !available ? (
        <Alert severity="info" variant="outlined">
          Pick a run to replay.
        </Alert>
      ) : panels.isPending || runs.isPending ? (
        <CircularProgress size={24} />
      ) : runs.isError ? (
        // Whatever went wrong reading the runs, said rather than swallowed. Without this the branch
        // below claims the campaign has nothing to replay, which is a statement about the campaign —
        // when what actually happened is that we could not find out. A campaign reaches this view
        // only once it has recorded runs, so a failure here is always a failed read and never an
        // empty store: it is reported as the error it is, in the words the service used.
        <Alert severity="error" variant="outlined">
          Could not read this campaign&apos;s runs: {(runs.error as Error).message}
        </Alert>
      ) : !provider ? (
        <Alert severity="info" variant="outlined">
          This campaign has no runs to replay.
        </Alert>
      ) : (
        <>
          {/* Said alongside the view rather than instead of it: the transport bar is there for
              every campaign, so replacing the whole host would now hide a working panel to
              explain that there are none. */}
          {bare && (
            <Alert severity="info" variant="outlined">
              This run view has only the playback transport, which every campaign gets. Declare
              panels under <code>visualization.results.run_view.panels</code> — see{' '}
              <b>Edit visualization</b> — to show anything else.
            </Alert>
          )}
          {nowOn && live ? <TapTail tap={tap} /> : null}
          <Box
            // The panels are the point of this view, so they get the whole window rather than
            // sitting inside the page gutter: the negative margins cancel App's `p: 3` on the main
            // Box on the three sides that touch the window (the header row above keeps its
            // padding), so keep them in step with that padding.
            sx={{ flexGrow: 1, minHeight: 0, mx: -3, mb: -3, position: 'relative' }}
          >
            <PanelHost key={runKey} panels={specs} clock={clock} data={provider} />
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
