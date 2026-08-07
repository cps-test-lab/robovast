import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { robovast, hasResults, type CampaignSummary } from '@/lib/robovastClient'
import { KeepAlive } from '@/components/KeepAlive'
import { lazyView } from '@/lib/lazyView'
import type { ResultsRefresh } from './RefreshResultsButton'

// The three sub-views are fetched separately because their dependencies barely overlap:
// Run view brings Three.js (the 3D scene) and Plotly (the panels), the Data browser brings
// Monaco (the SQL editor), and the Explorer brings neither. Bundled together, picking any
// one of them downloaded all of it. `KeepAlive` still holds each mounted once visited, so
// this changes when a chunk arrives, not how the views behave.
const ExplorerView = lazyView('Explorer', () => import('./ExplorerView')
  .then((m) => ({ default: m.ExplorerView })))
const RunView = lazyView('Run view', () => import('./RunView')
  .then((m) => ({ default: m.RunView })))
const DataBrowser = lazyView('Data browser', () => import('./DataBrowser')
  .then((m) => ({ default: m.DataBrowser })))

// Persist the Data-browser campaign across reloads (kept from the old Eval page).
const LAST_CAMPAIGN_KEY = 'eval.campaignId'

// The Results topic container: fetches the campaign list once and owns the campaign selection,
// shared by both sub-views (mirrors ConfigPage). Explorer is the default view; both are kept alive
// so each keeps its state across navigation.
export function ResultsPage({ view }: { view: string }) {
  const campaigns = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => robovast.listCampaigns(200, 0),
    // Poll so a campaign that finishes while the Results topic is open is *noticed*; what the views
    // show only changes when the user asks (below). Paused while the tab is unfocused.
    refetchInterval: 15_000,
  })
  // The Results topic only shows campaigns whose results are ready to explore — finished *and*
  // postprocessed. Everything downstream (Explorer, Run, Data) shares this filtered list, so a
  // still-running or non-postprocessed campaign never appears in any Results view.
  const live = useMemo(
    () => (campaigns.data?.campaigns ?? []).filter(hasResults),
    [campaigns.data],
  )

  // The displayed list is a snapshot of `live`, adopted on first load and thereafter only when the
  // user clicks Refresh. Letting the poll write straight through would reshuffle the Explorer tree
  // and the run/campaign pickers under someone who is reading a result — the campaign on screen is
  // finished and immutable, so there is nothing to gain from moving it.
  const [shown, setShown] = useState<CampaignSummary[] | null>(null)
  useEffect(() => {
    if (shown === null && campaigns.data) setShown(live)
  }, [shown, campaigns.data, live])
  const list = shown ?? live

  const shownIds = new Set(list.map((c) => c.campaign_id))
  const liveIds = new Set(live.map((c) => c.campaign_id))
  const newCount = live.filter((c) => !shownIds.has(c.campaign_id)).length
  // Deletions count as stale too — a campaign the service no longer has is a dead entry in the tree.
  const gone = list.some((c) => !liveIds.has(c.campaign_id))
  // Only the clicked refetch shows a spinner — `isFetching` also covers the 15 s poll, which would
  // blink the button at everyone every 15 seconds.
  const [refreshing, setRefreshing] = useState(false)
  const refresh: ResultsRefresh = {
    refresh: () => {
      setRefreshing(true)
      campaigns
        .refetch()
        .then((res) => {
          // Keep the current list on a failed refetch rather than blanking every view.
          if (res.data) setShown(res.data.campaigns.filter(hasResults))
        })
        .finally(() => setRefreshing(false))
    },
    newCount,
    stale: newCount > 0 || gone,
    busy: refreshing,
  }

  const [campaignId, setCampaignId] = useState(() => localStorage.getItem(LAST_CAMPAIGN_KEY) ?? '')

  // Default the Data browser to the newest campaign, and self-heal a selection that no longer
  // exists. Keyed on the available set so a click-selected campaign is never overridden.
  // [0] is the newest because the service lists newest-first and the filter preserves that order.
  const evalCampaigns = list
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
    <Box sx={{ position: 'relative' }}>
      <KeepAlive active={view !== 'data' && view !== 'run'}>
        <ExplorerView campaigns={list} refresh={refresh} />
      </KeepAlive>
      <KeepAlive active={view === 'run'}>
        <RunView
          campaignId={campaignId}
          campaigns={list}
          onCampaignChange={setCampaignId}
          refresh={refresh}
        />
      </KeepAlive>
      <KeepAlive active={view === 'data'}>
        <DataBrowser
          campaignId={campaignId}
          campaigns={list}
          onCampaignChange={setCampaignId}
          refresh={refresh}
        />
      </KeepAlive>
    </Box>
  )
}
