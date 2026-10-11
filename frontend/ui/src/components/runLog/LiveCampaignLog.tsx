// A campaign's infrastructure log in the run log view: `useCampaignLogStream` feeding
// `RunLogView`, faceted by phase and coloured by level, following the newest row. Used by the
// Monitor's campaign log tab.

import { RunLogView } from './RunLogView'
import { useCampaignLogStream } from './useCampaignLogStream'

export function LiveCampaignLog({ campaignId }: { campaignId: string }) {
  const log = useCampaignLogStream(campaignId)
  return (
    <RunLogView
      data={log.data}
      isPending={log.isPending}
      error={log.error}
      note={log.note}
      // The host owns this and there is no verdict in an infrastructure log to cut at, so the
      // bar draws no toggle for it.
      hideShutdown={false}
      facetTitles={{ containers: 'Phase', nodes: 'Logger' }}
      tail
    />
  )
}
