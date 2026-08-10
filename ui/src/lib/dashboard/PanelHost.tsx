// PanelHost lays out and mounts the run-view panels. Each panel is absolutely positioned inside the
// host's relative container according to its `position` -- an `anchor` (edges/corners, centred along
// an edge, or `center`), or `fill` for the panel that takes whatever the docked panels leave over.
// It also draws the optional panel chrome (title bar + minimize toggle) and renders an explicit error
// for a panel whose `type` isn't registered -- never a silent drop.
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
// Size a panel falls back to when it declares none. Used by both the layout and the space a dock
// reserves, so the two cannot disagree about how big an undeclared bar is.
const DEF_W = '360px'
const DEF_H = '320px'
// What a minimized panel collapses to: its header row alone.
const HEADER_H = 28

// How much of a dragged panel has to stay inside the host. The container clips (overflow: hidden), so
// without this a panel could be dropped where nothing of it -- least of all the header you grab it by --
// is left on screen, with no way to get it back short of reloading the view. Vertically that is the
// header itself, so the two are the same number.
const KEEP_X = 80
const KEEP_Y = HEADER_H

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

// Which dimensions a panel can be resized along, and which edge of each axis the anchor leaves FREE
// -- the one it does not pin, so growing the panel moves it: a left panel's right edge (x:+1), a
// bottom-right panel's top-left corner (x/y:-1).
//
// Every edge gets a handle, not just the free one. Dragging an edge always moves that edge and
// leaves the opposite one where it is; on the pinned edge that takes a translation as well as a
// resize, because CSS would otherwise grow the panel away from the side the anchor holds (see
// `startResize`). This map is what says which edge that is.
//
// A left/right column takes both axes even though the anchor pins only its side: with no declared
// height it spans the band, and the first vertical drag is what gives it one (layoutStyle then
// top-anchors it), so the top edge stays put and the bottom edge follows the mouse.
//
// top/bottom bars are horizontally stretched edge to edge -- their width is ignored by layoutStyle --
// so they get the vertical axis alone rather than handles that would do nothing.
const RESIZE: Partial<Record<Anchor, Edge>> = {
  left: { x: 1, y: 1 },
  right: { x: -1, y: 1 },
  top: { y: 1 },
  bottom: { y: -1 },
  'top-left': { x: 1, y: 1 },
  'top-right': { x: -1, y: 1 },
  'bottom-left': { x: 1, y: -1 },
  'bottom-right': { x: -1, y: -1 },
  // Centred along one axis with the opposite edge pinned: the free edges are the one facing away
  // from the pinned edge, and either side along the centred axis (which grows it both ways, see
  // `CENTRED`).
  'top-center': { x: 1, y: 1 },
  'bottom-center': { x: -1, y: -1 },
  'left-center': { x: 1, y: 1 },
  'right-center': { x: -1, y: 1 },
  center: { x: 1, y: 1 },
}

// Which axes an anchor centres the panel on. A centred edge moves half of what the size does, so a
// resize drag doubles its delta on that axis to keep the handle under the cursor. Kept beside RESIZE
// so a new anchor declares both in one place instead of being forgotten in the drag handler.
const CENTRED: Partial<Record<Anchor, { x?: true; y?: true }>> = {
  center: { x: true, y: true },
  'top-center': { x: true },
  'bottom-center': { x: true },
  'left-center': { y: true },
  'right-center': { y: true },
}

/** A box edge, as the sign of the direction it faces: x +1 right / -1 left, y +1 bottom / -1 top.
 *  A corner handle carries both. Used for the resize handles and for the RESIZE map's free edge. */
type Edge = { x?: 1 | -1; y?: 1 | -1 }

