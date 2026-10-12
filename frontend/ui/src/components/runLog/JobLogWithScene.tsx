// A job's live log on the Campaigns page, with the 3D view of the run it executes beside it.
//
// The log is the Jobs list's own (`LiveJobLog`). The 3D view is the run view's `scene3d` panel,
// mounted over the same live data path the run view uses, and shown only where the run view would
// show one: a campaign whose `/panels` carry a `scene3d` -- declared, or contributed by a simulator
// backend that records scene state. Anywhere else the log keeps the full width.
//
// The 3D view lives in its own chunk: the Campaigns page is the app's entry point, and three.js is
// for the reader who opens a job's log on a campaign that has a scene.

import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Tooltip from '@mui/material/Tooltip'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useTheme } from '@mui/material/styles'
import ViewInArRoundedIcon from '@mui/icons-material/ViewInArRounded'
import { lazyView } from '@/lib/lazyView'
import { runOfJob, sceneSpecOf } from '@/lib/jobRun'
import { robovast, type JobSummary } from '@/lib/robovastClient'
import { LiveJobLog } from './LiveJobLog'

const JobRunScene = lazyView('3D view', () => import('./JobRunScene'))

/** Where this browser remembers that the reader closed the 3D view beside job logs. */
export const SCENE_BESIDE_LOG_KEY = 'robovast.jobLog.scene3d'

function readShown(): boolean {
  try {
    return localStorage.getItem(SCENE_BESIDE_LOG_KEY) !== 'hidden'
  } catch {
    return true
  }
}

function writeShown(shown: boolean): void {
  try {
    if (shown) localStorage.removeItem(SCENE_BESIDE_LOG_KEY)
    else localStorage.setItem(SCENE_BESIDE_LOG_KEY, 'hidden')
  } catch {
    // A browser that keeps nothing still closes the view; it just opens again next time.
  }
}

/** Width of the strip a closed 3D view leaves, holding the button that opens it again. */
const STRIP_WIDTH = 36

/** The log on the left and, when there is one, a pane of the same height on the right. Both get
 *  the height as given, because the log viewer windows its rows against its parent's height and
 *  the 3D canvas sizes itself to its box: neither can take it from the other. `side` is the 3D
 *  view (`wide`) or the strip that reopens it. */
export function LogBesidePane({
  height,
  log,
  side,
  sideKind = 'wide',
}: {
  height: number
  log: ReactNode
  side: ReactNode | null
  sideKind?: 'wide' | 'strip'
}) {
  return (
    <Box sx={{ display: 'flex', gap: 1 }} style={{ height }}>
      <Box
        data-pane="log"
        sx={{ flex: '3 1 0', minWidth: 0, display: 'flex', flexDirection: 'column' }}
        style={{ height }}
      >
        {log}
      </Box>
      {side ? (
        <Box
          data-pane="side"
          sx={{
            flex: sideKind === 'wide' ? '2 1 0' : `0 0 ${STRIP_WIDTH}px`,
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            borderLeft: 1,
            borderColor: 'divider',
          }}
          style={{ height }}
        >
          {side}
        </Box>
      ) : null}
    </Box>
  )
}

/** One job's live log, with its run's 3D view beside it where the campaign has one. */
export function JobLogWithScene({
  campaignId,
  job,
  height,
}: {
  campaignId: string
  job: JobSummary
  height: number
}) {
  const theme = useTheme()
  // Below this the log needs every column it has; the 3D view is not mounted at all, so it opens no
  // stream for a pane nobody could read.
  const wide = useMediaQuery(theme.breakpoints.up('md'))
  const run = runOfJob(job)
  // The run view's own query (same key), so a campaign open in both reads its panels once.
  const panels = useQuery({
    queryKey: ['panels', campaignId],
    queryFn: () => robovast.listCampaignPanels(campaignId),
    enabled: wide && !!run,
    retry: false,
  })
  const spec = sceneSpecOf(panels.data?.panels)
  const [shown, setShown] = useState(readShown)
  const show = (on: boolean) => {
    setShown(on)
    writeShown(on)
  }

  const log = <LiveJobLog campaignId={campaignId} jobName={job.job_name} />
  if (!wide || !run) return <LogBesidePane height={height} log={log} side={null} />
  if (panels.isError) {
    return (
      <LogBesidePane
        height={height}
        log={log}
        side={
          <Alert severity="warning" variant="outlined" sx={{ m: 1 }}>
            No 3D view: this campaign&apos;s panels could not be read (
            {(panels.error as Error).message}).
          </Alert>
        }
      />
    )
  }
  if (!spec) return <LogBesidePane height={height} log={log} side={null} />
  if (!shown) {
    return (
      <LogBesidePane
        height={height}
        log={log}
        sideKind="strip"
        side={
          <Tooltip title="Show this run in 3D beside its log" placement="left">
            <IconButton size="small" aria-label="show 3D view" onClick={() => show(true)}>
              <ViewInArRoundedIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        }
      />
    )
  }
  return (
    <LogBesidePane
      height={height}
      log={log}
      side={
        <JobRunScene
          campaignId={campaignId}
          run={run}
          spec={spec}
          timeline={panels.data?.timeline ?? null}
          onClose={() => show(false)}
        />
      }
    />
  )
}
