import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Container from '@mui/material/Container'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import Tooltip from '@mui/material/Tooltip'
import { robovast } from '@/lib/robovastClient'
import { formatUsageLabel } from '@/lib/format'
import { Monitor } from '@/pages/Monitor'
import { Launcher } from '@/pages/Launcher'
import { ConfigEditor } from '@/pages/ConfigEditor'
import { Eval } from '@/pages/Eval'

// The active tab is mirrored in the URL hash (e.g. #/launcher) so a browser refresh
// (or a bookmark / back-forward) restores the current page instead of snapping back to
// Monitor. Order matches the <Tab> list below.
const TABS = ['monitor', 'launcher', 'config', 'results'] as const

function tabFromHash(): number {
  const slug = window.location.hash.replace(/^#\/?/, '')
  const i = TABS.indexOf(slug as (typeof TABS)[number])
  return i >= 0 ? i : 0
}

// The whole app: a thin tabbed shell over the two M1 pages. The usage chip doubles as the
// service-connection indicator (green when the backend answers). Its tooltip keeps the
// version/backend discoverable.
export function App() {
  const [tab, setTab] = useState(tabFromHash)

  // Follow back/forward (and any external hash change) to the matching tab.
  useEffect(() => {
    const onHashChange = () => setTab(tabFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const selectTab = (v: number) => {
    setTab(v)
    window.location.hash = `/${TABS[v]}`
  }
  const usage = useQuery({
    queryKey: ['usage'],
    queryFn: () => robovast.resourceUsage(),
    refetchInterval: 15000,
    retry: false,
  })
  // Version is only used to fill the chip tooltip, so it need not poll often.
  const version = useQuery({
    queryKey: ['version'],
    queryFn: () => robovast.version(),
    retry: false,
  })

  return (
    <Box sx={{ minHeight: '100vh' }}>
      <AppBar position="sticky" color="transparent" elevation={0} sx={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
        <Toolbar>
          <Typography variant="h6" sx={{ color: 'primary.main', mr: 3 }}>
            RoboVAST
          </Typography>
          <Tabs value={tab} onChange={(_, v) => selectTab(v)}>
            <Tab label="Monitor" />
            <Tab label="Launcher" />
            <Tab label="Config" />
            <Tab label="Results" />
          </Tabs>
          <Box flexGrow={1} />
          <Tooltip
            title={
              version.isSuccess
                ? `robovast ${version.data.robovast_version}${version.data.backend ? ` · ${version.data.backend}` : ''}${
                    usage.isSuccess
                      ? ` · runs ${usage.data.parallel_runs ? 'in parallel' : 'sequentially'}`
                      : ''
                  }`
                : ''
            }
          >
            <Chip
              size="small"
              color={usage.isSuccess ? 'success' : 'default'}
              variant="outlined"
              label={usage.isSuccess ? formatUsageLabel(usage.data) : 'disconnected'}
            />
          </Tooltip>
        </Toolbar>
      </AppBar>

      <Container
        maxWidth={tab === 0 ? false : tab === 2 || tab === 3 ? 'xl' : 'md'}
        sx={{ py: 3 }}
      >
        {tab === 0 ? (
          <Monitor />
        ) : tab === 1 ? (
          <Launcher />
        ) : tab === 2 ? (
          <ConfigEditor />
        ) : (
          <Eval />
        )}
      </Container>
    </Box>
  )
}
