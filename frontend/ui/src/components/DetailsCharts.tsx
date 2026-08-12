import { useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import { useTheme } from '@mui/material/styles'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { containerColorer } from './containerColor'
import { MeterBar } from './MeterBar'
import { formatCpu } from '@/lib/campaignDetails'
import {
  formatAxisDuration,
  linePercents,
  linePoints,
  niceMax,
  objectiveDomain,
  pct,
} from '@/lib/detailsGeometry'
import type {
  ActionPoint,
  ResourceStats,
  BatchSummary,
  ContainerCpu,
  DurationBin,
} from '@/lib/campaignDetails'
import { formatBytes } from '@/lib/format'

// The Details panel's charts, as DOM.
//
// No charting library, on purpose. These five are fixed, hand-designed for a ~190x110px column, and
// three of them are stacked bars -- which is `MeterBar`, already in this app. A spec compiler bought
// none of what it costs here: its bundle (the only reason this module was lazy-loaded), hex colours
// that ignore the MUI theme, its own tooltip idiom beside MUI's everywhere else, and a defect class
// where a valid spec silently draws the wrong thing (axis labels came out black on the dark card
// because one `config.axis` override replaced the theme's whole block).
//
// So: `Box` with percentage widths for anything rectangular, one small inline `<svg><polyline>` for
// the two series that are genuinely curves, MUI `Tooltip` for every hover, and theme tokens
// (`success.main`, `error.main`, `warning.main`) so light and dark both follow. The arithmetic lives
// in `lib/detailsGeometry.ts` and is unit-tested; this file is only the markup.
//
// Vega is still right for the Results explorer's chart builder and user-declared `evaluation.plots`
// panels, where the spec is AUTHOR-supplied. That is what a spec compiler is for. This is not.

export type DetailsChartKind =
  | 'cpu'
  | 'memory'
  | 'histogram'
  | 'objective'
  | 'actions'

/** Where a chart would mislead, say why in its place — same footprint, no axes around nothing. */
function Note({ height, children }: { height: number; children: React.ReactNode }) {
  return (
    <Box sx={{ height, display: 'flex', alignItems: 'center' }}>
      <Typography variant="caption" color="text.secondary">
        {children}
      </Typography>
    </Box>
  )
}

/** The two ends of a shared axis, written out. The Vega version's labels were the thing nobody
 *  could read; these are ordinary text in a theme colour. */
function AxisEnds({ left, right }: { left: string; right: string }) {
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

/** A vertical marker inside a bar track, at `value` of `max`. */
function Marker({
  value,
  max,
  color,
  opacity = 1,
}: {
  value: number
  max: number
  color: string
  opacity?: number
}) {
  return (
    <Box
      sx={{
        position: 'absolute',
        top: -1,
        bottom: -1,
        left: `${pct(value, max)}%`,
        width: 2,
        ml: '-1px',
        bgcolor: color,
        opacity,
        borderRadius: 0.5,
      }}
    />
  )
}

const BAR_HEIGHT = 10

/** The fixed columns either side of a resource bar's track. The axis line reuses them as spacers,
 *  so its two ends sit under the ends of the TRACK rather than under the column -- otherwise "0"
 *  labels the container name and the maximum labels the suggested-value gutter, and neither number
 *  points at the thing it scales. */
const LABEL_WIDTH = 62
const VALUE_WIDTH = 46

/** How the containers combine into one pod request, as a ring.
 *
 *  A part-to-whole with three or four parts and a meaningful total is the one job a pie does better
 *  than a bar, and it is the question the bars beside it cannot answer: those compare each container
 *  against its own reservation, while this says which of them the POD's size is actually made of —
 *  the figure that divides into the cluster quota. The total sits in the hole, so the ring is read
 *  as "3.25 cores, and here is what they are".
 *
 *  It sweeps in on mount rather than appearing complete. `CollapsibleBox` unmounts its children, so
 *  mounting IS opening the panel: the first paint uses a zero-length dash and a frame later the real
 *  one, which the CSS transition then animates between. */
function PodRing({
  slices,
  total,
  caption,
  size = 46,
}: {
  slices: { label: string; value: number; color: string; note: string }[]
  total: string
  /** What the ring is a whole OF, for the legend's heading. */
  caption: string
  size?: number
}) {
  // `sx` resolves palette paths for `color`/`bgcolor`, but NOT for the SVG presentation
  // properties: `stroke: 'action.hover'` reaches the DOM verbatim and is simply invalid, so the
  // element draws nothing and nothing warns. Concrete values from the theme instead -- this is why
  // the objective chart shipped as dots with no line between them.
  const theme = useTheme()
  const [swept, setSwept] = useState(false)
  useEffect(() => {
    // A frame, not a timeout: the browser has to paint the zero-length state once for the
    // transition to have something to animate from.
    const frame = requestAnimationFrame(() => setSwept(true))
    return () => cancelAnimationFrame(frame)
  }, [])

  const sum = slices.reduce((n, s) => n + s.value, 0)
  const radius = 15.9155 // circumference 100, so a dash length IS a percentage
  const stroke = 5
  let offset = 0
  // One hover on the whole ring, carrying the legend. A slice at this size is a few pixels of arc,
  // so per-slice tooltips would be a game of aim -- and the thing a reader needs from a ring is
  // which colour is which, which is a property of the whole.
  const legend = (
    <Box sx={{ fontSize: 11 }}>
      <Box sx={{ mb: 0.25, opacity: 0.8 }}>{caption}</Box>
      {slices.map((slice) => (
        <Stack key={slice.label} direction="row" spacing={0.75} alignItems="center">
          <Box
            sx={{ width: 8, height: 8, borderRadius: '2px', bgcolor: slice.color, flexShrink: 0 }}
          />
          <Box>{slice.note}</Box>
          <Box flexGrow={1} />
          <Box sx={{ opacity: 0.7 }}>
            {sum > 0 ? `${Math.round((slice.value / sum) * 100)}%` : '—'}
          </Box>
        </Stack>
      ))}
    </Box>
  )
  return (
    <Tooltip title={legend} placement="top">
      <Box sx={{ position: 'relative', width: size, height: size, flexShrink: 0, cursor: 'help' }}>
        <Box
          component="svg"
          viewBox="0 0 40 40"
          sx={{ width: size, height: size, display: 'block' }}
        >
          <Box
            component="circle"
            cx="20"
            cy="20"
            r={radius}
            sx={{ fill: 'none', stroke: theme.palette.action.hover, strokeWidth: stroke }}
          />
          {slices.map((slice) => {
            const share = sum > 0 ? (slice.value / sum) * 100 : 0
            const start = offset
            offset += share
            return (
              <Box
                key={slice.label}
                component="circle"
                cx="20"
                cy="20"
                r={radius}
                // -90deg so the first slice starts at twelve o'clock, where a reader expects it.
                transform="rotate(-90 20 20)"
                sx={{
                  fill: 'none',
                  stroke: slice.color,
                  strokeWidth: stroke,
                  strokeDasharray: swept ? `${share} ${100 - share}` : '0 100',
                  strokeDashoffset: -start,
                  transition: 'stroke-dasharray 600ms cubic-bezier(0.4, 0, 0.2, 1)',
                }}
              />
            )
          })}
        </Box>
        <Typography
          variant="caption"
          sx={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 10,
            fontWeight: 600,
            lineHeight: 1,
          }}
        >
          {total}
        </Typography>
      </Box>
    </Tooltip>
  )
}

/** Per-container CPU: what was reserved, where the container actually sat, and the peak.
 *
 *  The track is a shared scale across containers so their bars are comparable. The reserved width
 *  is a filled region rather than a bar of its own, so an over-reservation reads as the empty space
 *  the box does not fill — which is the whole point of the column. */
/** One resource's view of the containers: the mean-usage ring, then a distribution bar each.
 *
 *  cpu and memory get the same treatment because a reader does the same thing with both — compare
 *  what was used against what was reserved, and copy a number into the `.vast`. What differs is the
 *  unit and how `suggested` was reached (sustained use for cpu, the peak for memory, since one
 *  over-run throttles and the other kills), and those are parameters rather than a second design. */
function ResourceBars({
  rows,
  height,
  resource,
}: {
  rows: ContainerCpu[]
  height: number
  resource: 'cpu' | 'memory'
}) {
  const cpu = resource === 'cpu'
  const statsOf = (row: ContainerCpu): ResourceStats =>
    cpu
      ? {
          p05: row.p05, p25: row.p25, p50: row.p50, p75: row.p75, p95: row.p95, peak: row.peak,
          mean: row.meanCores, declared: row.declared, suggested: row.suggested,
        }
      : row.mem
  // Two decimals of a core is meaningful; two decimals of a byte count is not, so the measured
  // figures go through the reading formatter and the actionable ones through the config formatter.
  const measured = (value: number) => (cpu ? value.toFixed(2) : formatBytes(value))
  const unit = cpu ? 'cores' : ''

  const stats = rows.map(statsOf)
  const max = niceMax(stats.flatMap((s) => [s.peak, s.declared ?? 0, s.suggested ?? 0]))
  const colorOf = containerColorer(rows.map((r) => r.container))
  // The ring splits MEAN USAGE -- what was actually consumed, not what was reserved or what is
  // being recommended. That makes it the one part of the column reporting fact rather than advice:
  // "the pod averaged 2.3 cores and here is whose they were".
  const slices = rows.map((row, i) => ({
    label: row.container,
    value: stats[i].mean,
    color: colorOf(row.container),
    note: `${row.container} ${measured(stats[i].mean)}`,
  }))
  const meanTotal = slices.reduce((n, s) => n + s.value, 0)
  return (
    <Stack direction="row" spacing={1} alignItems="flex-start" sx={{ minHeight: height }}>
      <PodRing
        slices={slices}
        total={cpu ? formatCpu(Math.round(meanTotal * 100) / 100) : formatBytes(meanTotal)}
        caption={`mean ${cpu ? 'cores' : 'RSS'} used, per container`}
      />
      <Box sx={{ flexGrow: 1, minWidth: 0 }}>
      {/* No per-row hover. The column header's hover already carries every container's numbers in
          one table, so a tooltip per bar meant the same figures reachable two ways -- and the row
          version won by accident, because a bar is what the pointer lands on first. One summary
          beats three partial ones. */}
      {rows.map((row, i) => (
        <Box key={row.container}>
          <Stack direction="row" spacing={0.75} alignItems="center" sx={{ mb: 0.5 }}>
            <Typography
              variant="caption"
              noWrap
              sx={{ width: LABEL_WIDTH, flexShrink: 0, fontSize: 10, color: 'text.secondary' }}
            >
              {row.container}
            </Typography>
            <Box
              sx={{
                position: 'relative',
                flexGrow: 1,
                minWidth: 0,
                height: BAR_HEIGHT,
                borderRadius: 0.75,
                bgcolor: 'action.hover',
              }}
            >
              {/* What the .vast reserved. Absent when it declared nothing, and then the bar is
                  read against the axis alone. */}
              {stats[i].declared !== null ? (
                <Box
                  sx={{
                    position: 'absolute',
                    inset: 0,
                    width: `${pct(stats[i].declared as number, max)}%`,
                    bgcolor: 'divider',
                    borderRadius: 0.75,
                  }}
                />
              ) : null}
              {/* p05..p95 -- the whiskers, as a thin span behind the box. */}
              <Box
                sx={{
                  position: 'absolute',
                  top: '50%',
                  mt: '-1px',
                  height: 2,
                  left: `${pct(stats[i].p05, max)}%`,
                  width: `${pct(stats[i].p95 - stats[i].p05, max)}%`,
                  bgcolor: 'success.main',
                  opacity: 0.5,
                }}
              />
              {/* p25..p75 -- where it sat, half its ticks inside. */}
              <Box
                sx={{
                  position: 'absolute',
                  top: 0,
                  bottom: 0,
                  left: `${pct(stats[i].p25, max)}%`,
                  width: `${Math.max(pct(stats[i].p75 - stats[i].p25, max), 1.5)}%`,
                  bgcolor: 'success.main',
                  opacity: 0.85,
                  borderRadius: 0.5,
                }}
              />
              <Marker value={stats[i].p50} max={max} color="background.paper" />
              <Marker value={stats[i].peak} max={max} color="warning.main" opacity={0.9} />
              {stats[i].suggested !== null ? (
                <Marker
                  value={stats[i].suggested as number}
                  max={max}
                  color="text.primary"
                  opacity={0.65}
                />
              ) : null}
            </Box>
            {/* What it MEASURED, not what is suggested. The row is a picture of the
                distribution, so the number closing it has to belong to that picture -- the median,
                which is the tick inside the box. The suggestion is advice rather than data, and
                advice reads wrong in a column of measurements; it lives in the header's hover,
                beside the p95 and peak it is derived from. */}
            <Typography
              variant="caption"
              sx={{
                width: VALUE_WIDTH,
                flexShrink: 0,
                fontSize: 10,
                textAlign: 'right',
                fontWeight: 600,
              }}
            >
              {measured(stats[i].p50)}
            </Typography>
          </Stack>
        </Box>
      ))}
        {/* Indented to the track: same spacers as a bar row, so the ends line up with the bar. */}
        <Stack direction="row" spacing={0.75}>
          <Box sx={{ width: LABEL_WIDTH, flexShrink: 0 }} />
          <Box sx={{ flexGrow: 1, minWidth: 0 }}>
            <AxisEnds left="0" right={cpu ? `${max} ${unit}` : formatBytes(max)} />
          </Box>
          <Box sx={{ width: VALUE_WIDTH, flexShrink: 0 }} />
        </Stack>
      </Box>
    </Stack>
  )
}

/** Where each run's trial ended: a mini meter per action, split by verdict, most runs first. */
function ActionList({ rows, height }: { rows: ActionPoint[]; height: number }) {
  const byAction = new Map<string, ActionPoint[]>()
  for (const row of rows) {
    const list = byAction.get(row.action)
    if (list) list.push(row)
    else byAction.set(row.action, [row])
  }
  // One shared denominator, so the bars compare actions rather than each filling its own row.
  const max = Math.max(
    1,
    ...[...byAction.values()].map((points) => points.reduce((n, p) => n + p.runs, 0)),
  )
  const COLOR: Record<string, string> = {
    passed: 'success.main',
    failed: 'error.main',
    other: 'text.disabled',
  }
  return (
    <Stack spacing={0.5} sx={{ minHeight: height }}>
      {[...byAction.entries()].map(([action, points]) => {
        const runs = points.reduce((n, p) => n + p.runs, 0)
        const parts = points.map((p) => `${p.runs} ${p.verdict}`).join(', ')
        return (
          <Tooltip key={action} placement="top" title={`${action} — ${parts}`}>
            <Stack direction="row" spacing={0.75} alignItems="center" sx={{ cursor: 'help' }}>
              <Box sx={{ width: 54, flexShrink: 0 }}>
                <MeterBar
                  height={8}
                  segments={points.map((p) => ({
                    // Of the shared maximum, so a row with half the runs is half as long.
                    fraction: p.runs / max,
                    color: COLOR[p.verdict] ?? 'text.disabled',
                    opacity: 0.9,
                  }))}
                />
              </Box>
              <Typography variant="caption" noWrap sx={{ fontSize: 10, minWidth: 0 }}>
                {action}
              </Typography>
              <Box flexGrow={1} />
              <Typography variant="caption" sx={{ fontSize: 10, color: 'text.secondary' }}>
                {runs}
              </Typography>
            </Stack>
          </Tooltip>
        )
      })}
    </Stack>
  )
}

/** The distribution of run durations, binned, each bar split by verdict.
 *
 *  The panel's other duration view is the heat strip, which keeps every run in EXECUTION ORDER --
 *  good for spotting the one straggler, useless for shape. This is the shape: whether the campaign
 *  has one mode or two, and whether a second mode is made of failures (a timeout) or of passes (a
 *  slower path through the scenario). Same data, two questions, and neither answers the other.
 *
 *  Binned over the data's range rather than from zero -- see `durationHistogram`. Both ends are
 *  labelled, so the range is stated rather than assumed. */
function DurationHistogram({ rows, height }: { rows: DurationBin[]; height: number }) {
  const plot = height - 14
  const max = Math.max(1, ...rows.map((b) => b.runs))
  // The whole range decides the unit, so every label on this chart -- both axis ends and every
  // bin's tooltip -- is in one unit and reads as a scale rather than as unrelated numbers.
  const span = rows.length ? rows[rows.length - 1].to - rows[0].from : 0
  const label = (value: number) => formatAxisDuration(value, span)
  return (
    <Box>
      <Stack direction="row" spacing="1px" alignItems="flex-end" sx={{ height: plot }}>
        {rows.map((bin) => (
          <Tooltip
            key={bin.from}
            placement="top"
            title={
              `${label(bin.from)}–${label(bin.to)} — ` +
              `${bin.runs} run${bin.runs === 1 ? '' : 's'}` +
              (bin.failed ? `, ${bin.failed} failed` : '') +
              (bin.other ? `, ${bin.other} without a verdict` : '')
            }
          >
            {/* One bar per bin, one colour. The verdict split that used to be stacked in here is
                gone: at 24 bins a campaign needs 100+ runs before a bin holds enough of both to
                stack, and below that every bar came out solid -- so the colour was reporting a
                single run's verdict in a chart whose whole job is to aggregate them. The counts
                are still in the hover, where they cost no width, and `Ended in` is where a
                failure gets attributed. */}
            <Stack
              justifyContent="flex-end"
              sx={{
                flexGrow: 1,
                flexBasis: 0,
                minWidth: 2,
                height: '100%',
                cursor: bin.runs ? 'help' : 'default',
              }}
            >
              {bin.runs ? (
                <Box
                  sx={{
                    height: `${pct(bin.runs, max)}%`,
                    bgcolor: 'primary.main',
                    opacity: 0.85,
                    borderRadius: '2px 2px 0 0',
                    '&:hover': { opacity: 1 },
                  }}
                />
              ) : null}
            </Stack>
          </Tooltip>
        ))}
      </Stack>
      <AxisEnds
        left={rows.length ? label(rows[0].from) : ''}
        right={rows.length ? label(rows[rows.length - 1].to) : ''}
      />
    </Box>
  )
}

/** A search's best objective per round: a line through one point per batch.
 *
 *  Both axes are labelled, which the other columns can get away with skipping and this one cannot.
 *  Elsewhere the quantity is named by the column header and the unit is obvious (cores, seconds); an
 *  objective is whatever the campaign declared it to be, its scale is arbitrary, and it is usually a
 *  narrow band far from zero. Without the y range on the plot, a rising line says only "it went up
 *  by some amount" -- and a search that improved by 0.4% looks identical to one that doubled.
 *
 *  The y axis is NOT zero-based, for that same reason: forcing the origin in flattens the curve the
 *  chart exists to show. The labels are what make that honest. */
function ObjectiveLine({ rows, height }: { rows: BatchSummary[]; height: number }) {
  const theme = useTheme()
  const data = rows
    .filter((r) => r.batch !== null && r.bestObjective !== null)
    .map((r) => ({ x: r.batch as number, y: r.bestObjective as number }))
  if (data.length < 2) return <Note height={height}>one round</Note>
  const plot = height - 14
  const W = 100
  const ys = data.map((d) => d.y)
  // A rate-shaped objective is read against the whole unit interval; anything else keeps its own
  // range. `objectiveDomain` owns that choice and says why.
  const domain = objectiveDomain(ys)
  const [low, high] = domain ?? [Math.min(...ys), Math.max(...ys)]
  const tick = { fontSize: 9, lineHeight: 1, color: 'text.secondary' as const }
  return (
    <Box>
      <Stack direction="row" spacing={0.5} alignItems="stretch">
        {/* The y ticks, outside the plot so the line still gets the full width. High at the top,
            low at the bottom, matching where each value actually sits. */}
        <Stack justifyContent="space-between" sx={{ height: plot, flexShrink: 0 }}>
          <Typography variant="caption" sx={tick} noWrap>
            {domain ? high : high.toPrecision(3)}
          </Typography>
          <Typography variant="caption" sx={tick} noWrap>
            {domain ? low : low.toPrecision(3)}
          </Typography>
        </Stack>
        <Box sx={{ position: 'relative', height: plot, flexGrow: 1, minWidth: 0 }}>
          <Box
            component="svg"
            viewBox={`0 0 ${W} ${plot}`}
            preserveAspectRatio="none"
            sx={{ width: '100%', height: plot, display: 'block', overflow: 'visible' }}
          >
            <Box
              component="polyline"
              points={linePoints(data, W, plot, domain)}
              sx={{
                fill: 'none',
                // A concrete colour: `sx` does not resolve palette paths for `stroke`, so
                // 'warning.main' reached the DOM verbatim and the line was never drawn.
                stroke: theme.palette.warning.main,
                strokeWidth: 1.5,
                vectorEffect: 'non-scaling-stroke',
              }}
            />
          </Box>
          {/* The dots are DOM, not SVG, so each carries its own MUI hover and stays round under
              the non-uniform scaling the viewBox applies to the line. */}
          {linePercents(data, domain).map((p, i) => (
            <Tooltip
              key={data[i].x}
              placement="top"
              title={`batch ${data[i].x} — best ${data[i].y.toPrecision(4)}`}
            >
              <Box
                sx={{
                  position: 'absolute',
                  left: `${p.x}%`,
                  top: `${100 - p.y}%`,
                  width: 5,
                  height: 5,
                  ml: '-2.5px',
                  mt: '-2.5px',
                  borderRadius: '50%',
                  bgcolor: 'warning.main',
                  cursor: 'help',
                }}
              />
            </Tooltip>
          ))}
        </Box>
      </Stack>
      <AxisEnds left={`batch ${data[0].x}`} right={`batch ${data[data.length - 1].x}`} />
    </Box>
  )
}

export function DetailsCharts({
  kind,
  rows,
  height,
}: {
  kind: DetailsChartKind
  rows: ContainerCpu[] | DurationBin[] | BatchSummary[] | ActionPoint[]
  height: number
}) {
  // An empty chart is worse than no chart: an axis pair around nothing reads as "measured, and it
  // was zero" rather than "nothing to measure".
  if (!rows.length) return <Note height={height}>no data</Note>
  switch (kind) {
    case 'cpu':
      return <ResourceBars rows={rows as ContainerCpu[]} height={height} resource="cpu" />
    case 'memory':
      return <ResourceBars rows={rows as ContainerCpu[]} height={height} resource="memory" />
    case 'actions':
      return <ActionList rows={rows as ActionPoint[]} height={height} />
    case 'histogram':
      return <DurationHistogram rows={rows as DurationBin[]} height={height} />
    case 'objective':
      return <ObjectiveLine rows={rows as BatchSummary[]} height={height} />
  }
}
