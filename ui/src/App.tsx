import { useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import TuneRoundedIcon from '@mui/icons-material/TuneRounded'
import RocketLaunchRoundedIcon from '@mui/icons-material/RocketLaunchRounded'
import MonitorHeartRoundedIcon from '@mui/icons-material/MonitorHeartRounded'
import InsightsRoundedIcon from '@mui/icons-material/InsightsRounded'
import { Sidebar, type NavTopic } from '@/components/Sidebar'
import { KeepAlive } from '@/components/KeepAlive'
import { ConfigPage } from '@/pages/config/ConfigPage'
import { Launcher } from '@/pages/Launcher'
import { Monitor } from '@/pages/Monitor'
import { ResultsPage } from '@/pages/results/ResultsPage'

// The whole navigation lives in the left sidebar: each topic is a top-level entry; a topic with
// several views expands to show them nested. The active topic/view is mirrored in the URL hash
// (e.g. #/config/files) so refresh / back-forward / bookmarks restore the view.
const TOPICS: NavTopic[] = [
  {
    id: 'config',
    label: 'Config',
    icon: <TuneRoundedIcon />,
    views: [
      { id: 'configuration', label: 'Editor' },
      { id: 'files', label: 'File Browser' },
    ],
  },
  {
    id: 'execution',
    label: 'Execution',
    icon: <RocketLaunchRoundedIcon />,
  },
  {
    id: 'monitor',
    label: 'Monitor',
    icon: <MonitorHeartRoundedIcon />,
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

// The view shown on a fresh load (no/unknown hash): the Launcher, so the app opens ready to run.
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
          navigation instead of resetting on unmount. */}
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, p: 3 }}>
        <KeepAlive active={nav.topicId === 'config'}>
          <ConfigPage view={nav.viewId} />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'execution'}>
          <Launcher />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'monitor'}>
          <Monitor />
        </KeepAlive>
        <KeepAlive active={nav.topicId === 'results'}>
          <ResultsPage view={nav.viewId} />
        </KeepAlive>
      </Box>
    </Box>
  )
}
