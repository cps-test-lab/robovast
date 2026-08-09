// The Explorer's Log tab: one run's merged log, the same view the run-view panel renders.
//
// Run level only. A config or campaign node has no single log — it has a *search across* runs,
// which is a different view with different controls; offering one tab for both made the tab mean
// two things depending on what was selected. That cross-run question is answered where a query
// belongs: `search_run_logs` (MCP) and SQL over `run_log` in the Data browser.
//
// No playback clock here, so `RunLogView` degrades to a plain filtered log — no greying, no
// divider, no jump button — rather than implying a position it does not have. The filters, the
// colours and the windowing are the panel's own code.

import Box from '@mui/material/Box'
import { RunLogView } from './RunLogView'
import { useRunLog } from './useRunLog'

/** Which node the Explorer has selected. Only a ``run`` node gets this tab (see above), but the
 *  scope carries the level so the view can say what it is showing. */
export interface LogTabScope {
  campaignId: string
  level: string
  configName?: string
  runId?: number
}

export function RunLogTab({ scope }: { scope: LogTabScope }) {
  const log = useRunLog({
    campaignId: scope.campaignId,
    configName: scope.configName,
    runId: scope.runId,
  })
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <RunLogView data={log.data} isPending={log.isPending} error={log.error} />
    </Box>
  )
}
