import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

// The two pieces every hand-drawn chart in this app needs, shared rather than duplicated.
//
// They started out private to `DetailsCharts.tsx`, which was right while the Details panel was the
// only place drawing charts by hand. `BatchObjectiveChart` also renders in the campaign card's live
// view, and having it import from the Details module would make the campaign card depend on a file
// whose whole subject is a different panel. So the shared bits moved down here and both import them.

/** Where a chart would mislead, say why in its place — same footprint, no axes around nothing.
 *
 *  An empty chart is worse than no chart: an axis pair around nothing reads as "measured, and it was
 *  zero" rather than "there is nothing to measure yet". */
export function Note({ height, children }: { height: number; children: ReactNode }) {
  return (
    <Box sx={{ height, display: 'flex', alignItems: 'center' }}>
      <Typography variant="caption" color="text.secondary">
        {children}
      </Typography>
    </Box>
  )
}

/** The two ends of a shared axis, written out as ordinary text in a theme colour. */
export function AxisEnds({ left, right }: { left: string; right: string }) {
  return (
    <Stack direction="row" justifyContent="space-between" sx={{ mt: 0.25 }}>
      <Typography variant="caption" color="text.secondary" sx={{ fontSize: 9 }}>
        {left}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ fontSize: 9 }}>
        {right}
      </Typography>
    </Stack>
  )
}
