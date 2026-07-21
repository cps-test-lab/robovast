import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'

function clamp01(x: number): number {
  return Math.min(1, Math.max(0, x))
}

// One region of a stacked meter (see `segments` below): a share of the track
// (0..1) in its own color. `striped` animates a diagonal hatch so an "active"
// segment (e.g. running) reads as in-progress rather than done.
export interface MeterSegment {
  fraction: number
  color: string
  opacity?: number
  striped?: boolean
}

// A compact horizontal meter. Two ways to fill it:
//   • `fraction` (0..1), optionally with a lighter `buffer` segment layered beneath
//     to show "in-progress on top of done"; `color` fixes the fill or it auto-
//     thresholds green → amber → red. Used by the sidebar usage / budget bars.
//   • `segments`: distinct regions laid end-to-end left→right (e.g. completed /
//     failed / running), each its own color. When given, it supersedes
//     fraction/buffer. Used by the campaign run bar.
// `text` overlays centered on the bar.
export function MeterBar({
  fraction,
  buffer,
  color,
  height = 16,
  text,
  segments,
}: {
  fraction?: number
  buffer?: number
  color?: string
  height?: number
  text?: ReactNode
  segments?: MeterSegment[]
}) {
  const f = clamp01(fraction ?? 0)
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
  // Lay the stacked regions end-to-end, tracking the running left offset so each
  // sits after the previous. A `striped` region gets an animated diagonal hatch.
  let offset = 0
  const stacked = segments?.map((s, i) => {
    const w = clamp01(s.fraction)
    const left = offset
    offset = clamp01(offset + w)
    return (
      <Box
        key={i}
        sx={{
          position: 'absolute',
          top: 0,
          bottom: 0,
          left: `${left * 100}%`,
          width: `${w * 100}%`,
          bgcolor: s.color,
          opacity: s.opacity ?? 0.85,
          transition: 'left 0.4s ease, width 0.4s ease',
          ...(s.striped
            ? {
                backgroundImage:
                  'linear-gradient(45deg, rgba(255,255,255,0.35) 25%, transparent 25%,' +
                  ' transparent 50%, rgba(255,255,255,0.35) 50%, rgba(255,255,255,0.35) 75%,' +
                  ' transparent 75%, transparent)',
                backgroundSize: '0.6rem 0.6rem',
                animation: 'meterStripes 0.8s linear infinite',
                '@keyframes meterStripes': {
                  from: { backgroundPosition: '0 0' },
                  to: { backgroundPosition: '0.6rem 0' },
                },
              }
            : {}),
        }}
      />
    )
  })
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
      {stacked ?? (
        <>
          {b > 0 ? segment(b, 0.28) : null}
          {segment(f, 0.55)}
        </>
      )}
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
