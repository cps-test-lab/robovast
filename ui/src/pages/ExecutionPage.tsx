import Box from '@mui/material/Box'
import { KeepAlive } from '@/components/KeepAlive'
import { Monitor } from '@/pages/Monitor'
import { Launcher } from '@/pages/Launcher'

// The Execution topic container: groups the two "run something" views — Monitor and Launcher — under
// one sidebar entry (mirrors ConfigPage/ResultsPage). They share no state, but both are kept alive so
// each retains its state (form input, live polling) when the user switches between them.
export function ExecutionPage({ view }: { view: string }) {
  return (
    <Box>
      <KeepAlive active={view !== 'launcher'}>
        <Monitor />
      </KeepAlive>
      <KeepAlive active={view === 'launcher'}>
        <Launcher />
      </KeepAlive>
    </Box>
  )
}