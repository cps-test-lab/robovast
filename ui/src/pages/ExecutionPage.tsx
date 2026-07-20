import Box from '@mui/material/Box'
import { KeepAlive } from '@/components/KeepAlive'
import { ConfigPage } from '@/pages/config/ConfigPage'
import { Launcher } from '@/pages/Launcher'

// The Execution topic container: groups the "set up and launch a run" views — Configuration, Files
// (both owned by ConfigPage, which shares the workspace bar and .vast schema between them) and the
// Launcher — under one sidebar entry. All are kept alive so each retains its state (workspace
// selection, editor buffer, launch form) when the user switches between them.
export function ExecutionPage({ view }: { view: string }) {
  return (
    <Box>
      <KeepAlive active={view !== 'launcher'}>
        <ConfigPage view={view} />
      </KeepAlive>
      <KeepAlive active={view === 'launcher'}>
        <Launcher />
      </KeepAlive>
    </Box>
  )
}
