import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'

import { isTerminalPhase } from '../lib/robovastClient'

// The controller's phase vocabulary (client.status.Phase): initializing → building → starting →
// plugin install → variation → importing → running → finishing → postprocessing → sharing →
// finished / failed / stopped. Map to MUI colors; unknown phases stay neutral. Which of them are
// *live* is not restated here — see RUNNING_PHASES in robovastClient, the one copy.
const COLOR: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info'> = {
  initializing: 'info',
  building: 'info',
  starting: 'info',
  'plugin install': 'info',
  variation: 'info',
  importing: 'info',
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

// A campaign reaches `finished` as soon as its runs are done, so a failed post-run step
// (postprocessing, upload-to-share) still leaves the phase green — the run data exists, but the
// derived data the user came for does not. `issue` names that step: it downgrades the indicator to
// warning and says so on the label, so a campaign missing its results never reads as a clean one.
// Warning rather than error is deliberate — the runs really did finish, and the step is
// re-triggerable from the actions menu.
function issueColor(phase: string, issue?: string | null) {
  return issue ? ('warning' as const) : (COLOR[phase] ?? 'default')
}

const issueTitle = (issue: string) =>
  `The runs finished, but ${issue}. Retrigger it from the campaign's actions menu.`

export function PhaseChip({
  phase,
  issue,
  size = 'small',
}: {
  phase: string
  issue?: string | null
  size?: 'small' | 'medium'
}) {
  return (
    <Chip
      size={size}
      label={issue ? `${phase} · ${issue}` : phase}
      color={issueColor(phase, issue)}
      title={issue ? issueTitle(issue) : undefined}
    />
  )
}

/** The "this is still going" animation, defined once so every live marker breathes at the same
 *  rate. Two dots pulsing a little differently on one screen reads as a rendering fault rather than
 *  as one idiom, and they do appear together: the monitor's phase dot and the run picker's campaign
 *  dot are the same statement about the same campaign.
 *
 *  Spread into an `sx`. The keyframes ride along with it, so a user has nothing to declare but this. */
export const livePulseSx = {
  animation: 'phaseDotPulse 1.4s ease-in-out infinite',
  '@keyframes phaseDotPulse': {
    '0%, 100%': { opacity: 1 },
    '50%': { opacity: 0.3 },
  },
} as const

// The compact form of PhaseChip: a phase-colored bullet pill with the phase name
// inside, sitting in front of a campaign name. The leading dot pulses while the
// campaign is live so a running campaign is obvious at a glance.
export function PhaseDot({ phase, issue }: { phase: string; issue?: string | null }) {
  // `isTerminalPhase` answers false for an absent phase on purpose — a phase not read yet
  // must not stop a poll — so an empty one would pulse without this. Nothing to say yet is
  // not something to animate.
  const live = !!phase && !isTerminalPhase(phase)
  const tone = issueColor(phase, issue)
  const color = tone !== 'default' ? `${tone}.main` : 'text.disabled'
  return (
    <Box
      component="span"
      title={issue ? issueTitle(issue) : undefined}
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
          ...(live ? livePulseSx : {}),
        }}
      />
      {/* `nowrap`: with an `issue` the label is a sentence fragment ("finished · postprocessing
          failed"), and in a narrow flex slot it wrapped to three lines — which stretched the pill,
          and with it the row it sits in, to three times its height. The pill is a marker; it takes
          the width it needs on one line or it is not one. */}
      <Box
        component="span"
        sx={{ fontSize: '0.75rem', fontWeight: 600, lineHeight: 1, whiteSpace: 'nowrap' }}
      >
        {issue ? `${phase} · ${issue}` : phase}
      </Box>
    </Box>
  )
}
