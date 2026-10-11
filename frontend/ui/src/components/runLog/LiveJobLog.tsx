// A running job's log in the run log view: `useJobLogStream` feeding `RunLogView`. Used by the
// Monitor's job rows, and by the run view's log panel and the Explorer's log tab while the
// campaign runs and there is no `run_log` table to read yet.

import Alert from '@mui/material/Alert'
import { RunLogView } from './RunLogView'
import { jobNameOf, useJobLogStream } from './useJobLogStream'

export function LiveJobLog({ campaignId, jobName }: { campaignId: string; jobName: string }) {
  const log = useJobLogStream(campaignId, jobName)
  return (
    <RunLogView
      data={log.data}
      isPending={log.isPending}
      error={log.error}
      note={log.note}
      tail
    />
  )
}

/** The live log of one run, addressed by the run's config and id. A campaign or configuration
 *  node selects many runs, and a job log is exactly one. */
export function LiveRunLog({
  campaignId,
  configName,
  runId,
}: {
  campaignId: string
  configName?: string
  runId?: number | string
}) {
  if (!configName || runId == null)
    return (
      <Alert severity="info" variant="outlined" sx={{ m: 1, py: 0 }}>
        Select a run to read its log. While a campaign is running, each run's log is read live
        from the files its job writes.
      </Alert>
    )
  return <LiveJobLog campaignId={campaignId} jobName={jobNameOf(configName, runId)} />
}
