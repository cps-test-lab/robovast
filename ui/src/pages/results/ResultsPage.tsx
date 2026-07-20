import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { robovast, campaignsNewestFirst, hasResults } from '@/lib/robovastClient'
import { KeepAlive } from '@/components/KeepAlive'
import { ExplorerView } from './ExplorerView'
import { DataBrowser } from './DataBrowser'
import { RunView } from './RunView'

// Persist the Data-browser campaign across reloads (kept from the old Eval page).
const LAST_CAMPAIGN_KEY = 'eval.campaignId'

// The Results topic container: fetches the campaign list once and owns the campaign selection,
// shared by both sub-views (mirrors ConfigPage). Explorer is the default view; both are kept alive
// so each keeps its state across navigation.
export function ResultsPage({ view }: { view: string }) {
  const campaigns = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => robovast.listCampaigns(200, 0),
  })
  // The Results topic only shows campaigns whose results are ready to explore — finished *and*
  // postprocessed. Everything downstream (Explorer, Run, Data) shares this filtered list, so a
  // still-running or non-postprocessed campaign never appears in any Results view.
  const list = (campaigns.data?.campaigns ?? []).filter(hasResults)

  const [campaignId, setCampaignId] = useState(() => localStorage.getItem(LAST_CAMPAIGN_KEY) ?? '')

  // Default the Data browser to the newest campaign, and self-heal a selection that no longer
  // exists. Keyed on the available set so a click-selected campaign is never overridden.
  const evalCampaigns = useMemo(() => campaignsNewestFirst(list), [list])
  useEffect(() => {
    if (!evalCampaigns.length) return
    if (!evalCampaigns.some((c) => c.campaign_id === campaignId)) {
      setCampaignId(evalCampaigns[0].campaign_id)
    }
  }, [evalCampaigns.map((c) => c.campaign_id).join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (campaignId) localStorage.setItem(LAST_CAMPAIGN_KEY, campaignId)
  }, [campaignId])

  if (campaigns.isPending) return <CircularProgress size={24} />
  if (campaigns.isError)
    return <Alert severity="error">{(campaigns.error as Error).message}</Alert>

  return (
    <Box>
      <KeepAlive active={view !== 'data' && view !== 'run'}>
        <ExplorerView campaigns={list} />
      </KeepAlive>
      <KeepAlive active={view === 'run'}>
        <RunView campaignId={campaignId} campaigns={list} onCampaignChange={setCampaignId} />
      </KeepAlive>
      <KeepAlive active={view === 'data'}>
        <DataBrowser
          campaignId={campaignId}
          campaigns={list}
          onCampaignChange={setCampaignId}
        />
      </KeepAlive>
    </Box>
  )
}
