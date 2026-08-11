import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { robovast, hasRecordedRuns, hasResults, type CampaignSummary } from '@/lib/robovastClient'
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

// Remember the campaign across *sessions* (kept from the old Eval page). Within a session the URL
// carries it; this is only the seed used when the URL names none — a fresh tab, or a return to the
// topic after browsing elsewhere.
const LAST_CAMPAIGN_KEY = 'eval.campaignId'

// The Results topic container: fetches the campaign list once and distributes the campaign selection
// to all three sub-views (mirrors ConfigPage). The selection itself lives in the URL, one level up in
// App — that is what lets a campaign card link into a view and a reload come back to it — so this
// page reads it from props and reports changes back rather than holding it. Explorer is the default
// view; all three are kept alive so each keeps its state across navigation.
export function ResultsPage({
  active,
  view,
  campaignId,
  onCampaignChange,
}: {
  /** The Results topic is the one on screen. Kept-alive pages stay mounted while hidden, so `view`
   *  alone cannot tell "the Explorer is showing" from "the Explorer is the Results topic's current
   *  view, but the user is in the monitor". */
  active: boolean
  view: string
  campaignId: string
  onCampaignChange: (campaignId: string) => void
}) {
  const campaigns = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => robovast.listCampaigns(200, 0),
    // Poll so a campaign that finishes while the Results topic is open is *noticed*; what the views
    // show only changes when the user asks (below). Paused while the tab is unfocused, so returning
    // to the tab reads once — otherwise the "new results are available" prompt can sit 15s behind
    // the campaign it is about. Only the *listing* is refreshed; the tree and the pickers still
    // move only on Refresh.
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  })
  // The Results topic only shows campaigns whose results are ready to explore — finished *and*
  // postprocessed. Everything downstream (Explorer, Run, Data) shares this filtered list, so a
  // still-running or non-postprocessed campaign never appears in any Results view.
  const live = useMemo(
    () => (campaigns.data?.campaigns ?? []).filter(hasResults),
    [campaigns.data],
  )

  // The displayed list is a snapshot of `live`, adopted on first load, on arriving at the Explorer
  // (below), and thereafter only when the user clicks Refresh. Letting the poll write straight
  // through would reshuffle the Explorer tree and the run/campaign pickers under someone who is
  // reading a result — the campaign on screen is finished and immutable, so there is nothing to gain
  // from moving it.
  const [shown, setShown] = useState<CampaignSummary[] | null>(null)
  useEffect(() => {
    if (shown === null && campaigns.data) setShown(live)
  }, [shown, campaigns.data, live])
  const list = shown ?? live
  // Take the service's answer as the new snapshot. A failed refetch carries no data; keep the list
  // that is on screen rather than blanking every view.
  const adopt = (res: { data?: { campaigns: CampaignSummary[] } }) => {
    if (res.data) setShown(res.data.campaigns.filter(hasResults))
  }

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
      campaigns.refetch().then(adopt).finally(() => setRefreshing(false))
    },
    newCount,
    stale: newCount > 0 || gone,
    busy: refreshing,
  }

  // Arriving at the Explorer catches the list up by itself. The freeze above protects a list that is
  // being *read*; a view that is only now being drawn has nobody reading it, so a campaign that
  // finished while the user was elsewhere should simply be in the tree when they get there — asking
  // them to press Refresh for it makes the button the price of admission. That leaves Refresh with
  // the one case it is actually for: a campaign finishing while the Explorer is already open.
  const explorerActive = active && view !== 'data' && view !== 'run'
  const wasActive = useRef(explorerActive)
  useEffect(() => {
    const entering = explorerActive && !wasActive.current
    wasActive.current = explorerActive
    if (!entering || !campaigns.data) return
    // Show what the poll already knows straight away, then ask once more: the poll runs at 15 s and
    // pauses while the window is unfocused, so the campaign someone just watched finish in the
    // monitor — the very one they are switching over here to look at — may not be in it yet.
    setShown(live)
    campaigns.refetch().then((res) => {
      // Not while the user has already moved on: past the arrival, this is an unrequested update
      // again, and the frozen snapshot is what the other two views are reading.
      if (wasActive.current) adopt(res)
    })
  }, [explorerActive]) // eslint-disable-line react-hooks/exhaustive-deps

  // Default the views to the newest campaign, and self-heal a selection that no longer exists.
  // [0] is the newest because the service lists newest-first and the filter preserves that order.
  // A campaign with no recorded runs is skipped when defaulting: it has no store, so it is the one
  // campaign neither the Run view nor the Data browser can show anything for — landing on it would
  // greet both views with an empty state while a readable campaign sat one click away.
  const evalCampaigns = list
  // Gated on the list having actually loaded: a transient service error leaves `campaigns.data`
  // in place (react-query keeps the last value), so the remembered selection survives an outage
  // instead of being wiped by a momentarily empty list.
  const loaded = !!campaigns.data
  // Campaign ids this page has already asked the service about and not found — so an id that is
  // genuinely gone costs one refetch, not one per render (see below).
  const probed = useRef(new Set<string>())
  useEffect(() => {
    if (!loaded) return
    if (evalCampaigns.some((c) => c.campaign_id === campaignId)) return
    // Asked for a campaign the *snapshot* lacks but the service has: adopt the live list. This is
    // the ordinary case for a campaign card's shortcut — the snapshot is deliberately frozen until
    // Refresh (above), so a campaign that finished while this tab was open is missing from it, and
    // that is exactly the campaign someone clicks through from the monitor. Nothing is being read
    // at that moment that the update could yank away: the click *is* the request to move.
    if (campaignId && live.some((c) => c.campaign_id === campaignId)) {
      setShown(live)
      return
    }
    // Asked for a campaign this page has never heard of. That is not yet a reason to overrule the
    // request: this list is a 15 s poll while the campaign cards are fed by the live stream, so a
    // campaign that has just finished is linked to from there *before* it turns up here. Ask the
    // service once, and decide on the answer — without this the shortcut lands on whatever campaign
    // was open before, which looks exactly like the click having done nothing.
    if (campaignId && !probed.current.has(campaignId)) {
      probed.current.add(campaignId)
      campaigns.refetch()
      return
    }
    if (campaigns.isFetching) return
    // No campaign named (fresh tab, or back from another topic), or one this service really does not
    // have: fall back to what this browser last looked at, then to the newest readable campaign.
    const remembered = localStorage.getItem(LAST_CAMPAIGN_KEY) ?? ''
    const seed = evalCampaigns.find((c) => c.campaign_id === remembered)
    const readable = evalCampaigns.find(hasRecordedRuns)
    // With nothing eligible, nothing may stay selected: the id would outlive the campaign it names
    // and the Data browser would keep querying a campaign no view lists — while the Explorer, which
    // renders the list directly, shows its empty state.
    onCampaignChange((seed ?? readable ?? evalCampaigns[0])?.campaign_id ?? '')
  }, [loaded, campaignId, campaigns.isFetching, evalCampaigns.map((c) => c.campaign_id).join(','), live.map((c) => c.campaign_id).join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (campaignId) localStorage.setItem(LAST_CAMPAIGN_KEY, campaignId)
    else localStorage.removeItem(LAST_CAMPAIGN_KEY)
  }, [campaignId])

  if (campaigns.isPending) return <CircularProgress size={24} />
  if (campaigns.isError)
    return <Alert severity="error">{(campaigns.error as Error).message}</Alert>

  return (
    <Box sx={{ position: 'relative' }}>
      <KeepAlive active={view !== 'data' && view !== 'run'}>
        <ExplorerView
          campaignId={campaignId}
          campaigns={list}
          onCampaignChange={onCampaignChange}
          refresh={refresh}
        />
      </KeepAlive>
      <KeepAlive active={view === 'run'}>
        <RunView
          campaignId={campaignId}
          campaigns={list}
          onCampaignChange={onCampaignChange}
          refresh={refresh}
        />
      </KeepAlive>
      <KeepAlive active={view === 'data'}>
        <DataBrowser
          campaignId={campaignId}
          campaigns={list}
          onCampaignChange={onCampaignChange}
          refresh={refresh}
        />
      </KeepAlive>
    </Box>
  )
}
