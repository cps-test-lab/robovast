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
import type { Anchor, PanelSpec, PanelPosition, PanelProps } from './types'
import type { PlaybackClock } from './clock'
import type { DataProvider } from './dataProvider'
import { useRemoteComponent } from '@/lib/remote'

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

// Full-width top/bottom bars dock at the very edge and reserve their height (`insets`); every other
// panel is laid out in the band between them, so a docked bar (e.g. the playback transport) is never
// occluded. A declared width/height is honoured where it fits the band, else the panel fills its
// column. `center` is centred in the band, `fill` covers it. `minimized` collapses to the header.
const CORNER_INSET = 12

function layoutStyle(
  pos: PanelPosition,
  z: number,
  minimized: boolean,
  insets: { top: number; bottom: number },
): CSSProperties {
  const base: CSSProperties = { position: 'absolute', zIndex: z }
  const anchor = pos.anchor ?? 'center'
  const w = len(pos.width, '360px')
  const h = minimized ? 'auto' : len(pos.height, '320px')
  const { top: T, bottom: B } = insets
  const bandH = `calc(100% - ${T + B}px)`
  const cornerBandH = `calc(100% - ${T + B + 2 * CORNER_INSET}px)`

  switch (anchor) {
    case 'fill':
      return { ...base, top: T, bottom: B, left: 0, right: 0, zIndex: 0 }
    case 'top':
      return { ...base, left: 0, right: 0, top: 0, height: h }
    case 'bottom':
      return { ...base, left: 0, right: 0, bottom: 0, height: h }
    case 'left':
    case 'right': {
      const side = anchor === 'right' ? { right: 0 } : { left: 0 }
      // Column lives inside the band; a declared height caps it (top-anchored), else it fills.
      const vertical = minimized
        ? { top: T }
        : pos.height != null
          ? { top: T, height: h, maxHeight: bandH }
          : { top: T, bottom: B }
      return { ...base, ...side, width: w, ...vertical }
    }
    case 'top-left':
      return { ...base, top: T + CORNER_INSET, left: CORNER_INSET, width: w, height: h, maxHeight: cornerBandH }
    case 'top-right':
      return { ...base, top: T + CORNER_INSET, right: CORNER_INSET, width: w, height: h, maxHeight: cornerBandH }
    case 'bottom-left':
      return { ...base, bottom: B + CORNER_INSET, left: CORNER_INSET, width: w, height: h, maxHeight: cornerBandH }
    case 'bottom-right':
      return { ...base, bottom: B + CORNER_INSET, right: CORNER_INSET, width: w, height: h, maxHeight: cornerBandH }
    case 'center':
    default:
      return {
        ...base,
        top: `calc(${T}px + (100% - ${T + B}px) / 2)`,
        left: '50%',
        transform: 'translate(-50%, -50%)',
        width: w,
        height: h,
        maxHeight: bandH,
      }
  }
}

// Loads a Module-Federation remote panel and mounts it with the full PanelProps contract
// (spec + clock + data), exactly like a built-in panel — so a remote panel is time-synced and
// queries the run's data.db the same way. Guarded loading/error states, never a silent drop.
function RemotePanel({ spec, clock, data }: PanelProps) {
  const { Comp, err } = useRemoteComponent<PanelProps>(spec.remote!)
  // Built-ins a remote can render rather than reimplement (see PanelBuiltins). Read from the
  // registry at mount rather than threaded down from PanelFrame: the registry is already the
  // one place a panel component lives, so there is no second list to keep in step.
  const builtins = { ScenarioTree: getPanel('scenario_tree')?.component }
  if (err) {
    return (
      <Box sx={{ p: 2, color: 'error.main', fontSize: 13 }}>
        Panel “{spec.remote!.name}” failed to load ({err}). Check the panel bundle / its providing plugin.
      </Box>
    )
  }
  if (!Comp) {
    return <Box sx={{ p: 2, color: 'text.secondary', fontSize: 13 }}>loading panel “{spec.remote!.name}”…</Box>
  }
  return <Comp spec={spec} clock={clock} data={data} builtins={builtins} />
}

function PanelFrame({
  spec,
  z,
  clock,
  data,
  insets,
}: {
  spec: PanelSpec
  z: number
  clock: PlaybackClock
  data: DataProvider
  insets: { top: number; bottom: number }
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
  // A fill/frameless panel has no Paper chrome and never a header (e.g. a full-view background).
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

  // A remote panel (package-provided or user-authored `custom`) is loaded at runtime via
  // Module Federation; a built-in panel comes from the static registry; anything else is an
  // explicit error (never a silent drop).
  const body = spec.remote ? (
    <RemotePanel spec={spec} clock={clock} data={data} />
  ) : plugin ? (
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
        ...layoutStyle(pos, z, minimized, insets),
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
  const visible = panels.filter((p) => !p.hidden)
  // Reserve the height of the full-width top/bottom bars so nothing else overlaps them.
  const px = (v: number | string | undefined) => (typeof v === 'number' ? v : 0)
  const insets = { top: 0, bottom: 0 }
  for (const p of visible) {
    if (p.position.anchor === 'top') insets.top += px(p.position.height)
    else if (p.position.anchor === 'bottom') insets.bottom += px(p.position.height)
  }
  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', overflow: 'hidden' }}>
      {visible.map((spec, i) => (
        // Key on the spec so only a panel whose declaration actually changed remounts (and re-fits);
        // editing one panel must not reset another's view (e.g. the costmap's pan/zoom).
        <PanelFrame
          key={`${i}:${JSON.stringify(spec)}`}
          spec={spec}
          z={i + 1}
          clock={clock}
          data={data}
          insets={insets}
        />
      ))}
    </Box>
  )
}
