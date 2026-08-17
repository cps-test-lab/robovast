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
import { registerPanel } from '@/lib/panels/registry'
import { useRunLog } from '@/components/runLog/useRunLog'
import { useClock, type PanelProps } from '@robovast/panel-kit'

// seconds -> m:ss.s
function fmt(s: number): string {
  if (!isFinite(s) || s < 0) s = 0
  const m = Math.floor(s / 60)
  const rem = s - m * 60
  return `${m}:${rem.toFixed(1).padStart(4, '0')}`
}

function PlaybackPanel({ clock, data }: PanelProps) {
  const { t, playing, speed, lo, hi, verdict, hideShutdown } = useClock(clock)
  const barRef = useRef<HTMLDivElement | null>(null)

  // Warnings and errors as tick marks on the bar, so the log's shape is visible *before* you
  // scrub into it -- otherwise finding the moment something went wrong means dragging blind.
  // Only the severe rows are fetched: the marks are the point, not the text, and one query for
  // a handful of rows costs less than the whole log. Reuses the same load the log panel and the
  // Explorer tab use, so react-query serves it from cache when the log panel is open too.
  const runId = Number(data.runId)
  const severe = useRunLog({
    campaignId: data.campaignId,
    configName: data.configName,
    runId: Number.isFinite(runId) ? runId : undefined,
    severities: ['warn', 'error'],
    maxRows: 5000,
  })

  const span = hi - lo
  const frac = span > 0 ? (t - lo) / span : 0

  const seekTo = (clientX: number) => {
    const el = barRef.current
    if (!el || span <= 0) return
    const rect = el.getBoundingClientRect()
    clock.seekFraction((clientX - rect.left) / rect.width)
  }

  // Press-and-drag scrubbing: seek on mouse-down, then follow the pointer until it's released.
  // Listeners go on the window so the drag keeps tracking even when the cursor leaves the bar.
  const startScrub = (e: React.MouseEvent) => {
    if (span <= 0) return
    e.preventDefault()
    seekTo(e.clientX)
    const onMove = (ev: MouseEvent) => seekTo(ev.clientX)
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
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
        onMouseDown={startScrub}
        sx={{
          flexGrow: 1,
          height: 16,
          borderRadius: 0.75,
          bgcolor: 'action.hover',
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
        {/* Where the trial ended, drawn only while the shutdown phase is *shown*: with it
            hidden the verdict is the end of the bar and a line there marks nothing. Shown, the
            bar is longer than the run and the reason is invisible without it -- so this is what
            says which part is the trial and which part is teardown.

            `text.primary`, not the fill's own colour: it is drawn over the played-through side
            as well, where a primary-on-primary line is no line at all. */}
        {!hideShutdown && verdict != null && span > 0 && verdict > lo && verdict < hi ? (
          <Box
            sx={{
              position: 'absolute',
              left: `${((verdict - lo) / span) * 100}%`,
              top: 0,
              bottom: 0,
              width: 2,
              pointerEvents: 'none',
              bgcolor: 'text.primary',
            }}
          />
        ) : null}
        {/* Drawn over the fill so a mark stays visible on the played-through side too. An
            error is drawn full height and a warning half, so the two read apart at a glance
            without relying on colour alone. */}
        {span > 0
          ? (severe.data?.rows ?? []).map((row, i) =>
              row.sim_time == null || row.sim_time < lo || row.sim_time > hi ? null : (
                <Box
                  key={i}
                  sx={{
                    position: 'absolute',
                    left: `${((row.sim_time - lo) / span) * 100}%`,
                    top: row.severity === 'error' ? 0 : '50%',
                    bottom: 0,
                    width: 2,
                    // Not pointer-events:none by accident -- clicks must reach the bar
                    // underneath, so a mark is a target for scrubbing rather than a dead spot.
                    pointerEvents: 'none',
                    bgcolor: row.severity === 'error' ? '#d32f2f' : '#b58900',
                  }}
                />
              ),
            )
          : null}
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
    defaultPosition: { anchor: 'bottom', height: 40 },
    resizable: false,
    minimizable: false,
    // Framed (a bordered transport bar at the bottom) but no header: it has no title and isn't
    // minimizable, so PanelHost renders no header row.
  },
  component: PlaybackPanel,
})

export default PlaybackPanel
