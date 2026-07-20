import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Tooltip from '@mui/material/Tooltip'

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

const LIVE_PHASES = ['starting', 'running', 'finishing', 'postprocessing']

export function PhaseChip({ phase, size = 'small' }: { phase: string; size?: 'small' | 'medium' }) {
  return <Chip size={size} label={phase} color={COLOR[phase] ?? 'default'} />
}

// The compact form of PhaseChip: a small phase-colored dot that sits in front of a
// campaign name. Pulses while the campaign is live so a running campaign is obvious at
// a glance; the tooltip names the phase for the exact word.
export function PhaseDot({ phase }: { phase: string }) {
  const live = LIVE_PHASES.includes(phase)
  return (
    <Tooltip title={phase} placement="top">
      <Box
        component="span"
        sx={{
          width: 9,
          height: 9,
          borderRadius: '50%',
          flexShrink: 0,
          bgcolor: COLOR[phase] && COLOR[phase] !== 'default' ? `${COLOR[phase]}.main` : 'text.disabled',
          animation: live ? 'phaseDotPulse 1.4s ease-in-out infinite' : 'none',
          '@keyframes phaseDotPulse': {
            '0%, 100%': { opacity: 1 },
            '50%': { opacity: 0.3 },
          },
        }}
      />
    </Tooltip>
  )
}
