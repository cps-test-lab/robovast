// PanelHost lays out and mounts the run-view panels. Each panel is absolutely positioned inside the
// host's relative container according to its `position.anchor` (edges/corners, `center`, or `fill` for
// a full-view background). It also draws the optional panel chrome (title bar + minimize toggle) and
// renders an explicit error for a panel whose `type` isn't registered -- never a silent drop.
//
// The declared anchor is the panel's starting place, not a cage: dragging a panel's header moves it
// from there (and drops it where the mouse is released), the same way dragging its resize handle
// overrides the declared size. Both overrides live in this component and last for the mounted view --
// the .vast stays the authored layout.

import { useRef, useState, type CSSProperties, type RefObject } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import RemoveRoundedIcon from '@mui/icons-material/RemoveRounded'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import { getPanel } from './registry'
import type { Anchor, HostPanelProps, PanelSpec, PanelPosition } from './types'
import type { DataProvider, PanelProps, PlaybackClock } from '@robovast/panel-kit'
import { useRemoteComponent } from '@/lib/remote'

const len = (v: number | string | undefined, fallback: string): string =>
  v == null ? fallback : typeof v === 'number' ? `${v}px` : v

const MIN_W = 140
const MIN_H = 90

// How much of a dragged panel has to stay inside the host. The container clips (overflow: hidden), so
// without this a panel could be dropped where nothing of it -- least of all the header you grab it by --
// is left on screen, with no way to get it back short of reloading the view.
const KEEP_X = 80
const KEEP_Y = 28

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

