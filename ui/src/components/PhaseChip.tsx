import Chip from '@mui/material/Chip'

// The controller's phase vocabulary (control_server.Status): starting → running → finishing →
// finished / failed / stopped. Map to MUI colors; unknown phases stay neutral.
const COLOR: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info'> = {
  starting: 'info',
  running: 'warning',
  finishing: 'warning',
  postprocessing: 'warning',
  finished: 'success',
  failed: 'error',
  stopped: 'error',
  error: 'error',
}

export function PhaseChip({ phase, size = 'small' }: { phase: string; size?: 'small' | 'medium' }) {
  return <Chip size={size} label={phase} color={COLOR[phase] ?? 'default'} />
}
