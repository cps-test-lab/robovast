import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { useTheme } from '@mui/material/styles'
import { AxisEnds, Note } from './chartPrimitives'
import { bandDomain, bandPoints, linePercents, linePoints } from '@/lib/detailsGeometry'
import type { BatchObjective, SearchHistory, StageObjective } from '@/lib/robovastClient'

// A search's objective over its batches: the spread each round measured, and the best found so far
// laid over it.
//
// One chart, two callers. The campaign card renders it live from `/search/history` while a search
// runs; the Details panel renders it from the same route afterwards. One component reading one
// route, so the two views cannot drift apart or disagree.
//
// Why the band and not just the line: a flat best-so-far has two very different causes, and the
// decision the reader is making (let it run, or stop it) depends on which. If the spread is still
// wide the search is exploring and may yet find something; if the spread has collapsed onto the
// best value, the strategy is re-sampling one region and further batches will buy nothing. The
// best-so-far line alone cannot tell those apart.
//
// A search in stages draws one segment per stage, each on its own scale and under its own
// objective's name: a stage may minimise a margin and the next a time, and one curve across them
// would put two measures on one axis. `best_so_far` starts again with each stage for the same
// reason.
//
// No charting library, deliberately — see the header of DetailsCharts.tsx for the full reasoning.

/** Only batches that actually measured something can be drawn. A batch where every draw failed to
 *  compose, or produced nothing measurable, has null statistics: it is a gap in the record, and
 *  plotting it at zero would invent a result that never happened. */
function drawable(batches: BatchObjective[]) {
  return batches.filter(
    (b): b is BatchObjective & { min: number; max: number; mean: number; best_so_far: number } =>
      b.n_scored > 0 &&
      typeof b.min === 'number' &&
      typeof b.max === 'number' &&
      typeof b.mean === 'number' &&
      typeof b.best_so_far === 'number',
  )
}

/** Why there is no chart, in the reader's terms. `unavailable` is the service's own word for it —
 *  see interface.py:SearchHistory — and each case is a normal answer rather than a failure, so each
 *  gets a sentence instead of an empty plot. */
function reason(unavailable: string | null | undefined, drawn: number): string | null {
  if (unavailable === 'multi_objective') {
    return 'several objectives — no single value to trend'
  }
  if (unavailable === 'batch_mode') return 'not a search'
  if (unavailable === 'no_store') return 'nothing recorded yet'
  if (drawn === 0) return 'no objective measured yet'
  if (drawn === 1) return 'one round so far'
  return null
}

/** What the stage that ended the campaign says under its rounds. */
export function endedHere(stage: StageObjective): string {
  return `ended the search here: ${stage.stop_reason ?? 'its condition found no cell'}`
}

/** One segment of the chart: a stage's rounds, under the stage's own objective. */
export interface ChartSegment {
  /** `2/4 human` for a search of several stages; null for the one stage of a search without. */
  label: string | null
  stage: StageObjective | undefined
  batches: BatchObjective[]
  unavailable: string | null | undefined
}

/** The segments *history* is drawn as: one per stage, in the file's order, or a single unlabelled
 *  one for a search in one stage (and for a history with nothing to show). */
export function chartSegments(history: SearchHistory): ChartSegment[] {
  const stages = history.stages ?? []
  if (history.unavailable || stages.length <= 1) {
    return [{
      label: null,
      stage: stages[0],
      batches: history.batches,
      unavailable: history.unavailable ?? stages[0]?.unavailable,
    }]
  }
  return stages.map((stage) => ({
    label: `${stage.idx + 1}/${stages.length} ${stage.name}`,
    stage,
    batches: history.batches.filter((b) => b.stage === stage.name),
    unavailable: stage.unavailable,
  }))
}

export function BatchObjectiveChart({
  history,
  height = 110,
}: {
  history: SearchHistory
  height?: number
}) {
  const segments = chartSegments(history)
  if (segments.length === 1 && segments[0].label === null) {
    const [only] = segments
    return (
      <StageChart batches={only.batches} stage={only.stage} unavailable={only.unavailable}
        height={height} />
    )
  }
  // The total height is shared so the panel does not grow with the number of stages.
  const each = Math.max(56, Math.floor(height / segments.length))
  return (
    <Stack spacing={0.75}>
      {segments.map((seg) => (
        <Box key={seg.label}>
          <Typography
            variant="caption"
            color="text.secondary"
            noWrap
            sx={{ fontSize: 9, display: 'block', fontWeight: 600 }}
          >
            {seg.label}
          </Typography>
          <StageChart batches={seg.batches} stage={seg.stage} unavailable={seg.unavailable}
            height={each} />
        </Box>
      ))}
    </Stack>
  )
}

