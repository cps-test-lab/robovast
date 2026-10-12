// The 3D view of one job's run, beside its live log: the run view's `Scene3DView` over the run's
// own provider and clock, following the edge of the recording while the run records and showing
// where it ended once it has. No playback controls -- it shows the run *now*, which is what the log
// beside it shows -- and the free orbit camera of the run view's base layer.
//
// Nothing is stored for it: the scene is the world's cached glTF file (built on first view, as in
// the run view) and the motion is the run's live stream. Unmounting -- closing the log or this
// view -- closes the stream with the provider.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import CenterFocusStrongRoundedIcon from '@mui/icons-material/CenterFocusStrongRounded'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import OpenInFullRoundedIcon from '@mui/icons-material/OpenInFullRounded'
import { PlaybackClock, useClock, type PanelSpec } from '@robovast/panel-kit'
import { runPhaseOf, runRowSql, runViewHashOf, type JobRun } from '@/lib/jobRun'
import { dbDataProvider } from '@/lib/panels/dataProvider'
import { useRunTimeline, type RunTimeline } from '@/lib/panels/runTimeline'
import { robovast } from '@/lib/robovastClient'
import { Scene3DView } from '@/panels/run_view/Scene3DPanel'

/** How often a run that has not started recording is looked for again. */
const STARTING_POLL_MS = 5_000

function LiveState({ clock }: { clock: PlaybackClock }) {
  const { following } = useClock(clock)
  return following ? (
    <Chip label="live" size="small" color="error" variant="outlined" sx={{ height: 18 }} />
  ) : null
}

export default function JobRunScene({
  campaignId,
  run,
  spec,
  timeline,
  onClose,
}: {
  campaignId: string
  run: JobRun
  spec: PanelSpec
  timeline: RunTimeline | null
  onClose: () => void
}) {
  // The run's `run_view` row: none until the job has started its run, then whether it is still
  // recording. Read until it exists and not after: the provider is opened on the answer, and a run
  // that finishes later is told so by its own stream (`eof`), which keeps the last state on screen
  // rather than rebuilding the view.
  const row = useQuery({
    queryKey: ['job-run-row', campaignId, run.configName, run.runId],
    queryFn: () => robovast.queryCampaignDataSql(campaignId, runRowSql(run), 1),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (q) => (q.state.data?.rows.length ? false : STARTING_POLL_MS),
  })
  const phase = row.data ? runPhaseOf(row.data.rows) : null

  const provider = useMemo(
    () => (phase === 'live' || phase === 'finished'
      ? dbDataProvider(
          campaignId, run.configName, run.runId,
          () => robovast.describeCampaignData(campaignId),
          { live: phase === 'live' },
        )
      : null),
    [campaignId, run.configName, run.runId, phase],
  )
  useEffect(() => () => provider?.close(), [provider])
  const clock = useMemo(() => new PlaybackClock(), [provider])
  useEffect(() => () => clock.dispose(), [clock])
  const liveError = useRunTimeline(provider, clock, timeline, { openAtEnd: true })

  // This view's own reset, offered by its own button: the run view header's channel would let that
  // header re-frame a camera on another page.
  const [reset, setReset] = useState<(() => void) | null>(null)
  const registerReset = useCallback((fn: () => void) => {
    setReset(() => fn)
    return () => setReset(null)
  }, [])

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <Stack direction="row" spacing={0.5} alignItems="center" sx={{ px: 1, minHeight: 32 }}>
        <Typography variant="caption" color="text.secondary" noWrap sx={{ flexGrow: 1 }}>
          3D · {run.configName} · run {run.runId}
        </Typography>
        {provider?.live ? <LiveState clock={clock} /> : null}
        {phase === 'finished' ? (
          <Chip label="finished" size="small" variant="outlined" sx={{ height: 18 }} />
        ) : null}
        <Tooltip title="Put the camera back where the scene opened">
          <span>
            <IconButton
              size="small"
              aria-label="reset 3D view"
              disabled={!reset}
              onClick={() => reset?.()}
            >
              <CenterFocusStrongRoundedIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
        <Tooltip title="Hide the 3D view beside job logs">
          <IconButton size="small" aria-label="hide 3D view" onClick={onClose}>
            <CloseRoundedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Stack>
      {liveError ? (
        <Alert severity="error" variant="outlined" sx={{ mx: 1, mb: 1, py: 0 }}>
          The live stream of this run failed: {liveError}
        </Alert>
      ) : null}
      <Box sx={{ flexGrow: 1, minHeight: 0, position: 'relative' }}>
        {row.isError ? (
          <Alert severity="warning" variant="outlined" sx={{ m: 1 }}>
            Could not read this run: {(row.error as Error).message}
          </Alert>
        ) : !provider ? (
          <Stack direction="row" spacing={1} alignItems="center" sx={{ p: 2 }}>
            <CircularProgress size={16} />
            <Typography variant="body2" color="text.secondary">
              {phase === 'starting'
                ? 'This job has not started its run yet; the 3D view follows it once it records.'
                : 'Looking up this job’s run…'}
            </Typography>
          </Stack>
        ) : (
          <Scene3DView
            key={provider.scope + (provider.live ? ':live' : ':done')}
            spec={spec}
            clock={clock}
            data={provider}
            registerReset={registerReset}
          />
        )}
        {/* Over the canvas, top right: the scene's own alerts anchor top left and top centre. A
            link rather than a handler, so it can also be opened in a new tab. */}
        <Tooltip title="Open in run view, with every panel of this run" placement="left">
          <IconButton
            size="small"
            aria-label="open in run view"
            href={runViewHashOf(campaignId, run)}
            sx={{
              position: 'absolute', top: 8, right: 8,
              bgcolor: 'background.paper', opacity: 0.85, '&:hover': { bgcolor: 'background.paper', opacity: 1 },
            }}
          >
            <OpenInFullRoundedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>
    </Box>
  )
}
