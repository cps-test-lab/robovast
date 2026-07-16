import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Container from '@mui/material/Container'
import Tab from '@mui/material/Tab'
import Tabs from '@mui/material/Tabs'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import { robovast } from '@/lib/robovastClient'
import { Monitor } from '@/pages/Monitor'
import { Launcher } from '@/pages/Launcher'
import { ConfigEditor } from '@/pages/ConfigEditor'
import { Eval } from '@/pages/Eval'

// The whole app: a thin tabbed shell over the two M1 pages. The version chip doubles as the
// service-connection indicator (green when the handshake succeeds).
export function App() {
  const [tab, setTab] = useState(0)
  const version = useQuery({
    queryKey: ['version'],
    queryFn: () => robovast.version(),
    refetchInterval: 15000,
    retry: false,
  })

  return (
    <Box sx={{ minHeight: '100vh' }}>
      <AppBar position="sticky" color="transparent" elevation={0} sx={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
        <Toolbar>
          <Typography variant="h6" sx={{ color: 'primary.main', mr: 3 }}>
            RoboVAST
          </Typography>
          <Tabs value={tab} onChange={(_, v) => setTab(v)}>
            <Tab label="Monitor" />
            <Tab label="Launcher" />
            <Tab label="Config" />
            <Tab label="Results" />
          </Tabs>
          <Box flexGrow={1} />
          <Chip
            size="small"
            color={version.isSuccess ? 'success' : 'default'}
            variant="outlined"
            label={
              version.isSuccess
                ? `service ${version.data.robovast_version}${version.data.backend ? ` · ${version.data.backend}` : ''}`
                : 'disconnected'
            }
          />
        </Toolbar>
      </AppBar>

      <Container maxWidth={tab === 2 || tab === 3 ? 'xl' : 'md'} sx={{ py: 3 }}>
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
