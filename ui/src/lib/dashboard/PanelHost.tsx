// PanelHost lays out and mounts the run-view panels. Each panel is absolutely positioned inside the
// host's relative container according to its `position.anchor` (edges/corners, `center`, or `fill` for
// a full-view background). It also draws the optional panel chrome (title bar + minimize toggle) and
// renders an explicit error for a panel whose `type` isn't registered -- never a silent drop.

import { useState, type CSSProperties } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import RemoveRoundedIcon from '@mui/icons-material/RemoveRounded'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import { getPanel } from './registry'
import type { PanelSpec, PanelPosition } from './types'
import type { PlaybackClock } from './clock'
import type { DataProvider } from './dataProvider'

const len = (v: number | string | undefined, fallback: string): string =>
  v == null ? fallback : typeof v === 'number' ? `${v}px` : v

// Map a panel position to absolute CSS within the (position: relative) host container.
function layoutStyle(pos: PanelPosition, z: number): CSSProperties {
  const w = () => len(pos.width, '360px')
  const h = () => len(pos.height, '320px')
  const base: CSSProperties = { position: 'absolute', zIndex: z }
  switch (pos.anchor ?? 'center') {
    case 'fill':
      return { ...base, inset: 0, zIndex: 0 }
    case 'bottom':
      return { ...base, left: 0, right: 0, bottom: 0, height: h() }
    case 'top':
      return { ...base, left: 0, right: 0, top: 0, height: h() }
    case 'left':
      return { ...base, top: 0, bottom: 0, left: 0, width: w() }
    case 'right':
      return { ...base, top: 0, bottom: 0, right: 0, width: w() }
    case 'top-left':
      return { ...base, top: 12, left: 12, width: w(), height: h() }
    case 'top-right':
      return { ...base, top: 12, right: 12, width: w(), height: h() }
    case 'bottom-left':
      return { ...base, bottom: 12, left: 12, width: w(), height: h() }
    case 'bottom-right':
      return { ...base, bottom: 12, right: 12, width: w(), height: h() }
    case 'center':
    default:
      return {
        ...base,
        top: '50%',
        left: '50%',
        transform: 'translate(-50%, -50%)',
        width: w(),
        height: h(),
      }
  }
}

function PanelFrame({
  spec,
  z,
  clock,
  data,
}: {
  spec: PanelSpec
  z: number
  clock: PlaybackClock
  data: DataProvider
}) {
  const [minimized, setMinimized] = useState(spec.minimized)
  const plugin = getPanel(spec.type)
  const showHeader = spec.minimizable || !!spec.title
  const isFill = spec.position.anchor === 'fill'

  const body = plugin ? (
    <plugin.component spec={spec} clock={clock} data={data} />
  ) : (
    <Box sx={{ p: 2, color: 'error.main', fontSize: 13 }}>
      Unknown panel type “{spec.type}”. Register it or fix the vast <code>visualization.panels</code>.
    </Box>
  )

  return (
    <Paper
      elevation={isFill ? 0 : 3}
      square={isFill}
      sx={{
        ...layoutStyle(spec.position, z),
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        bgcolor: isFill ? 'transparent' : 'background.paper',
        border: isFill ? 0 : 1,
        borderColor: 'divider',
      }}
    >
      {showHeader ? (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            px: 1,
            py: 0.25,
            borderBottom: minimized ? 0 : 1,
            borderColor: 'divider',
            bgcolor: 'action.hover',
          }}
        >
          <Typography variant="caption" sx={{ fontWeight: 600, flexGrow: 1 }}>
            {spec.title ?? plugin?.manifest.label ?? spec.type}
          </Typography>
          {spec.minimizable ? (
            <IconButton size="small" onClick={() => setMinimized((m) => !m)}>
              {minimized ? <AddRoundedIcon fontSize="inherit" /> : <RemoveRoundedIcon fontSize="inherit" />}
            </IconButton>
          ) : null}
        </Box>
      ) : null}
      {!minimized ? <Box sx={{ position: 'relative', flexGrow: 1, minHeight: 0 }}>{body}</Box> : null}
    </Paper>
  )
}

export function PanelHost({
  panels,
  clock,
  data,
}: {
  panels: PanelSpec[]
  clock: PlaybackClock
  data: DataProvider
}) {
  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', overflow: 'hidden' }}>
      {panels
        .filter((p) => !p.hidden)
        .map((spec, i) => (
          <PanelFrame key={i} spec={spec} z={i + 1} clock={clock} data={data} />
        ))}
    </Box>
  )
}
