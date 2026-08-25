// The merged run log, rendered. Used by two hosts that differ in exactly one thing: the
// run-view panel passes a `cursor` (the playback position) and the Explorer tab does not.
//
// With a cursor the view *follows* it: rows not yet logged are greyed, a divider marks the
// position, and a button appears to jump back to it once you have scrolled away. Without one
// it degrades to a plain filtered log -- no greying, no divider, no jump -- rather than
// pretending to a position it does not have.
//
// Rendering is windowed and every row is one line high, which is not an optimisation but the
// mechanism: knowing where a row *is* (`index * ROW_H`) is what makes both the greying and
// scroll-to-cursor possible without measuring anything, and what lets a 50k-line log scroll.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Fab from '@mui/material/Fab'
import CircularProgress from '@mui/material/CircularProgress'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import ArrowDownwardRoundedIcon from '@mui/icons-material/ArrowDownwardRounded'
import ArrowUpwardRoundedIcon from '@mui/icons-material/ArrowUpwardRounded'
import { lastAtOrBefore } from '@robovast/panel-kit'
import { containerColorer } from '../containerColor'
import { parseAnsi, stripAnsi } from './ansi'
import { LogFilterBar } from './LogFilterBar'
import {
  compileFilter,
  EMPTY_FILTER,
  facetsOf,
  highlightOf,
  type LogFilter,
} from './logFilter'
import type { LogRow, RunLogData } from './useRunLog'

/** One row's height in px. Fixed, so a row's position is arithmetic rather than a measurement. */
const ROW_H = 18

/** Characters of the node column. Exact in this monospace face, so it is also the length at
 *  which a name is truncated and earns a tooltip. */
const NODE_CHARS = 12

/** How long the pointer must rest on a truncated node before its full name appears.
 *
 *  Deliberately slower than MUI's default. This panel is read by dragging across it -- selecting
 *  a line to paste into an issue is the common gesture, and `hasSelection` exists because even
 *  auto-scroll had to be stopped from fighting it. On the usual hover delay the tooltip flashes
 *  up mid-drag over every column it crosses; a second is past a drag's pace while still being
 *  the pause of someone who stopped to look. */
const NODE_TIP_MS = 1000
/** Rows rendered beyond the viewport, so a fast scroll does not show blank strips. */
const OVERSCAN = 12

/** How many lines word wrap will take on. Wrapping gives up the fixed row height windowing
 *  depends on, so it renders every filtered row at once; past this the tab would stall, so the
 *  toggle is disabled with the reason on it instead. */
import { ERROR, WARNING } from '@/colors'
const WRAP_MAX_ROWS = 5000

const SEVERITY_COLOR = { warn: WARNING, error: ERROR } as const

/** One line for an unwrapped row, saying what it left out.
 *
 *  The merge deliberately folds a multi-line event into one row (a traceback is one thing that
 *  happened, not forty). Unwrapped, a row is one line by construction, so the rest is summarized
 *  rather than clipped in silence — turn wrap on to read it. */
export function flatten(message: string): string {
  if (!message.includes('\n')) return message
  // The first NON-EMPTY line, not the first line: nav2's lifecycle messages begin with a newline
  // ("\n\tmap_server lifecycle node launched."), so taking text up to the first newline left the
  // row showing nothing but the "+3 lines" marker.
  // Emptiness is judged on the *visible* text: a line holding only a reset sequence
  // (`ESC[0m`) is blank to a reader, and counting it would report "+1 more" for nothing.
  const lines = message
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => stripAnsi(l).trim().length)
  if (!lines.length) return ''
  const extra = lines.length - 1
  return extra ? `${lines[0]}  ⏎ +${extra} more` : lines[0]
}


