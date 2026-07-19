// PlaybackPanel: the transport bar for the run-view, spanning the bottom. It is the sole writer of the
// shared PlaybackClock -- a click on the progress bar seeks, play/pause toggles playback, and the 2×
// button fast-forwards. Every other panel just reads the clock, so scrubbing here moves them all. The
// timeline range is set on the clock by the RunView (from the run's rosbag timestamps); this panel is
// pure UI over the clock and holds no data of its own.

import { useRef } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import PauseRoundedIcon from '@mui/icons-material/PauseRounded'
import FastForwardRoundedIcon from '@mui/icons-material/FastForwardRounded'
import { registerPanel } from '@/lib/dashboard/registry'
import { useClock } from '@/lib/dashboard/clock'
import type { PanelProps } from '@/lib/dashboard/types'

// seconds -> m:ss.s
function fmt(s: number): string {
  if (!isFinite(s) || s < 0) s = 0
  const m = Math.floor(s / 60)
  const rem = s - m * 60
  return `${m}:${rem.toFixed(1).padStart(4, '0')}`
}

function PlaybackPanel({ clock }: PanelProps) {
  const { t, playing, speed, lo, hi } = useClock(clock)
  const barRef = useRef<HTMLDivElement | null>(null)

  const span = hi - lo
  const frac = span > 0 ? (t - lo) / span : 0

  const seekTo = (clientX: number) => {
    const el = barRef.current
    if (!el || span <= 0) return
    const rect = el.getBoundingClientRect()
    clock.seekFraction((clientX - rect.left) / rect.width)
  }

  const empty = span <= 0

  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, px: 2, height: '100%' }}>
      <IconButton size="small" onClick={() => clock.togglePlay()} disabled={empty} aria-label="play/pause">
        {playing ? <PauseRoundedIcon /> : <PlayArrowRoundedIcon />}
      </IconButton>
      <IconButton
        size="small"
        color={speed >= 2 ? 'primary' : 'default'}
        onClick={() => clock.setSpeed(speed >= 2 ? 1 : 2)}
        disabled={empty}
        aria-label="2x speed"
      >
        <FastForwardRoundedIcon />
      </IconButton>

      <Box
        ref={barRef}
        onMouseDown={(e) => seekTo(e.clientX)}
        sx={{
          flexGrow: 1,
          height: 8,
          borderRadius: 4,
          bgcolor: 'action.disabledBackground',
          cursor: empty ? 'default' : 'pointer',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        <Box
          sx={{
            position: 'absolute',
            inset: 0,
            width: `${frac * 100}%`,
            bgcolor: 'primary.main',
          }}
        />
      </Box>

      <Typography variant="caption" sx={{ fontFamily: 'monospace', whiteSpace: 'nowrap' }}>
        {empty ? '—' : `${fmt(t - lo)} / ${fmt(span)}`}
      </Typography>
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'playback',
    label: 'Playback',
    defaultPosition: { anchor: 'bottom', height: 56 },
    resizable: false,
    minimizable: false,
  },
  component: PlaybackPanel,
})

export default PlaybackPanel
