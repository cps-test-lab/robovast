import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'

function clamp01(x: number): number {
  return Math.min(1, Math.max(0, x))
}

// A compact horizontal meter: a rounded track filled proportional to `fraction`
// (0..1), with an optional lighter `buffer` segment layered beneath it to show
// "in-progress on top of done" (the buffer is the wider, fainter fill; the primary
// fill sits on top). Give `color` to fix the fill, or omit it to auto-threshold
// green → amber → red as it fills. `text` overlays centered on the bar. Shared by the
// sidebar usage bars and the campaign run/budget bars so the two read identically.
export function MeterBar({
  fraction,
  buffer,
  color,
  height = 16,
  text,
}: {
  fraction: number
  buffer?: number
  color?: string
  height?: number
  text?: ReactNode
}) {
  const f = clamp01(fraction)
  const b = buffer == null ? 0 : clamp01(buffer)
  const fill = color ?? (f < 0.7 ? 'success.main' : f < 0.9 ? 'warning.main' : 'error.main')
  const segment = (width: number, opacity: number) => (
    <Box
      sx={{
        position: 'absolute',
        top: 0,
        bottom: 0,
        left: 0,
        width: `${width * 100}%`,
        bgcolor: fill,
        opacity,
        transition: 'width 0.4s ease',
      }}
    />
  )
  return (
    <Box
      sx={{
        position: 'relative',
        height,
        borderRadius: 0.75,
        bgcolor: 'action.hover',
        overflow: 'hidden',
      }}
    >
      {b > 0 ? segment(b, 0.28) : null}
      {segment(f, 0.55)}
      {text != null ? (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            lineHeight: 1,
            whiteSpace: 'nowrap',
          }}
        >
          {text}
        </Typography>
      ) : null}
    </Box>
  )
}