/** The time column for one row.
 *
 *  Sim time where it exists. Where it does not, the **wall** offset from the run's first log
 *  line — returned with `wall` set, which the column renders dimmed so it is never read as sim
 *  time.
 *
 *  Half a real run has no sim time and legitimately so: on a measured campaign the first log line
 *  came 16 s before the first line the clock map can place, because container boot, the recorder
 *  starting and `Executing scenario` all happen before the simulator publishes /clock. Rendering
 *  all of that as `--:--` read as a broken column; the wall offset says when it happened instead.
 *  `--:--` is left for a line with no timestamp at all.
 *
 *  The figure is plain: no marker, no second face. The column is a stack of monospace figures
 *  52 px wide, and anything prefixed to half of them costs the rest their alignment for what the
 *  dimming already says. The tooltip carries the explanation for a reader who wants it. */
export function rowTime(row: LogRow, wallBase: number | null): { text: string; wall: boolean } {
  if (row.sim_time != null) return { text: fmtTime(row.sim_time), wall: false }
  if (row.wall_ts != null && wallBase != null)
    return { text: fmtTime(row.wall_ts - wallBase), wall: true }
  return { text: '--:--', wall: false }
}


/** The node column: a fixed `NODE_CHARS` wide, with the full name behind a slow hover.
 *
 *  The tooltip is attached only to a name that is actually cut short. Wrapping every cell would
 *  put a popper over `amcl` to tell the reader it is called `amcl`, and would pay MUI's hover
 *  bookkeeping on every visible row for it.
 *
 *  `disableInteractive` is what keeps a drag-selection safe: the popper is then
 *  `pointer-events: none`, so it cannot swallow the mouseup that ends a selection even if it
 *  opens right under the cursor. */
