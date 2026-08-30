import { useCallback, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import RocketLaunchRoundedIcon from '@mui/icons-material/RocketLaunchRounded'
import InsightsRoundedIcon from '@mui/icons-material/InsightsRounded'
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded'
import { Sidebar, type NavTopic } from '@/components/Sidebar'
import {
  ConfigIcon,
  DataBrowserIcon,
  ExplorerIcon,
  RunViewIcon,
  SUBVIEW_ICON_SIZE,
} from '@/components/viewIcons'
import { KeepAlive } from '@/components/KeepAlive'
import { lazyView } from '@/lib/lazyView'
import { CAMPAIGN_SEL, hashFor, navFromHash, nextNav, type Nav, type ResultsSel } from '@/lib/hashNav'

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
const AdminPage = lazyView('Admin', () => import('@/pages/admin/AdminPage')
  .then((m) => ({ default: m.AdminPage })))

// The whole navigation lives in the left sidebar: each topic is a top-level entry; a topic with
// several views expands to show them nested. The active topic/view is mirrored in the URL hash so
// refresh / back-forward / bookmarks restore the view; the grammar and its two campaign scopes live
// in `@/lib/hashNav`.
const TOPICS: NavTopic[] = [
  {
    // One consolidated page: the Editor / Files split is a tab bar inside the page (left column),
    // not sidebar sub-views, so config is a leaf topic. `#/config` still resolves.
    id: 'config',
    label: 'Config',
    icon: <ConfigIcon />,
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
  {
    // Not about a campaign: the service itself — which version it runs, how loaded it has
    // been, and what it has been doing. Pinned to the foot of the rail beside the meters
    // reporting that same service (see NavTopic.footer).
    id: 'admin',
    label: 'Admin',
    icon: <SettingsRoundedIcon />,
    footer: true,
  },
]

// The view shown on a fresh load (no/unknown hash): the merged Campaigns page, so the app opens
// ready to launch and watch runs.
const DEFAULT_NAV: Nav = {
  topicId: 'execution',
  viewId: '',
  campaignId: '',
  sel: CAMPAIGN_SEL,
  tab: '',
  configCampaignId: '',
  shareImport: '',
}

const readNav = () => navFromHash(window.location.hash, TOPICS, DEFAULT_NAV)

export function App() {
  const [nav, setNav] = useState<Nav>(readNav)

  // Follow back/forward (and any external hash change).
  useEffect(() => {
    const onHashChange = () => setNav(readNav())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const select = (topicId: string, viewId?: string) => {
    const next = nextNav(nav, TOPICS, topicId, viewId)
    setNav(next)
    window.location.hash = hashFor(next)
  }

  // A share-import request has been delivered to the campaign view, so the URL stops carrying
  // it. replaceState for the same reason `setResults` uses it -- this reflects something that
  // already happened on screen and must not cost a Back press to get past. Clearing it is also
  // what makes pasting the same link twice work: the second paste is then a real hash change.
  const clearShareImport = useCallback(() => {
    setNav((prev) => {
      if (!prev.shareImport) return prev
      const next = { ...prev, shareImport: '' }
      window.history.replaceState(null, '', `#${hashFor(next)}`)
      return next
    })
  }, [])

  // What the Results views are showing — the campaign, the node within it, and which of that
  // node's tabs — written from inside them (a picker, the Explorer tree, or the self-heal that
  // repairs a stale id). replaceState, not an assignment to `location.hash`: this reflects a
  // selection the user already made *in* the view, so it must not cost a Back press to get past —
  // otherwise flipping through five runs buries the campaign list five steps deep. It also fires no
  // `hashchange`, so this cannot loop back through the listener above. Only a jump from outside (a
  // campaign card, or the Explorer/Run view cross-links) pushes a real history entry.
  //
  // Guarded on the *hash* rather than field by field: the hash is exactly the identity that matters
  // here, so one comparison covers every field and cannot go stale when another is added.
  const setResults = (campaignId: string, sel: ResultsSel, tab: string) => {
    // A different campaign is a different set of configs and runs, so a node from the old one
    // addresses nothing; it is dropped rather than carried into a campaign that has no such config.
    const next: Nav = campaignId === nav.campaignId
      ? { ...nav, sel, tab }
      : { ...nav, campaignId, sel: CAMPAIGN_SEL, tab: '' }
    if (hashFor(next) === hashFor(nav)) return
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
          {/* `campaignId` set means the page is showing that campaign's frozen config instead of a
              workspace — a deep link from a campaign card, never a sidebar click. `onExit` is the
              way back out, and it is the ordinary Config selection: hence `select`, whose transition
              drops the campaign (see nextNav). */}
          <ConfigPage campaignId={nav.configCampaignId} onExit={() => select('config')} />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'execution'}>
          <Monitor shareImport={nav.shareImport} onShareImportConsumed={clearShareImport} />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'results'}>
          <ResultsPage
            view={nav.viewId}
            campaignId={nav.campaignId}
            sel={nav.sel}
            tab={nav.tab}
            onResultsChange={setResults}
          />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'admin'}>
          <AdminPage />
        </KeepAlive>
      </Box>
    </Box>
  )
}
