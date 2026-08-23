import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { useTheme } from '@mui/material/styles'
import { AxisEnds, Note } from './chartPrimitives'
import { bandDomain, bandPoints, linePercents, linePoints } from '@/lib/detailsGeometry'
import type { BatchObjective, SearchHistory } from '@/lib/robovastClient'

// A search's objective over its batches: the spread each round measured, and the best found so far
// laid over it.
//
// One chart, two callers. The campaign card renders it live from `/search/history` while a search
// runs; the Details panel renders it from the same route afterwards. It used to be `ObjectiveLine`
// in DetailsCharts.tsx, post-hoc only and best-per-batch only — the two views are now the same
// component reading the same route, so they cannot drift apart or disagree.
//
// Why the band and not just the line: a flat best-so-far has two very different causes, and the
// decision the reader is making (let it run, or stop it) depends on which. If the spread is still
// wide the search is exploring and may yet find something; if the spread has collapsed onto the
// best value, the strategy is re-sampling one region and further batches will buy nothing. The
// best-so-far line alone cannot tell those apart.
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
function reason(history: SearchHistory, drawn: number): string | null {
  if (history.unavailable === 'multi_objective') {
    return 'several objectives — no single value to trend'
  }
  if (history.unavailable === 'batch_mode') return 'not a search'
  if (history.unavailable === 'no_store') return 'nothing recorded yet'
  if (drawn === 0) return 'no objective measured yet'
  if (drawn === 1) return 'one round so far'
  return null
}

export function BatchObjectiveChart({
  history,
  height = 110,
}: {
  history: SearchHistory
  height?: number
}) {
  const theme = useTheme()
  const rows = drawable(history.batches)
  const why = reason(history, rows.length)
  if (why) return <Note height={height}>{why}</Note>

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
  const goal = history.direction === 'minimize' ? 'lower is better' : 'higher is better'

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
                ` · ${rows[i].n_scored}/${rows[i].n_units} scored`
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
          {history.objective_name ?? 'objective'} {history.direction === 'minimize' ? '↓' : '↑'}
        </Typography>
      </Tooltip>
    </Box>
  )
}