// Which dimensions a resize handle drives, and the sign mapping mouse delta -> size delta, per anchor.
// The handle sits on the panel's free edge/corner -- the one the anchor does NOT pin, so dragging it
// only ever changes the size and never fights the layout: a left panel is dragged by its right edge
// (wider as the mouse moves right, x:+1); a bottom-right panel by its top-left corner (both dimensions
// grow as the mouse moves up-left, x/y:-1).
//
// A left/right column takes both axes even though the anchor pins only its side: with no declared
// height it spans the band, and the first vertical drag is what gives it one (layoutStyle then
// top-anchors it), so the top edge stays put and the bottom edge follows the mouse.
//
// top/bottom bars are horizontally stretched edge to edge -- their width is ignored by layoutStyle --
// so they get the vertical axis alone rather than a handle that would do nothing.
const RESIZE: Partial<Record<Anchor, { x?: 1 | -1; y?: 1 | -1 }>> = {
  left: { x: 1, y: 1 },
  right: { x: -1, y: 1 },
  top: { y: 1 },
  bottom: { y: -1 },
  'top-left': { x: 1, y: 1 },
  'top-right': { x: -1, y: 1 },
  'bottom-left': { x: 1, y: -1 },
  'bottom-right': { x: -1, y: -1 },
  // Centred horizontally with its bottom pinned: the free edges are the right (which grows it
  // both ways, see `gain`) and the top.
  'bottom-center': { x: -1, y: -1 },
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
      // Above the two edge strips it overlaps, so the corner drives both axes where they meet.
      zIndex: 3,
      [rz.x === 1 ? 'right' : 'left']: 0,
      [rz.y === 1 ? 'bottom' : 'top']: 0,
      cursor: rz.x * rz.y === 1 ? 'nwse-resize' : 'nesw-resize',
    }
  if (rz.x)
    return { ...base, top: 0, bottom: 0, width: 8, [rz.x === 1 ? 'right' : 'left']: 0, cursor: 'ew-resize' }
  return { ...base, left: 0, right: 0, height: 8, [rz.y === 1 ? 'bottom' : 'top']: 0, cursor: 'ns-resize' }
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
    case 'bottom-center':
      // Floats above the reserved bottom band (`bottom: B + inset`) rather than docking into
      // it at `bottom: 0` the way `bottom` does -- which is what lets it share the bottom
      // edge with the playback bar instead of covering it. It reserves no inset of its own,
      // so it overlays whatever fills the band behind it.
      //
      // `bottom` is the pinned edge, so the shared `h` above (`auto` when minimized)
      // collapses it *downward* to its header and expands it back *upward*, exactly as the
      // bottom corners already behave. No minimize handling of its own.
      return {
        ...base,
        bottom: B + CORNER_INSET,
        left: '50%',
        transform: 'translateX(-50%)',
        width: w,
        height: h,
        maxHeight: cornerBandH,
      }
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
function RemotePanel({ spec, clock, data }: HostPanelProps) {
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
  hostRef,
  onRaise,
}: {
  spec: PanelSpec
  z: number
  clock: PlaybackClock
  data: DataProvider
  insets: { top: number; bottom: number }
  hostRef: RefObject<HTMLDivElement | null>
  onRaise: () => void
}) {
  const [minimized, setMinimized] = useState(spec.minimized)
  const paperRef = useRef<HTMLDivElement | null>(null)
  // Live size override once the user has dragged a resize handle; starts from the declared position.
  const [size, setSize] = useState<{ w?: number | string; h?: number | string }>({
    w: spec.position.width,
    h: spec.position.height,
  })
  // Live move override, in pixels away from wherever the anchor put the panel. Kept as a translation
  // rather than rewritten left/top so it composes with every anchor -- including the edge anchors,
  // whose offsets are relative to the opposite side, and `center`, which is placed by a transform.
  const [offset, setOffset] = useState({ dx: 0, dy: 0 })
  const plugin = getPanel(spec.type)
  const isFill = spec.position.anchor === 'fill'
  // A fill/frameless panel has no Paper chrome and never a header (e.g. a full-view background).
  const frameless = isFill || spec.frameless
  const showHeader = !frameless && (spec.minimizable || !!spec.title)
  const rz = RESIZE[spec.position.anchor ?? 'center']
  // `fixed: true` locks the whole layout of a panel whose declared geometry is part of the campaign's
  // dashboard; `resizable: false` is the narrower opt-out, for one that may be moved but not resized.
  const canResize = spec.resizable && !spec.fixed && !isFill && !minimized && !!rz
  // The header is the drag handle, so a panel that shows none cannot be moved -- which is what keeps
  // the docked playback bar and the `fill` 3D background in place.
  const canMove = showHeader && !spec.fixed

  // `use` is the subset of the anchor's axes this handle drives -- an edge strip drives one, the
  // corner where they meet drives both.
  const startResize = (e: React.MouseEvent, use: { x?: 1 | -1; y?: 1 | -1 }) => {
    e.preventDefault()
    e.stopPropagation()
    const rect = paperRef.current?.getBoundingClientRect()
    if (!rect) return
    onRaise()
    const { clientX: x0, clientY: y0 } = e
    const { width: w0, height: h0 } = rect
    // A panel centred on an axis grows away from its centre, so that edge moves half of what
    // the size does; without this the handle drifts out from under the cursor at half the drag
    // speed. `bottom-center` is centred horizontally only -- its bottom is pinned, so its
    // vertical gain is 1 while `center`'s is 2 on both axes.
    const anchor = spec.position.anchor ?? 'center'
    const gainX = anchor === 'center' || anchor === 'bottom-center' ? 2 : 1
    const gainY = anchor === 'center' ? 2 : 1
    const onMove = (ev: MouseEvent) => {
      setSize((s) => ({
        ...s,
        ...(use.x ? { w: Math.max(MIN_W, w0 + gainX * use.x * (ev.clientX - x0)) } : null),
        ...(use.y ? { h: Math.max(MIN_H, h0 + gainY * use.y * (ev.clientY - y0)) } : null),
      }))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const startMove = (e: React.MouseEvent) => {
    // The header also carries the minimize toggle: a press there is a click on the button, not a grab.
    if (e.button !== 0 || (e.target as HTMLElement).closest('button')) return
    e.preventDefault() // suppresses the text selection a drag across the view would otherwise paint
    onRaise()
    const rect = paperRef.current?.getBoundingClientRect()
    const bounds = hostRef.current?.getBoundingClientRect()
    if (!rect || !bounds) return
    const { clientX: x0, clientY: y0 } = e
    const from = offset
    const onMouseMove = (ev: MouseEvent) => {
      // Clamp in viewport coordinates (where both rects live), then convert back to an offset.
      const left = clamp(
        rect.left + ev.clientX - x0,
        bounds.left + KEEP_X - rect.width,
        bounds.right - KEEP_X,
      )
      const top = clamp(rect.top + ev.clientY - y0, bounds.top, bounds.bottom - KEEP_Y)
      setOffset({ dx: from.dx + left - rect.left, dy: from.dy + top - rect.top })
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onUp)
  }

  // One strip per axis the anchor leaves free, plus the corner where two of them meet -- so a panel
  // is grabbed anywhere along its free edge, not only at the corner.
  const handles: { x?: 1 | -1; y?: 1 | -1 }[] =
    canResize && rz
      ? [
          ...(rz.x ? [{ x: rz.x }] : []),
          ...(rz.y ? [{ y: rz.y }] : []),
          ...(rz.x && rz.y ? [rz] : []),
        ]
      : []

  const pos: PanelPosition = { ...spec.position, width: size.w, height: size.h }
  const layout = layoutStyle(pos, z, minimized, insets)
  if (offset.dx || offset.dy) {
    // After the anchor's own transform, so `center`'s translate(-50%, -50%) still centres the panel
    // and the drag displaces it from there.
    const move = `translate(${offset.dx}px, ${offset.dy}px)`
    layout.transform = layout.transform ? `${layout.transform} ${move}` : move
  }

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
        ...layout,
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
          onMouseDown={canMove ? startMove : undefined}
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            px: 1,
            py: 0.25,
            borderBottom: minimized ? 0 : 1,
            borderColor: 'divider',
            bgcolor: 'action.hover',
            ...(canMove ? { cursor: 'move', userSelect: 'none' } : null),
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
      {handles.map((use, i) => (
        <Box
          key={i}
          onMouseDown={(e) => startResize(e, use)}
          sx={{ ...handleSx(use), '&:hover': { bgcolor: 'primary.main', opacity: 0.35 } }}
        />
      ))}
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
  const hostRef = useRef<HTMLDivElement | null>(null)
  // The panel last grabbed by its header, raised above the rest. Panels otherwise stack in declaration
  // order, which is a layout the author chose for panels that do not overlap -- once one is dragged
  // over another, the one being moved is the one you mean to see.
  const [front, setFront] = useState(-1)
  const visible = panels.filter((p) => !p.hidden)
  // Reserve the height of the full-width top/bottom bars so nothing else overlaps them.
  const px = (v: number | string | undefined) => (typeof v === 'number' ? v : 0)
  const insets = { top: 0, bottom: 0 }
  for (const p of visible) {
    if (p.position.anchor === 'top') insets.top += px(p.position.height)
    else if (p.position.anchor === 'bottom') insets.bottom += px(p.position.height)
  }
  return (
    <Box
      ref={hostRef}
      sx={{ position: 'relative', width: '100%', height: '100%', overflow: 'hidden' }}
    >
      {visible.map((spec, i) => (
        // Key on the spec so only a panel whose declaration actually changed remounts (and re-fits);
        // editing one panel must not reset another's view (e.g. the costmap's pan/zoom).
        <PanelFrame
          key={`${i}:${JSON.stringify(spec)}`}
          spec={spec}
          z={i + 1 + (i === front ? visible.length : 0)}
          clock={clock}
          data={data}
          insets={insets}
          hostRef={hostRef}
          onRaise={() => setFront(i)}
        />
      ))}
    </Box>
  )
}