function StageChart({
  batches,
  stage,
  unavailable,
  height,
}: {
  batches: BatchObjective[]
  stage: StageObjective | undefined
  unavailable: string | null | undefined
  height: number
}) {
  const theme = useTheme()
  const rows = drawable(batches)
  // A stage the campaign ended before is said to be one, not drawn as an empty plot, and the
  // stage that ended it says why under its rounds.
  if (stage?.outcome === 'not_run') return <Note height={height}>not run</Note>
  const why = reason(unavailable, rows.length)
  if (why) {
    return <Note height={height}>{stage?.outcome === 'condition' ? endedHere(stage) : why}</Note>
  }

  const plot = height - 14
  const W = 100
  // One scale for everything drawn, so the best-so-far line cannot escape the band beneath it.
  const domain = bandDomain(rows.flatMap((r) => [r.min, r.max, r.best_so_far]))
  const [lo, hi] = domain
  const bands = rows.map((r) => ({ x: r.idx, lo: r.min, hi: r.max }))
  const best = rows.map((r) => ({ x: r.idx, y: r.best_so_far }))
  const mean = rows.map((r) => ({ x: r.idx, y: r.mean }))
  const tick = { fontSize: 9, lineHeight: 1, color: 'text.secondary' as const }
  const fmt = (v: number) => v.toPrecision(3)
  const goal = stage?.direction === 'minimize' ? 'lower is better' : 'higher is better'

  return (
    <Box>
      <Stack direction="row" spacing={0.5} alignItems="stretch">
        {/* The y ticks live outside the plot so the series keep the full width. An objective's
            scale is arbitrary, so without them a rising line says only "it went up by some
            amount" — a search that improved by 0.4% would look identical to one that doubled. */}
        <Stack justifyContent="space-between" sx={{ height: plot, flexShrink: 0 }}>
          <Typography variant="caption" sx={tick} noWrap>
            {fmt(hi)}
          </Typography>
          <Typography variant="caption" sx={tick} noWrap>
            {fmt(lo)}
          </Typography>
        </Stack>
        <Box sx={{ position: 'relative', height: plot, flexGrow: 1, minWidth: 0 }}>
          <Box
            component="svg"
            viewBox={`0 0 ${W} ${plot}`}
            preserveAspectRatio="none"
            sx={{ width: '100%', height: plot, display: 'block', overflow: 'visible' }}
          >
            {/* Concrete colours from the theme, never palette paths: `sx` does not resolve those
                for SVG presentation properties, so `fill: 'primary.main'` reaches the DOM verbatim
                and the shape silently draws nothing. That is exactly how the chart this replaces
                once shipped as dots with no line between them. */}
            <Box
              component="polygon"
              points={bandPoints(bands, W, plot, domain)}
              sx={{ fill: theme.palette.primary.main, opacity: 0.18 }}
            />
            <Box
              component="polyline"
              points={linePoints(mean, W, plot, domain)}
              sx={{
                fill: 'none',
                stroke: theme.palette.primary.main,
                strokeWidth: 1,
                opacity: 0.55,
                vectorEffect: 'non-scaling-stroke',
              }}
            />
            <Box
              component="polyline"
              points={linePoints(best, W, plot, domain)}
              sx={{
                fill: 'none',
                stroke: theme.palette.warning.main,
                strokeWidth: 1.5,
                vectorEffect: 'non-scaling-stroke',
              }}
            />
          </Box>
          {/* The dots are DOM rather than SVG, so each carries an ordinary MUI hover and stays
              round under the non-uniform scaling the viewBox applies to the lines. */}
          {linePercents(best, domain).map((p, i) => (
            <Tooltip
              key={rows[i].idx}
              placement="top"
              title={
                `batch ${rows[i].idx} — best so far ${fmt(rows[i].best_so_far)}` +
                ` · this round ${fmt(rows[i].min)}–${fmt(rows[i].max)}` +
                ` (mean ${fmt(rows[i].mean)})` +
                ` · ${rows[i].n_scored}/${rows[i].n_units} scored` +
                (rows[i].complete ? '' : ' so far')
              }
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
      <AxisEnds
        left={`batch ${rows[0].idx}`}
        right={`batch ${rows[rows.length - 1].idx}`}
      />
      {stage?.outcome === 'condition' ? (
        <Typography variant="caption" color="warning.main" sx={{ fontSize: 9, display: 'block' }}>
          {endedHere(stage)}
        </Typography>
      ) : null}
      {/* The objective's name and an arrow for its direction, because neither is inferable from
          the curve: a falling line is progress for a minimized objective and a regression for a
          maximized one. Kept to a name and one glyph so it still fits the Details panel's ~190px
          column on one line — the sentence version wrapped to three there and pushed the card
          open. The rest of the explanation lives in the hover, which costs no width. */}
      <Tooltip title={`${goal} · the band is each round's range, the line the best so far`}>
        <Typography
          variant="caption"
          color="text.secondary"
          noWrap
          sx={{ fontSize: 9, display: 'block', cursor: 'help' }}
        >
          {stage?.objective_name ?? 'objective'} {stage?.direction === 'minimize' ? '↓' : '↑'}
        </Typography>
      </Tooltip>
    </Box>
  )
}
