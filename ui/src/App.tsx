import { useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import TuneRoundedIcon from '@mui/icons-material/TuneRounded'
import RocketLaunchRoundedIcon from '@mui/icons-material/RocketLaunchRounded'
import InsightsRoundedIcon from '@mui/icons-material/InsightsRounded'
import { Sidebar, type NavTopic } from '@/components/Sidebar'
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
// (e.g. #/config/files) so refresh / back-forward / bookmarks restore the view.
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
      { id: 'explorer', label: 'Explorer' },
      { id: 'run', label: 'Run view' },
      { id: 'data', label: 'Data Browser' },
    ],
  },
]

interface Nav {
  topicId: string
  viewId: string
}

// The view shown on a fresh load (no/unknown hash): the merged Campaigns page, so the app opens
// ready to launch and watch runs.
const DEFAULT_NAV: Nav = { topicId: 'execution', viewId: '' }

// Parse #/topic/view into a valid {topicId, viewId}, defaulting the view to the topic's first (or ''
// for leaf topics) and falling back to DEFAULT_NAV when the hash is empty/unknown.
function navFromHash(): Nav {
  const [rawTopic, rawView] = window.location.hash.replace(/^#\/?/, '').split('/')
  const topic = TOPICS.find((t) => t.id === rawTopic)
  if (!topic) return DEFAULT_NAV
  const view = topic.views?.find((v) => v.id === rawView)?.id ?? topic.views?.[0]?.id ?? ''
  return { topicId: topic.id, viewId: view }
}

function hashFor({ topicId, viewId }: Nav): string {
  return viewId ? `/${topicId}/${viewId}` : `/${topicId}`
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
    const next = { topicId: topic.id, viewId: view }
    setNav(next)
    window.location.hash = hashFor(next)
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
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, p: 3 }}>
        <KeepAlive active={nav.topicId === 'config'}>
          <ConfigPage />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'execution'}>
          <Monitor />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'results'}>
          <ResultsPage view={nav.viewId} />
        </KeepAlive>
      </Box>
    </Box>
  )
}
