import { useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import TuneRoundedIcon from '@mui/icons-material/TuneRounded'
import RocketLaunchRoundedIcon from '@mui/icons-material/RocketLaunchRounded'
import InsightsRoundedIcon from '@mui/icons-material/InsightsRounded'
import { Sidebar, type NavTopic } from '@/components/Sidebar'
import { DataBrowserIcon, ExplorerIcon, RunViewIcon, SUBVIEW_ICON_SIZE } from '@/components/viewIcons'
import { KeepAlive } from '@/components/KeepAlive'
import { lazyView } from '@/lib/lazyView'

// Each page is fetched on first visit rather than in the entry bundle. Config and Results
// pull in Monaco, Plotly, Three and Vega between them — several megabytes that the campaign
// list, which is where the app opens, never touches. `lazyView` retries a failed import and
// puts a boundary behind it, because the service is often reached through a port-forward.
const ConfigPage = lazyView('Config', () => import('@/pages/config/ConfigPage')
  .then((m) => ({ default: m.ConfigPage })))
const Monitor = lazyView('Campaigns', () => import('@/pages/Monitor')
  .then((m) => ({ default: m.Monitor })))
const ResultsPage = lazyView('Results', () => import('@/pages/results/ResultsPage')
  .then((m) => ({ default: m.ResultsPage })))

// The whole navigation lives in the left sidebar: each topic is a top-level entry; a topic with
// several views expands to show them nested. The active topic/view is mirrored in the URL hash
// (e.g. #/config/files) so refresh / back-forward / bookmarks restore the view. A topic with views
// may carry a third segment naming the campaign on screen (#/results/run/<campaign_id>) — see Nav.
const TOPICS: NavTopic[] = [
  {
    // One consolidated page: the Editor / Files split is a tab bar inside the page (left column),
    // not sidebar sub-views, so config is a leaf topic. `#/config` still resolves.
    id: 'config',
    label: 'Config',
    icon: <TuneRoundedIcon />,
  },
  {
    // Launch + monitor merged into one page: the launch form is a bar atop the live campaign list,
    // so a launched campaign lives only in that list — never in a second, page-local copy that can
    // linger after it is deleted. `id: 'execution'` is kept so DEFAULT_NAV and #/execution resolve.
    id: 'execution',
    label: 'Campaigns',
    icon: <RocketLaunchRoundedIcon />,
  },
  {
    id: 'results',
    label: 'Results',
    icon: <InsightsRoundedIcon />,
    views: [
      { id: 'explorer', label: 'Explorer', icon: <ExplorerIcon sx={{ fontSize: SUBVIEW_ICON_SIZE }} /> },
      { id: 'run', label: 'Run view', icon: <RunViewIcon sx={{ fontSize: SUBVIEW_ICON_SIZE }} /> },
      { id: 'data', label: 'Data Browser', icon: <DataBrowserIcon sx={{ fontSize: SUBVIEW_ICON_SIZE }} /> },
    ],
  },
]

interface Nav {
  topicId: string
  viewId: string
  /** The campaign the view is showing, for topics whose views are campaign-scoped (Results). It is
   *  held here, in the URL, rather than inside the page: that is what lets a campaign card link
   *  straight into a view, a reload come back to the same campaign, and a link be pasted to someone
   *  else. Empty until a campaign is chosen — the page then fills it in (see setCampaign). */
  campaignId: string
}

// The view shown on a fresh load (no/unknown hash): the merged Campaigns page, so the app opens
// ready to launch and watch runs.
const DEFAULT_NAV: Nav = { topicId: 'execution', viewId: '', campaignId: '' }

// Parse #/topic/view/campaign into a valid Nav, defaulting the view to the topic's first (or '' for
// leaf topics) and falling back to DEFAULT_NAV when the hash is empty/unknown. The campaign is taken
// verbatim — the page validates it against the campaigns it has and repairs the hash if it is stale,
// which is the only place that knows whether an id still names anything.
function navFromHash(): Nav {
  const [rawTopic, rawView, rawCampaign] = window.location.hash.replace(/^#\/?/, '').split('/')
  const topic = TOPICS.find((t) => t.id === rawTopic)
  if (!topic) return DEFAULT_NAV
  const view = topic.views?.find((v) => v.id === rawView)?.id ?? topic.views?.[0]?.id ?? ''
  // Only a topic with views can be campaign-scoped; a leaf topic's third segment is noise.
  return { topicId: topic.id, viewId: view, campaignId: topic.views ? (rawCampaign ?? '') : '' }
}

function hashFor({ topicId, viewId, campaignId }: Nav): string {
  if (!viewId) return `/${topicId}`
  return campaignId ? `/${topicId}/${viewId}/${campaignId}` : `/${topicId}/${viewId}`
}

export function App() {
  const [nav, setNav] = useState<Nav>(navFromHash)

  // Follow back/forward (and any external hash change).
  useEffect(() => {
    const onHashChange = () => setNav(navFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const select = (topicId: string, viewId?: string) => {
    const topic = TOPICS.find((t) => t.id === topicId) ?? TOPICS[0]
    const view = viewId ?? topic.views?.[0]?.id ?? ''
    // The campaign is carried through every navigation: going from Explorer to the Data browser is
    // a change of lens on one campaign, not a request for a different one, and stepping out to the
    // campaign list and back should return to what was being read. It only reaches the hash for a
    // topic that has views (hashFor), so `#/config` stays `#/config`.
    const next = { topicId: topic.id, viewId: view, campaignId: nav.campaignId }
    setNav(next)
    window.location.hash = hashFor(next)
  }

  // The campaign shown by the Results views, written from inside them (their picker, the Explorer
  // tree, or the self-heal that repairs a stale id). replaceState, not an assignment to
  // `location.hash`: this reflects a selection the user already made *in* the view, so it must not
  // cost a Back press to get past — otherwise flipping through five campaigns buries the campaign
  // list five steps deep. It also fires no `hashchange`, so this cannot loop back through the
  // listener above. Only a jump from outside (a campaign card) pushes a real history entry.
  const setCampaign = (campaignId: string) => {
    if (nav.campaignId === campaignId) return
    const next = { ...nav, campaignId }
    window.history.replaceState(null, '', `#${hashFor(next)}`)
    setNav(next)
  }

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      <Sidebar
        topics={TOPICS}
        activeTopic={nav.topicId}
        activeView={nav.viewId}
        onSelect={select}
      />
      {/* Each view is kept alive (mounted-but-hidden) once visited, so its state persists across
          navigation instead of resetting on unmount. That also means a view that throws stays
          mounted and keeps throwing — hence a boundary per view, inside lazyView. */}
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, p: 3, position: 'relative' }}>
        <KeepAlive active={nav.topicId === 'config'}>
          <ConfigPage />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'execution'}>
          <Monitor />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'results'}>
          <ResultsPage
            view={nav.viewId}
            campaignId={nav.campaignId}
            onCampaignChange={setCampaign}
          />
        </KeepAlive>
      </Box>
    </Box>
  )
}
