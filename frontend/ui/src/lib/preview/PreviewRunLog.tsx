// The log of a finished run of a campaign that is still running.
//
// The Run view's log panel and the Explorer's Log tab both read the `run_log` table, which
// postprocessing writes when a campaign ends. Before then that table holds nothing, but the run's
// own output does exist: its containers wrote `logs/system*.log` into the job's artifact dir, and
// the service already serves them merged and tagged over the same SSE tail the Monitor uses for a
// running job. So this reads what the run itself left behind rather than what the index has yet to
// be told, which is the rule the whole during-campaign view follows.
//
// It shows LESS than the post-hoc view, and that is deliberate rather than unfinished:
//
//   * no severity/node/container filtering, and no `/rosout` rows -- both come from the merge
//     postprocessing performs (`results_processing/run_log.py`), which joins the container files
//     with `rosout.csv`. Raw lines carry a `[LEVEL] [epoch] [node]` stamp only where rclpy or an
//     entrypoint wrote them; third-party output (a gz warning) carries nothing.
//   * no playback cursor. Greying past the cursor needs sim time, which comes from the clock map
//     -- also postprocessing. Inferring it from line epochs would put the log and the replay on
//     two slightly different clocks, and a reader would have no way to tell which was wrong.
//
// The post-hoc view stays the authority; this one is honest about being an early look.

import { Alert, Box, Typography } from '@mui/material'
import { LogPanel } from '@/components/LogPanel'
import { robovast } from '@/lib/robovastClient'

/** `job_name` is `<config>/<run>` -- the same identity the preview run tree carries, which is why
 *  a preview row needs no extra lookup to address its log. */
export const jobNameOf = (configName: string, runId: number | string) =>
  `${configName}/${runId}`

export function PreviewRunLog({
  campaignId,
  configName,
  runId,
}: {
  campaignId: string
  configName?: string
  runId?: number | string
}) {
  // A campaign or configuration node selects many runs, and a job log addresses exactly one.
  // Saying so beats streaming an arbitrary run's log under a heading that claims more.
  if (!configName || runId == null)
    return (
      <Alert severity="info" variant="outlined" sx={{ m: 1, py: 0 }}>
        Select a run to read its log. While a campaign is running its logs are read per run,
        from what each one wrote.
      </Alert>
    )

  const jobName = jobNameOf(configName, runId)
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ px: 1, pt: 0.5, flexShrink: 0 }}
      >
        Live container output — not filtered or time-synced to playback until the campaign
        finishes.
      </Typography>
      <Box sx={{ flexGrow: 1, minHeight: 0 }}>
        {/* resetKey: a new run is a new stream, so the tail restarts rather than appending the
            next run's lines onto the previous one's. */}
        <LogPanel
          resetKey={`${campaignId}:${jobName}`}
          streamUrl={robovast.jobLogStreamUrl(campaignId, jobName)}
        />
      </Box>
    </Box>
  )
}