function NodeCell({ node }: { node: string }) {
  const cell = (
    <Box
      component="span"
      sx={{
        color: 'text.secondary',
        width: `${NODE_CHARS}ch`,
        flexShrink: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}
    >
      {node}
    </Box>
  )
  if (node.length <= NODE_CHARS) return cell
  return (
    <Tooltip
      title={node}
      placement="bottom-start"
      enterDelay={NODE_TIP_MS}
      // Also on the way in from another cell: crossing the column during a drag must not
      // inherit a delay already served somewhere else.
      enterNextDelay={NODE_TIP_MS}
      disableInteractive
    >
      {cell}
    </Tooltip>
  )
}


/** `m:ss.s`, matching the playback bar's readout. */
export function fmtTime(s: number | null): string {
  if (s == null || !isFinite(s)) return '--:--'
  const sign = s < 0 ? '-' : ''
  const abs = Math.abs(s)
  const m = Math.floor(abs / 60)
  return `${sign}${m}:${(abs - m * 60).toFixed(1).padStart(4, '0')}`
}

export interface RunLogViewProps {
  data?: RunLogData
  isPending?: boolean
  error?: Error
  /** Playback position in sim seconds. Omit for a log with no clock (the Explorer). */
  cursor?: number
  /** Seek the host's clock — enables click-to-seek and the next/prev-error buttons. */
  onSeek?: (simTime: number) => void
  /** Called when a row is activated in a scope that spans runs (the Explorer's config level). */
  onOpenRun?: (row: LogRow) => void
  /** Show which run each row came from — for a scope wider than one run. */
  showRun?: boolean
  filter?: LogFilter
  onFilterChange?: (filter: LogFilter) => void
  /** Stop the log at the run's scenario verdict. **Whoever owns this state shows the
   *  button**: omit it and the view owns it (on by default) and puts a toggle in the filter
   *  bar; pass it and the host owns it and the bar shows none. In the run view the header's
   *  one control governs the whole view, so a second toggle here would be a second answer to
   *  the same question. */
  hideShutdown?: boolean
  /** Extra note in the footer, e.g. the Explorer's scope. */
  note?: string
}

export function RunLogView({
  data,
  isPending,
  error,
  cursor,
  onSeek,
  onOpenRun,
  showRun,
  filter: filterProp,
  onFilterChange,
  hideShutdown: hideShutdownProp,
  note,
}: RunLogViewProps) {
  const [ownFilter, setOwnFilter] = useState<LogFilter>(EMPTY_FILTER)
  const filter = filterProp ?? ownFilter
  const setFilter = onFilterChange ?? setOwnFilter

  // Same idiom as the filter above: the prop wins where a host passes one, and the view keeps
  // its own otherwise. On by default -- the reason to open a run's log is what the run did,
  // and its teardown is a page of lifecycle and TF errors that describe none of it.
  const [ownHideShutdown, setOwnHideShutdown] = useState(true)
  const hideShutdown = hideShutdownProp ?? ownHideShutdown

  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [viewportH, setViewportH] = useState(240)
  const [scrollTop, setScrollTop] = useState(0)
  const [following, setFollowing] = useState(true)
  // Off by default: one line per row is what makes a log scannable, and it is what lets the list
  // be windowed. Wrapping is for the occasional line somebody needs to read whole.
  const [wrap, setWrap] = useState(false)
  // Set while the component scrolls itself, so its own scroll event is not mistaken for the
  // user scrolling away -- which would make the view stop following the moment it followed.
  const selfScroll = useRef(false)

  const rows = data?.rows ?? []
  const facets = useMemo(() => facetsOf(rows), [rows])
  // Over the facets, not the filtered rows: the colours are assigned from every container the log
  // holds, so filtering one out does not repaint the ones that stay.
  const containerColor = useMemo(
    () => containerColorer(facets.containers.map((f) => f.value)),
    [facets],
  )
  const compiled = useMemo(() => compileFilter(filter), [filter])

  // The shutdown cut, applied *before* the filter so the footer's "N of M lines hidden by the
  // filter" keeps meaning the filter. Not part of `compileFilter`: that returns a row-wise
  // predicate, and this needs the run's verdict -- a property of the whole load, not of a row.
  //
  // Cut on `wall_ts`, never on sim time. The clock map does not extrapolate, so the lines this
  // is here to remove -- logged after /clock stopped -- have NO sim time to compare against;
  // they are exactly the rows a sim-time cut cannot see.
  //
  // The rows arrive in wall order (see `pageSql`), so this cut is a prefix. It stays a
  // predicate rather than a slice because a row with no `wall_ts` at all
  // (`time_source: 'none'`) is kept wherever it sits: it cannot be placed, and dropping it
  // would be a guess dressed as a filter.
  const verdictWall = data?.verdict?.wallTs ?? null
  const scoped = useMemo(
    () => (hideShutdown && verdictWall != null
      ? rows.filter((r) => r.wall_ts == null || r.wall_ts <= verdictWall)
      : rows),
    [rows, hideShutdown, verdictWall],
  )
  const shown = useMemo(
    () => (compiled.passthrough ? scoped : scoped.filter(compiled.test)),
    [scoped, compiled],
  )

  // The run's earliest wall stamp, so a row the clock map cannot place still says *when*. Over
  // all loaded rows, not the filtered ones: the offsets must not shift when a filter is typed.
  const wallBase = useMemo(() => {
    let min: number | null = null
    for (const r of rows) if (r.wall_ts != null && (min == null || r.wall_ts < min)) min = r.wall_ts
    return min
  }, [rows])

  // The cursor's row, in the *filtered* index space -- which is the space the list is drawn
  // in, so filtering can never desync the divider from the rows around it.
  //
  // A row the clock map could not place carries the sim time of the last row it could, which
  // makes the array non-decreasing -- what `lastAtOrBefore`'s binary search requires. A fixed
  // -Inf for those rows would not: the rows are in wall order, so a shutdown line sits at the
  // end, and a -Inf there breaks the search for every row before it. Carrying forward also
  // places the cursor correctly -- boot lines keep -Inf (the cursor is never before them) and
  // the shutdown sits at the run's final sim time, which is when it happened.
  const shownTimes = useMemo(() => {
    let last = Number.NEGATIVE_INFINITY
    return shown.map((r) => {
      if (r.sim_time != null) last = r.sim_time
      return last
    })
  }, [shown])
  // Only a single run's rows rise all the way through: a wider scope holds run after run, each
  // restarting at zero, so the array is not sorted and one sim time names a moment in every one
  // of them. No cursor there rather than a binary search over unsorted input -- the Explorer's
  // tab already passes none, and a run view that could not resolve its run id gets the same
  // plain log instead of a divider pointing at an arbitrary row.
  const cursorIndex = useMemo(() => {
    if (cursor == null || !shownTimes.length || !data?.singleRun) return -1
    return lastAtOrBefore(shownTimes, cursor)
  }, [shownTimes, cursor, data?.singleRun])

  // A *callback* ref, not an effect. The component returns early while the log is loading (and
  // for "no table" / error), so on first render there is no scroll container at all: an effect
  // with `[]` deps ran against a null ref, bailed, and never ran again -- leaving `viewportH` at
  // its initial guess forever. The panel then rendered only that many rows however tall it was
  // made, which showed as the log stopping short with empty space under it.
  //
  // A callback ref fires whenever the node appears or is replaced, whatever the render path.
  const observer = useRef<ResizeObserver | null>(null)
  const attachScroll = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    scrollRef.current = el
    if (!el) return
    setViewportH(el.clientHeight)
    const ro = new ResizeObserver(() => setViewportH(el.clientHeight))
    ro.observe(el)
    observer.current = ro
  }, [])

  /** Is the user holding a selection inside this panel right now? */
  const hasSelection = useCallback((): boolean => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false
    const node = sel.anchorNode
    return !!node && !!scrollRef.current?.contains(node)
  }, [])

  const scrollToCursor = useCallback(() => {
    const el = scrollRef.current
    if (!el || cursorIndex < 0) return
    // Never scroll out from under a selection. Auto-scrolling is what made text impossible to
    // select while playing: every clock tick moved the container, and the drag lost its anchor.
    // Following resumes by itself as soon as the selection is dropped.
    if (hasSelection()) return
    selfScroll.current = true
    if (wrap) {
      // Wrapped rows have no computable offset, so ask the row itself where it is. It exists:
      // wrap mode renders all of them.
      const row = el.querySelector(`[data-row="${cursorIndex}"]`)
      if (row) row.scrollIntoView({ block: 'center' })
      return
    }
    // Keep the cursor row about two thirds down, so what has already happened is what fills
    // the panel and the next lines are visible as they arrive.
    el.scrollTop = Math.max(0, cursorIndex * ROW_H - el.clientHeight * 0.66)
  }, [cursorIndex, hasSelection, wrap])

  // Follow the cursor while following. The dependency is the cursor row, not the clock: a
  // clock tick that does not change which row is current changes nothing to look at. `viewportH`
  // is in there too, so growing the panel re-centres the current line instead of leaving it
  // wherever the old height had put it.
  useEffect(() => {
    if (following) scrollToCursor()
  }, [following, scrollToCursor, viewportH])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    setScrollTop(el.scrollTop)
    if (selfScroll.current) {
      selfScroll.current = false
      return
    }
    // A user-initiated scroll away from the cursor stops the follow; the jump button is then
    // how you get back, rather than having to chase the playhead by hand. Wrapped, there is no
    // predicted offset to compare with, so any deliberate scroll counts.
    if (cursorIndex < 0) return
    if (wrap) {
      setFollowing(false)
      return
    }
    const target = Math.max(0, cursorIndex * ROW_H - el.clientHeight * 0.66)
    if (Math.abs(el.scrollTop - target) > ROW_H) setFollowing(false)
  }

  const jumpToCursor = useCallback(() => {
    setFollowing(true)
    scrollToCursor()
  }, [scrollToCursor])

  // Escape does the same as the button, for a reader who scrolled with the keyboard.
  useEffect(() => {
    if (following || cursorIndex < 0) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') jumpToCursor()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [following, cursorIndex, jumpToCursor])

  const seekTo = (row: LogRow) => {
    if (row.sim_time != null && onSeek) onSeek(row.sim_time)
    if (onOpenRun) onOpenRun(row)
  }


  /** Next/previous row more severe than `other`, seeking the clock to it. */
  const stepSeverity = (dir: 1 | -1) => {
    const from = cursorIndex < 0 ? (dir === 1 ? -1 : shown.length) : cursorIndex
    const range = dir === 1
      ? shown.slice(from + 1)
      : shown.slice(0, Math.max(0, from)).reverse()
    const hit = range.find((r) => r.severity !== 'other')
    if (hit) seekTo(hit)
  }

  if (error)
    return (
      <Alert severity="warning" variant="outlined" sx={{ m: 1, py: 0 }}>
        {error.message}
      </Alert>
    )
  if (isPending)
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 1 }}>
        <CircularProgress size={16} />
        <Typography variant="caption" color="text.secondary">
          Loading log…
        </Typography>
      </Box>
    )
  if (data?.missingTable)
    return (
      <Alert severity="info" variant="outlined" sx={{ m: 1, py: 0 }}>
        No <code>run_log</code> table: this campaign was postprocessed before the merged log
        existed. Re-run postprocessing to build it.
      </Alert>
    )

  // Windowing is exact only while every row is one line high, which is what `wrap` gives up:
  // a wrapped row's height depends on its text and the panel's width, so `index * ROW_H` stops
  // describing where anything is. So wrap mode renders the filtered rows in normal flow instead,
  // and scroll-to-cursor asks the DOM (the row exists, because they all do).
  //
  // That costs one node per row, so it is refused above WRAP_MAX_ROWS rather than freezing the
  // tab -- refused visibly, with the reason on the button.
  const windowed = !wrap
  const first = windowed ? Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN) : 0
  const last = windowed
    ? Math.min(shown.length, Math.ceil((scrollTop + viewportH) / ROW_H) + OVERSCAN)
    : shown.length
  const visibleRows = shown.slice(first, last)
  // Counted against what the filter was *given*, so the two exclusions stay separable: a
  // filter matching everything it saw must not be reported as having hidden the shutdown.
  const hidden = scoped.length - shown.length
  const shutdownHidden = rows.length - scoped.length

  // What the footer used to say, now only when there is something to say: the log gets the whole
  // panel, and a line count that is simply the number of lines was noise on every single view.
  // These are not noise -- each one changes how the log should be read.
  const notes: string[] = []
  if (data?.clock && data.clock.source === 'none')
    notes.push('No clock map for this run: the lines are wall-time only, so none has a sim time.')
  // Never silent: a log that stops early has to say that it was cut and where.
  if (shutdownHidden > 0)
    notes.push(
      `${shutdownHidden} shutdown lines hidden — what the run said after the scenario `
      + `${data?.verdict?.status ?? 'ended'}.`,
    )
  if (hidden > 0) notes.push(`${hidden} of ${scoped.length} lines hidden by the filter.`)
  if (data?.truncated)
    notes.push(`Load capped at ${rows.length} lines; earlier lines were not fetched.`)
  if (note) notes.push(note)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <LogFilterBar
        filter={filter}
        onChange={setFilter}
        facets={facets}
        invalidRegex={compiled.invalidRegex}
        onStepSeverity={cursor != null ? stepSeverity : undefined}
        hideShutdown={hideShutdown}
        // Whoever owns the state shows the button; a host that passes `hideShutdown` owns it
        // and gets no handler, so the bar renders none.
        onHideShutdownChange={hideShutdownProp == null ? setOwnHideShutdown : undefined}
        shutdownDisabledReason={
          verdictWall == null
            ? 'This run recorded no scenario verdict, so there is no shutdown to hide.'
            : ''
        }
        wrap={wrap}
        onWrapChange={setWrap}
        wrapDisabledReason={
          shown.length > WRAP_MAX_ROWS
            ? `Word wrap is unavailable for ${shown.length} lines (limit ${WRAP_MAX_ROWS}) — `
              + 'wrapping has to render every line at once. Filter the log down first.'
            : ''
        }
        notes={notes}
      />

      {/* minWidth/minHeight 0 are load-bearing: without them a flex child refuses to shrink
          below its content and the list scrolls the whole panel instead of itself. */}
      <Box sx={{ position: 'relative', flexGrow: 1, minHeight: 0, minWidth: 0 }}>
        <Box
          ref={attachScroll}
          onScroll={onScroll}
          sx={{
            position: 'absolute',
            inset: 0,
            overflowY: 'auto',
            // A log line is routinely wider than the panel. Unwrapped it scrolls sideways --
            // truncating it with an ellipsis hid exactly the end of the message, which is where
            // the coordinates and the reason usually are.
            overflowX: wrap ? 'hidden' : 'auto',
            // Emoji/symbol fallbacks after the monospace stack: logs carry ✓ ✗ ⏎ ▶ and the odd
            // emoji, and a bare `monospace` renders whatever the system font lacks as tofu.
            fontFamily:
              'ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace, ' +
              '"Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", "Symbola"',
            fontSize: 11.5,
            lineHeight: `${ROW_H}px`,
          }}
        >
          {!shown.length ? (
            <Typography variant="caption" color="text.secondary" sx={{ p: 1, display: 'block' }}>
              {rows.length ? 'No lines match the filter.' : 'This run logged nothing.'}
            </Typography>
          ) : (
            <Box
              sx={
                windowed
                  ? { height: shown.length * ROW_H, position: 'relative' }
                  : { position: 'relative' }
              }
            >
              <Box
                sx={
                  windowed
                    ? { position: 'absolute', top: first * ROW_H, left: 0, minWidth: '100%',
                        width: 'max-content' }
                    : {}
                }
              >
                {visibleRows.map((row, i) => {
                  const index = first + i
                  const future = cursorIndex >= 0 && index > cursorIndex
                  const hl = highlightOf(row, filter)
                  return (
                    <Box
                      key={index}
                      data-row={index}
                      // Double click, not single: a click is also how a text selection ends, so
                      // seeking on it made the log unselectable -- every drag jumped playback.
                      onDoubleClick={() => seekTo(row)}
                      sx={{
                        // Fixed height only unwrapped; a wrapped row is as tall as its text.
                        height: wrap ? 'auto' : ROW_H,
                        minHeight: ROW_H,
                        // Unwrapped a row is exactly one line, so anything taller must be
                        // clipped: a folded multi-line event (a traceback, a multi-line nav2
                        // message) otherwise painted its extra lines straight over the rows
                        // below, which is the text that appeared to linger after a toggle.
                        overflow: wrap ? 'visible' : 'hidden',
                        display: 'flex',
                        // Wrapped, the row is several lines tall; the time/container/node columns
                        // belong beside its FIRST line, not floating in the middle of it.
                        alignItems: 'flex-start',
                        gap: 1,
                        px: 1,
                        // `max-content` is what makes the row wider than the panel instead of
                        // squeezing its message, so the container has something to scroll.
                        width: wrap ? 'auto' : 'max-content',
                        minWidth: '100%',
                        whiteSpace: wrap ? 'pre-wrap' : 'pre',
                        // Explicit, because the rows are also a click target: a log is text
                        // first, and copying a line out of it is the commonest thing to want.
                        userSelect: 'text',
                        cursor: 'text',
                        // Not-yet-logged lines stay readable but plainly behind the cursor;
                        // the divider says exactly where "now" is.
                        opacity: future ? 0.38 : 1,
                        borderTop:
                          index === cursorIndex + 1 && cursorIndex >= 0
                            ? '1px solid'
                            : undefined,
                        borderColor: 'primary.main',
                        bgcolor: hl ? `${SEVERITY_COLOR[hl]}18` : undefined,
                        '&:hover': { bgcolor: 'action.hover' },
                      }}
                    >
                      {(() => {
                        const t = rowTime(row, wallBase)
                        return (
                          <Box
                            component="span"
                            // The only wording there is, so it says what the figure is and why
                            // it is not a sim time. Deliberately not "the clock had not started
                            // yet": the clock map does not extrapolate at *either* end, so the
                            // whole shutdown phase lands here for the opposite reason.
                            title={
                              t.wall
                                ? 'wall time since the first log line — the simulator\'s clock '
                                  + 'was not running when this line was logged, so it has no '
                                  + 'sim time'
                                : undefined
                            }
                            sx={{
                              // Colour alone marks a wall time, so the figures stay one face
                              // and one width down the column.
                              color: t.wall ? 'text.disabled' : 'text.secondary',
                              width: 52,
                              flexShrink: 0,
                            }}
                          >
                            {t.text}
                          </Box>
                        )
                      })()}
                      {showRun ? (
                        <Box
                          component="span"
                          sx={{ color: 'text.secondary', width: 130, flexShrink: 0 }}
                        >
                          {row.config_name}/{row.run_id}
                        </Box>
                      ) : null}
                      <Box
                        component="span"
                        sx={{ color: containerColor(row.container), width: 74, flexShrink: 0 }}
                      >
                        {row.container || '?'}
                      </Box>
                      {/* Clamped, not sized to the longest name there might be: a node is
                          `scenario_execution_server.action_runner` often enough that fitting one
                          would spend a quarter of the row on a column the message has to be read
                          past.
                          A deliberate cost, not an oversight: a namespaced name carries its
                          discriminator at the *end*, so the four `scenario_execution_*` nodes and
                          the two `lifecycle_manager_*` ones all render alike (6 of 34 names on a
                          measured run). Resting on one names it -- the column is for scanning,
                          and the width is the point. */}
                      <NodeCell node={row.node} />
                      <Box
                        component="span"
                        sx={{
                          flexGrow: 1,
                          color: hl ? SEVERITY_COLOR[hl] : undefined,
                          // `minWidth: 0` is what lets wrapping happen at all: a flex child
                          // defaults to refusing to shrink below its content, so `pre-wrap` never
                          // got a narrower box to wrap into and widened the row instead -- which
                          // read as the message being mis-indented rather than un-wrapped.
                          // Unwrapped the opposite is wanted: the cell extends and the container
                          // scrolls sideways.
                          minWidth: wrap ? 0 : 'auto',
                          flexBasis: wrap ? 0 : 'auto',
                          wordBreak: wrap ? 'break-word' : 'normal',
                        }}
                      >
                        {parseAnsi(wrap ? row.message : flatten(row.message)).map(
                          (span, si) => (
                            // The producer's own colour wins where it set one; the severity
                            // colour styles the rest, so a highlighted line stays recognisable
                            // without overpainting what gz or nav2 chose to emphasise.
                            <Box component="span" key={si} sx={span.style}>
                              {span.text}
                            </Box>
                          ),
                        )}
                      </Box>
                    </Box>
                  )
                })}
              </Box>
            </Box>
          )}
        </Box>

        {/* Appears only once following has stopped, and points the way back. */}
        {!following && cursorIndex >= 0 ? (
          <Tooltip
            title={`Back to the current line (${fmtTime(
              shown[cursorIndex]?.sim_time ?? cursor ?? null,
            )}) and resume following — Escape does the same`}
          >
            <Fab
              size="small"
              color="primary"
              aria-label="back to the current line"
              onClick={jumpToCursor}
              sx={{ position: 'absolute', right: 12, bottom: 10, zIndex: 4 }}
            >
              {scrollTop > cursorIndex * ROW_H ? (
                <ArrowUpwardRoundedIcon fontSize="small" />
              ) : (
                <ArrowDownwardRoundedIcon fontSize="small" />
              )}
            </Fab>
          </Tooltip>
        ) : null}
      </Box>
    </Box>
  )
}
