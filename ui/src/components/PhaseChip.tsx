import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'

// The controller's phase vocabulary (control_server.Status): building → starting → plugin install →
// variation → running → finishing → postprocessing → sharing → finished / failed / stopped. Map to
// MUI colors; unknown phases stay neutral.
const COLOR: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info'> = {
  building: 'info',
  starting: 'info',
  'plugin install': 'info',
  variation: 'info',
  running: 'warning',
  finishing: 'warning',
  postprocessing: 'warning',
  sharing: 'warning',
  finished: 'success',
  failed: 'error',
  stopped: 'error',
  crashed: 'error',
  // `unknown` (a campaign whose live driver was lost to a service restart) has no
  // color entry on purpose — it falls through to the neutral 'default' chip.
}

const LIVE_PHASES = [
  'building', 'starting', 'plugin install', 'variation', 'running', 'finishing', 'postprocessing',
  'sharing',
]

export function PhaseChip({ phase, size = 'small' }: { phase: string; size?: 'small' | 'medium' }) {
  return <Chip size={size} label={phase} color={COLOR[phase] ?? 'default'} />
}

// The compact form of PhaseChip: a phase-colored bullet pill with the phase name
// inside, sitting in front of a campaign name. The leading dot pulses while the
// campaign is live so a running campaign is obvious at a glance.
export function PhaseDot({ phase }: { phase: string }) {
  const live = LIVE_PHASES.includes(phase)
  const color =
    COLOR[phase] && COLOR[phase] !== 'default' ? `${COLOR[phase]}.main` : 'text.disabled'
  return (
    <Box
      component="span"
      sx={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 0.75,
        px: 1,
        py: 0.25,
        borderRadius: 999,
        border: 1,
        borderColor: color,
        color,
        bgcolor: (t) => t.palette.action.hover,
      }}
    >
      <Box
        component="span"
        sx={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          flexShrink: 0,
          bgcolor: color,
          animation: live ? 'phaseDotPulse 1.4s ease-in-out infinite' : 'none',
          '@keyframes phaseDotPulse': {
            '0%, 100%': { opacity: 1 },
            '50%': { opacity: 0.3 },
          },
        }}
      />
      <Box component="span" sx={{ fontSize: '0.75rem', fontWeight: 600, lineHeight: 1 }}>
        {phase}
      </Box>
    </Box>
  )
}
