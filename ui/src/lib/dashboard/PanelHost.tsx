// PanelHost lays out and mounts the run-view panels. Each panel is absolutely positioned inside the
// host's relative container according to its `position.anchor` (edges/corners, `center`, or `fill` for
// a full-view background). It also draws the optional panel chrome (title bar + minimize toggle) and
// renders an explicit error for a panel whose `type` isn't registered -- never a silent drop.

import { useRef, useState, type CSSProperties } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import RemoveRoundedIcon from '@mui/icons-material/RemoveRounded'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import { getPanel } from './registry'
import type { Anchor, PanelSpec, PanelPosition } from './types'
import type { PlaybackClock } from './clock'
import type { DataProvider } from './dataProvider'

const len = (v: number | string | undefined, fallback: string): string =>
  v == null ? fallback : typeof v === 'number' ? `${v}px` : v

const MIN_W = 140
const MIN_H = 90

// Which dimensions a resize handle drives, and the sign mapping mouse delta -> size delta, per anchor.
// The handle sits on the panel's inner edge/corner (the one facing the view centre): a left panel is
// dragged by its right edge (wider as the mouse moves right, x:+1); a bottom-right panel by its
// top-left corner (both dimensions grow as the mouse moves up-left, x/y:-1).
const RESIZE: Partial<Record<Anchor, { x?: 1 | -1; y?: 1 | -1 }>> = {
  left: { x: 1 },
  right: { x: -1 },
  top: { y: 1 },
  bottom: { y: -1 },
  'top-left': { x: 1, y: 1 },
  'top-right': { x: -1, y: 1 },
  'bottom-left': { x: 1, y: -1 },
  'bottom-right': { x: -1, y: -1 },
  center: { x: 1, y: 1 },
}

// Absolute-positioned handle geometry inside the (positioned) Paper for a given resize spec.
function handleSx(rz: { x?: 1 | -1; y?: 1 | -1 }): CSSProperties {
  const base: CSSProperties = { position: 'absolute', zIndex: 2 }
  if (rz.x && rz.y)
    return {
      ...base,
      width: 16,
      height: 16,
      [rz.x === 1 ? 'right' : 'left']: 0,
      [rz.y === 1 ? 'bottom' : 'top']: 0,
      cursor: rz.x * rz.y === 1 ? 'nwse-resize' : 'nesw-resize',
    }
  if (rz.x)
    return { ...base, top: 0, bottom: 0, width: 6, [rz.x === 1 ? 'right' : 'left']: 0, cursor: 'ew-resize' }
  return { ...base, left: 0, right: 0, height: 6, [rz.y === 1 ? 'bottom' : 'top']: 0, cursor: 'ns-resize' }
}

// Map a panel position to absolute CSS within the (position: relative) host container. When
// `minimized`, only the header shows, so the panel must collapse to its content height rather than
// keep the height it would otherwise stretch/fix to -- otherwise a left/right panel stays full-height
// with an empty body.
function layoutStyle(pos: PanelPosition, z: number, minimized: boolean): CSSProperties {
  const w = () => len(pos.width, '360px')
  const h = () => (minimized ? 'auto' : len(pos.height, '320px'))
  const base: CSSProperties = { position: 'absolute', zIndex: z }
  switch (pos.anchor ?? 'center') {
    case 'fill':
      return { ...base, inset: 0, zIndex: 0 }
    case 'bottom':
      return { ...base, left: 0, right: 0, bottom: 0, height: h() }
    case 'top':
      return { ...base, left: 0, right: 0, top: 0, height: h() }
    case 'left':
      return { ...base, top: 0, bottom: minimized ? 'auto' : 0, left: 0, width: w() }
    case 'right':
      return { ...base, top: 0, bottom: minimized ? 'auto' : 0, right: 0, width: w() }
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
  const paperRef = useRef<HTMLDivElement | null>(null)
  // Live size override once the user has dragged a resize handle; starts from the declared position.
  const [size, setSize] = useState<{ w?: number | string; h?: number | string }>({
    w: spec.position.width,
    h: spec.position.height,
  })
  const plugin = getPanel(spec.type)
  const isFill = spec.position.anchor === 'fill'
  // A fill/frameless panel has no Paper chrome and never a header (the playback bar floats directly).
  const frameless = isFill || spec.frameless
  const showHeader = !frameless && (spec.minimizable || !!spec.title)
  const rz = RESIZE[spec.position.anchor ?? 'center']
  const canResize = spec.resizable && !isFill && !minimized && !!rz

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const rect = paperRef.current?.getBoundingClientRect()
    if (!rect || !rz) return
    const { clientX: x0, clientY: y0 } = e
    const { width: w0, height: h0 } = rect
    const onMove = (ev: MouseEvent) => {
      setSize((s) => ({
        ...s,
        ...(rz.x ? { w: Math.max(MIN_W, w0 + rz.x * (ev.clientX - x0)) } : null),
        ...(rz.y ? { h: Math.max(MIN_H, h0 + rz.y * (ev.clientY - y0)) } : null),
      }))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const pos: PanelPosition = { ...spec.position, width: size.w, height: size.h }

  const body = plugin ? (
    <plugin.component spec={spec} clock={clock} data={data} />
  ) : (
    <Box sx={{ p: 2, color: 'error.main', fontSize: 13 }}>
      Unknown panel type “{spec.type}”. Register it or fix the vast <code>visualization.panels</code>.
    </Box>
  )

  return (
    <Paper
      ref={paperRef}
      elevation={frameless ? 0 : 3}
      square={frameless}
      sx={{
        ...layoutStyle(pos, z, minimized),
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        bgcolor: frameless ? 'transparent' : 'background.paper',
        border: frameless ? 0 : 1,
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
      {canResize && rz ? (
        <Box
          onMouseDown={startResize}
          sx={{ ...handleSx(rz), '&:hover': { bgcolor: 'primary.main', opacity: 0.35 } }}
        />
      ) : null}
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