// Absolute-positioned handle geometry inside the (positioned) Paper for a given resize spec.
function handleSx(rz: Edge): CSSProperties {
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

// Full-width top/bottom bars and left/right columns dock at the very edge and reserve the space they
// occupy (`insets`); every other panel is laid out in the rectangle they leave over, so a docked bar
// (e.g. the playback transport) is never occluded. A declared width/height is honoured where it fits
// that rectangle, else the panel fills its column. `center` is centred in it, a `fill` panel covers
// it. `minimized` collapses to the header.
const CORNER_INSET = 12
// Breathing room between neighbouring docked panels: a dock reserves its own size PLUS this, so
// whatever is laid out next to it clears it, and a column's members are separated by it. Panels
// still sit flush against the view's outer edges -- the gap is between panels, not a page margin.
const PANEL_GAP = 8
const GAP = `${PANEL_GAP}px`

/** The sides a panel can dock against. */
type Side = 'top' | 'bottom' | 'left' | 'right'
const dockSide = (a: Anchor | undefined): Side | null =>
  a === 'top' || a === 'bottom' || a === 'left' || a === 'right' ? a : null

/** What each side reserves, as one CSS length ("40px", "max(320px, 25%)", "calc(40px + 12px)").
 *  Composed per side rather than added numerically because the sizes may mix units, and because the
 *  two kinds of side compose differently (see the loop in PanelHost). A percentage is left for the
 *  browser to resolve against the host box -- its height for top/bottom, its width for left/right --
 *  which is what a percentage-sized dock means, and is why such a dock used to reserve nothing. */
type Insets = Record<Side, string>

/** Where a docked panel sits along its own side, and what a fraction of its column means.
 *  `offset` is what the same-side docks before it take up, gaps included; `band` is the column's
 *  height once the gaps between its members are removed, so members sized in percent still tile it
 *  exactly. Both are meaningless for a panel that does not dock, which gets zero and the full band. */
interface Dock {
  offset: string
  band: string
}

const ZERO = '0px'
const PX = /^-?\d+(?:\.\d+)?px$/
/** Adds up the plain-pixel terms, so the common all-pixel case stays a plain length instead of a
 *  calc() the browser would fold anyway -- the generated CSS is read in devtools when a layout looks
 *  wrong, and `328px` says more there than `calc(320px + 8px)`. */
const foldPx = (terms: string[]): string[] => {
  const px = terms.filter((t) => PX.test(t))
  if (px.length < 2) return terms
  const sum = px.reduce((n, t) => n + parseFloat(t), 0)
  return [`${sum}px`, ...terms.filter((t) => !PX.test(t))]
}
/** The terms as one length: calc(a + b), the single term, or 0. */
const plus = (...terms: string[]): string => {
  const t = foldPx(terms.filter((x) => x && x !== ZERO))
  return t.length === 0 ? ZERO : t.length === 1 ? t[0] : `calc(${t.join(' + ')})`
}
/** The widest of the terms: a column is one gutter, as wide as its widest member. Duplicates are
 *  dropped so the usual column -- every member the same width -- stays a plain length. */
const widest = (terms: string[]): string => {
  const t = [...new Set(terms)]
  return t.length === 0 ? ZERO : t.length === 1 ? t[0] : `max(${t.join(', ')})`
}
/** What is left of the host along one axis: calc(100% - a - b). */
const minus = (...parts: string[]): string => {
  const t = foldPx(parts.filter((x) => x && x !== ZERO))
  return t.length === 0 ? '100%' : `calc(100% - ${t.join(' - ')})`
}
/** Centre of the free band along one axis, as (100% + near - far) / 2 -- the algebraic form of "the
 *  near inset plus half of what is left", which keeps it one flat calc(). */
const mid = (near: string, far: string): string => {
  const terms = (near !== ZERO ? ` + ${near}` : '') + (far !== ZERO ? ` - ${far}` : '')
  return terms === '' ? '50%' : `calc((100%${terms}) / 2)`
}
/** A column member's height. A bare percentage is taken of the COLUMN -- the band between the bars
 *  -- not of the host: "50%" in a column means half the column, which is what the author means and
 *  what they otherwise have to write out as calc(50% - <every bar's height>). Anything else (pixels,
 *  or an explicit calc()) passes through untouched. */
const columnHeight = (v: number | string | undefined, band: string): string | undefined => {
  if (v == null) return undefined
  if (typeof v === 'number') return `${v}px`
  const pct = /^\s*(\d+(?:\.\d+)?)%\s*$/.exec(v)
  if (!pct) return v
  // Unwrap the band's own calc() before scaling it, so the result is one flat expression rather
  // than a nested one. The parentheses are load-bearing: `100% - 40px * 0.5` would scale the
  // subtrahend alone.
  const inner = band.startsWith('calc(') && band.endsWith(')') ? band.slice(5, -1) : band
  return `calc((${inner}) * ${Number(pct[1]) / 100})`
}

function layoutStyle(
  pos: PanelPosition,
  z: number,
  minimized: boolean,
  ins: Insets,
  dock: Dock,
): CSSProperties {
  const { offset: off, band: columnBand } = dock
  const base: CSSProperties = { position: 'absolute', zIndex: z }
  const anchor = pos.anchor ?? 'center'
  const w = len(pos.width, DEF_W)
  const h = minimized ? 'auto' : len(pos.height, DEF_H)
  const { top: T, bottom: B, left: L, right: R } = ins
  const CI = `${CORNER_INSET}px`
  const bandH = minus(T, B)
  const bandW = minus(L, R)
  const cornerBandH = minus(T, B, `${2 * CORNER_INSET}px`)
  const cornerBandW = minus(L, R, `${2 * CORNER_INSET}px`)
  // A floating panel (a corner or an edge-centre): the edges it pins, plus its declared size clamped
  // to the rectangle the docks leave over, so an oversized one cannot spill across them.
  const float = (edges: CSSProperties): CSSProperties => ({
    ...base,
    ...edges,
    width: w,
    height: h,
    maxWidth: cornerBandW,
    maxHeight: cornerBandH,
  })

  // The panel that takes what is left over. Handled before the switch because it declares no anchor
  // (which the switch would read as `center`), and kept at the bottom of the stack: a panel spanning
  // the whole free rectangle is by definition the layer the floating ones sit on.
  if (pos.fill) {
    const box = { ...base, zIndex: 0, top: T, left: L, right: R }
    return minimized ? box : { ...box, bottom: B }
  }

  switch (anchor) {
    // A bar owns the full width, so bars can only stack along the inset axis: `off` is what the bars
    // declared before it on the same side take, which is why the side reserves their sum.
    case 'top':
      return { ...base, left: 0, right: 0, top: off, height: h }
    case 'bottom':
      return { ...base, left: 0, right: 0, bottom: off, height: h }
    case 'left':
    case 'right': {
      // A column owns the full height, so its members stack *inside* one gutter rather than beside
      // it: they share the gutter's edge, and `off` is how far down it the ones above reach. A
      // member with a declared height gets it (already resolved against the column by
      // `columnHeight`), and one without takes the rest of the column.
      const side = anchor === 'right' ? { right: 0 } : { left: 0 }
      const top = plus(T, off)
      const vertical = minimized
        ? { top }
        : pos.height != null
          ? { top, height: columnHeight(pos.height, columnBand), maxHeight: bandH }
          : { top, bottom: B }
      return { ...base, ...side, width: w, ...vertical }
    }
    // The corners and the edge-centres float *inside* the reserved bands rather than docking into
    // them, so they sit beside a column instead of over it. They reserve nothing of their own, and
    // overlay whatever fills the rectangle behind them.
    case 'top-left':
      return float({ top: plus(T, CI), left: plus(L, CI) })
    case 'top-right':
      return float({ top: plus(T, CI), right: plus(R, CI) })
    case 'bottom-left':
      return float({ bottom: plus(B, CI), left: plus(L, CI) })
    case 'bottom-right':
      return float({ bottom: plus(B, CI), right: plus(R, CI) })
    // Centred along one edge. The pinned edge is the one it is named after, so the shared `h` above
    // (`auto` when minimized) collapses it *towards* that edge and expands it back away from it,
    // exactly as the adjacent corners already behave. No minimize handling of its own.
    case 'top-center':
      return float({ top: plus(T, CI), left: mid(L, R), transform: 'translateX(-50%)' })
    case 'bottom-center':
      return float({ bottom: plus(B, CI), left: mid(L, R), transform: 'translateX(-50%)' })
    case 'left-center':
      return float({ left: plus(L, CI), top: mid(T, B), transform: 'translateY(-50%)' })
    case 'right-center':
      return float({ right: plus(R, CI), top: mid(T, B), transform: 'translateY(-50%)' })
    case 'center':
    default:
      return {
        ...base,
        top: mid(T, B),
        left: mid(L, R),
        transform: 'translate(-50%, -50%)',
        width: w,
        height: h,
        maxWidth: bandW,
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
  dock,
  hostRef,
  onRaise,
}: {
  spec: PanelSpec
  z: number
  clock: PlaybackClock
  data: DataProvider
  insets: Insets
  dock: Dock
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
  // A frameless panel has no Paper chrome and never a header (e.g. the scene3d base layer, which
  // declares it in its manifest).
  const frameless = spec.frameless
  const showHeader = !frameless && (spec.minimizable || !!spec.title)
  const rz = RESIZE[spec.position.anchor ?? 'center']
  // `fixed: true` locks the whole layout of a panel whose declared geometry is part of the campaign's
  // dashboard; `resizable: false` is the narrower opt-out, for one that may be moved but not resized.
  // A filling panel is sized by the docks around it and has no free edge to drag.
  const canResize = spec.resizable && !spec.fixed && !spec.position.fill && !minimized && !!rz
  // The header is the drag handle, so a panel that shows none cannot be moved -- which is what keeps
  // the docked playback bar and the `fill` 3D background in place.
  const canMove = showHeader && !spec.fixed

  // `use` names the box edge being dragged -- x:+1 right, x:-1 left, y:+1 bottom, y:-1 top; a corner
  // handle carries both. The size delta is `edge * mouse delta` either way, so the same arithmetic
  // drives all four edges.
  const startResize = (e: React.MouseEvent, use: Edge) => {
    e.preventDefault()
    e.stopPropagation()
    const rect = paperRef.current?.getBoundingClientRect()
    if (!rect) return
    onRaise()
    const { clientX: x0, clientY: y0 } = e
    const { width: w0, height: h0 } = rect
    // A panel centred on an axis grows away from its centre, so that edge moves half of what
    // the size does; without this the handle drifts out from under the cursor at half the drag
    // speed. Which axes those are is the anchor's own property -- see CENTRED.
    const centred = CENTRED[spec.position.anchor ?? 'center'] ?? {}
    const gainX = centred.x ? 2 : 1
    const gainY = centred.y ? 2 : 1
    // Is this handle on the edge the anchor PINS? Resizing then grows the panel from the opposite
    // side, so the pinned edge would not follow the mouse without a matching translation. A centred
    // axis pins neither edge -- it grows both ways on its own.
    const pinX = !centred.x && !!use.x && use.x === -(rz?.x ?? 0)
    const pinY = !centred.y && !!use.y && use.y === -(rz?.y ?? 0)
    const from = offset
    const onMove = (ev: MouseEvent) => {
      const w = use.x ? Math.max(MIN_W, w0 + gainX * use.x * (ev.clientX - x0)) : w0
      const h = use.y ? Math.max(MIN_H, h0 + gainY * use.y * (ev.clientY - y0)) : h0
      setSize((s) => ({ ...s, ...(use.x ? { w } : null), ...(use.y ? { h } : null) }))
      // Translate by what the size actually took, not by the raw mouse delta -- otherwise the panel
      // keeps sliding after it has hit MIN_W/MIN_H and the drag stops resizing it.
      if (pinX || pinY) {
        setOffset({
          dx: from.dx + (pinX ? use.x! * (w - w0) : 0),
          dy: from.dy + (pinY ? use.y! * (h - h0) : 0),
        })
      }
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

  // A strip along every edge of each axis the panel can be resized along, plus the corners where two
  // of them meet -- so a panel is grabbed by whichever edge is nearest, not only the one facing the
  // middle of the view. An axis the anchor gives no size (a full-width bar's width) has no handle,
  // because dragging it could not change anything.
  const SIDES: (1 | -1)[] = [1, -1]
  const handles: Edge[] =
    canResize && rz
      ? [
          ...(rz.x ? SIDES.map((x) => ({ x })) : []),
          ...(rz.y ? SIDES.map((y) => ({ y })) : []),
          ...(rz.x && rz.y ? SIDES.flatMap((x) => SIDES.map((y) => ({ x, y }))) : []),
        ]
      : []

  const pos: PanelPosition = { ...spec.position, width: size.w, height: size.h }
  const layout = layoutStyle(pos, z, minimized, insets, dock)
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
  // Docked panels always stack vertically, but the two kinds of side reserve differently. A bar owns
  // the full width, so bars stack along the inset axis and their side reserves the SUM of their
  // heights. A column owns the full height, so its members stack inside one gutter and their side
  // reserves the WIDEST of them, once. Read from the DECLARED layout: a view-local resize or
  // minimize of a dock does not reflow the others until the view is reloaded.
  //
  // Two passes, because a column member's height may be a fraction of the column, which is not known
  // until every bar has been counted. `stacked` must hold the resolved heights the boxes use, or the
  // members drift away from the offsets placing them.
  // Every dock's footprint is its own size PLUS the gap, so the sums below place its neighbour clear
  // of it: the next bar down, the next member of its column, and -- through `insets` -- everything
  // laid out in the rectangle the docks leave over.
  const barHeight = (p: PanelSpec) =>
    p.minimized ? `${HEADER_H}px` : len(p.position.height, DEF_H)
  const bars = { top: [] as string[], bottom: [] as string[] }
  const memberCount = { left: 0, right: 0 }
  for (const p of visible) {
    const side = dockSide(p.position.anchor)
    if (side === 'top' || side === 'bottom') bars[side].push(plus(barHeight(p), GAP))
    else if (side) memberCount[side] += 1
  }
  const T0 = plus(...bars.top)
  const B0 = plus(...bars.bottom)
  // A fraction of a column is a fraction of what its members actually get to share, which is the
  // band minus the gaps between them -- so two members at "50%" still tile it exactly.
  const columnBand = (side: 'left' | 'right') =>
    minus(T0, B0, memberCount[side] > 1 ? `${(memberCount[side] - 1) * PANEL_GAP}px` : ZERO)

  const widths = { left: [] as string[], right: [] as string[] }
  const stacked: Record<Side, string[]> = { top: [], bottom: [], left: [], right: [] }
  const docks: Dock[] = [] // per visible panel, where it sits along its side and what its column is
  for (const p of visible) {
    const side = dockSide(p.position.anchor)
    const band = side === 'left' || side === 'right' ? columnBand(side) : minus(T0, B0)
    docks.push({ offset: side ? plus(...stacked[side]) : ZERO, band })
    if (!side) continue
    if (side === 'left' || side === 'right') {
      widths[side].push(plus(len(p.position.width, DEF_W), GAP))
      // A member with no declared height takes the rest of the column, so there is no height to
      // stack behind it -- and by the same token nothing may follow it, which the config validator
      // is what enforces.
      const h = p.minimized ? `${HEADER_H}px` : columnHeight(p.position.height, band)
      if (h) stacked[side].push(plus(h, GAP))
    } else {
      stacked[side].push(plus(barHeight(p), GAP))
    }
  }
  const insets: Insets = {
    top: T0,
    bottom: B0,
    left: widest(widths.left),
    right: widest(widths.right),
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
          dock={docks[i]}
          hostRef={hostRef}
          onRaise={() => setFront(i)}
        />
      ))}
    </Box>
  )
}
