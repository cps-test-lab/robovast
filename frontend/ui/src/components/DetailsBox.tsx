import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { BatchObjectiveChart } from './BatchObjectiveChart'
import { CollapsibleBox } from './CollapsibleBox'
import { DetailsCharts as Charts } from './DetailsCharts'
import { robovast, type SearchHistory } from '@/lib/robovastClient'
import { formatBytes, formatDuration } from '@/lib/format'
import {
  CPU_HEADROOM,
  MEM_HEADROOM,
  DETAILS_ACTIONS_SQL,
  DETAILS_CPU_SQL,
  DETAILS_DECLARED_CPU_SQL,
  DETAILS_MAX_ROWS,
  DETAILS_RUNS_SQL,
  cpuHeadline,
  durationHistogram,
  formatCpu,
  formatMemQuantity,
  jobsInFlight,
  memHeadline,
  summariseActions,
  summariseBatches,
  summariseCpu,
  totals,
  type ActionRow,
  type CpuRow,
  type CpuSummary,
  type DeclaredCpuRow,
  type RunRow,
} from '@/lib/campaignDetails'

// "Did this run well, and what did it cost?" for one finished campaign -- the question the
// Results Explorer does not answer. Small charts side by side, because the Monitor page exists to
// show campaigns and their status: this panel widens, it does not grow.
//
// **It does not query until it is opened.** Every campaign on the page would otherwise run four
// SQL statements on mount, and the CPU one scans every 1 Hz sample of every run. Behind a closed
// box that cost is zero, which is what lets the panel exist on a list view at all.
//
// The charts are imported directly, not lazily. They were Vega specs, and `lazyView` was here to
// keep that library out of Monitor's entry chunk (Monitor is the default route and is not
// code-split). They are DOM now -- see `DetailsCharts.tsx` for why -- so there is nothing left to
// defer, and the charts arrive with the frame instead of a moment after it.

/** Height of one column's chart, and the reason every column looks the same height.
 *
 *  Sized to what the columns actually DRAW, not to a round number: the resource columns are a 46px
 *  ring beside three or four 14px bar rows, and the lists are five ~13px rows — about 62px of
 *  content plus a 14px axis line. At the old 110 the bar and list columns were padded with dead
 *  space while the histogram, which fills whatever it is given, stood a head taller than its
 *  neighbours. A column with more rows than fits still grows; this is a floor, not a clamp. */
export const CHART_HEIGHT = 76

/** Below this many runs, the distribution columns say so instead of drawing one. */
const MIN_DISTRIBUTION_RUNS = 3

/** Same footprint as a chart, saying why there isn't one. */
function TooFew({ height, runs }: { height: number; runs: number }) {
  return (
    <Box sx={{ height, display: 'flex', alignItems: 'center' }}>
      <Typography variant="caption" color="text.secondary">
        {runs === 1 ? '1 run' : `${runs} runs`}
      </Typography>
    </Box>
  )
}

/** The headline for the Objective column: the last batch's best-so-far, which IS the campaign's
 *  best by construction — the service folds it in the objective's declared direction, so this no
 *  longer has to guess that a bigger number is a better one. */
function bestOf(history: SearchHistory): string | undefined {
  const last = [...history.batches].reverse().find((b) => b.best_so_far != null)
  return last?.best_so_far == null ? undefined : `best ${last.best_so_far.toPrecision(3)}`
}

function useDetails(campaignId: string, enabled: boolean, postprocessed: boolean) {
  return useQuery({
    // `postprocessed` is part of the key so the answer is re-fetched when the metric tables
    // arrive: a finished campaign is postprocessed a few minutes LATER, and without it the
    // panel caches the pre-postprocessing answer for the whole session.
    queryKey: ['campaign-details', campaignId, postprocessed],
    enabled,
    retry: false,
    // Within one postprocessing state the rows cannot change, so this is read once.
    staleTime: Infinity,
    queryFn: async () => {
      const runs = await robovast.queryCampaignDataSql(
        campaignId,
        DETAILS_RUNS_SQL,
        DETAILS_MAX_ROWS,
      )
      // The CPU pair is allowed to fail: `resource_usage` lives in data.db, which a campaign
      // that was never postprocessed does not have. That is a missing column, not a broken
      // panel, so it resolves to [] and `summariseCpu` returns null from there.
      //
      // The actions query is allowed to fail for the same reason and one more: `behaviors` is
      // written by scenario_execution's --bt-log, so a campaign whose trials are not driven by a
      // scenario tree has no such table at all. Each optional query resolves to [] on its own, so
      // one missing table costs one column rather than the panel.
      //
      // Each carries its own failure rather than flattening to []: "no rows" and "the query
      // failed" are different facts, and the CPU column states a CAUSE. It once said "not
      // postprocessed" about a campaign with 43k rows of `resource_usage`, because a swallowed
      // error and an absent table were indistinguishable by the time it rendered.
      const optional = <T,>(sql: string) =>
        robovast
          .queryCampaignDataSql(campaignId, sql, DETAILS_MAX_ROWS)
          .then((r) => ({ rows: r.rows as T[], error: null as string | null }))
          .catch((e) => ({ rows: [] as T[], error: (e as Error)?.message ?? 'query failed' }))
      const [cpu, declared, actions] = await Promise.all([
        optional<CpuRow>(DETAILS_CPU_SQL),
        optional<DeclaredCpuRow>(DETAILS_DECLARED_CPU_SQL),
        optional<ActionRow>(DETAILS_ACTIONS_SQL),
      ])
      return {
        runs: runs.rows as RunRow[],
        cpu: cpu.rows,
        cpuError: cpu.error,
        declared: declared.rows,
        actions: actions.rows,
      }
    },
  })
}

/** The recommendation, per container, as a hover.
 *
 *  This is what the panel is FOR — the rest of it is context for this table. It is a hover rather
 *  than a permanent block because it is read once, when sizing the `.vast`, and then never again;
 *  the chart beside it is what gets looked at repeatedly.
 *
 *  Every container gets a line, including the ones with nothing to suggest, because a table that
 *  silently omits a container reads as a complete answer for a pod it did not measure. */
function CpuAdvice({
  cpu,
  inFlightNow,
  inFlightThen,
}: {
  cpu: CpuSummary
  /** How many pods of the declared size fit the lane's quota; null when nothing was declared or
   *  the lane's capacity is unknown. */
  inFlightNow: number | null
  inFlightThen: number | null
}) {
  const cell = { padding: '1px 8px 1px 0', whiteSpace: 'nowrap' as const, fontSize: 11 }
  return (
    <Box>
      <Box component="table" sx={{ borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ opacity: 0.7 }}>
            <th style={{ ...cell, textAlign: 'left' }}>container</th>
            <th style={{ ...cell, textAlign: 'right' }}>declared</th>
            <th style={{ ...cell, textAlign: 'right' }}>median</th>
            <th style={{ ...cell, textAlign: 'right' }}>p95</th>
            <th style={{ ...cell, textAlign: 'right' }}>peak</th>
            <th style={{ ...cell, textAlign: 'right' }}>suggested</th>
          </tr>
        </thead>
        <tbody>
          {cpu.containers.map((c) => (
            <tr key={c.container}>
              <td style={{ ...cell, textAlign: 'left' }}>
                {c.container}
                {c.bursty ? ' ⚡' : ''}
              </td>
              <td style={{ ...cell, textAlign: 'right' }}>
                {c.declared === null ? '—' : formatCpu(c.declared)}
              </td>
              <td style={{ ...cell, textAlign: 'right' }}>{c.p50.toFixed(2)}</td>
              <td style={{ ...cell, textAlign: 'right' }}>{c.p95.toFixed(2)}</td>
              <td style={{ ...cell, textAlign: 'right' }}>{c.peak.toFixed(2)}</td>
              <td style={{ ...cell, textAlign: 'right', fontWeight: 600 }}>
                {c.suggested === null ? `too few samples (${c.ticks})` : formatCpu(c.suggested)}
              </td>
            </tr>
          ))}
          {cpu.declaredPod !== null || cpu.suggestedPod !== null ? (
            <tr style={{ borderTop: '1px solid rgba(255,255,255,0.2)' }}>
              <td style={{ ...cell, textAlign: 'left', fontWeight: 600 }}>pod</td>
              <td style={{ ...cell, textAlign: 'right' }}>
                {cpu.declaredPod === null ? '—' : formatCpu(cpu.declaredPod)}
              </td>
              <td style={cell} colSpan={3} />
              <td style={{ ...cell, textAlign: 'right', fontWeight: 600 }}>
                {cpu.suggestedPod === null ? '—' : formatCpu(cpu.suggestedPod)}
              </td>
            </tr>
          ) : null}
        </tbody>
      </Box>
      {/* The payoff, where the numbers it is derived from are. Gated on the SUGGESTED figure
          alone: a campaign that declares no cpu has nothing to show an arrow from, and that is
          exactly the campaign whose author most needs the number. Labelled an estimate because it
          is one -- it assumes the whole quota is free and that packing is perfect, and a pod is
          atomic, so one needing more than the free quota waits however much total exists. The
          dependable half is that a smaller request fits more often. */}
      {inFlightThen ? (
        <Box sx={{ mt: 0.75, fontSize: 11 }}>
          {inFlightNow ? `${inFlightNow} → ${inFlightThen}` : inFlightThen} jobs in flight (est.)
        </Box>
      ) : null}
      {/* One line. The hover is read while editing a `.vast`, so it has to say where the number
          goes and how it was reached -- and stop there. */}
      <Box component="p" sx={{ mt: 0.5, mb: 0, maxWidth: 320, fontSize: 11, opacity: 0.85 }}>
        p95 + {Math.round((CPU_HEADROOM - 1) * 100)}% headroom, rounded up to a quarter core, for{' '}
        <code>execution.containers.&lt;name&gt;.resources.cpu</code>.
        {cpu.containers.some((c) => c.bursty) ? ' ⚡ throttled during its bursts.' : ''}
      </Box>
    </Box>
  )
}

/** The memory equivalent of `CpuAdvice`. Its own component rather than a mode of that one: the
 *  columns differ (there is no burst notion for memory, and no jobs-in-flight estimate, since the
 *  quota that binds a RoboVAST campaign is CPU), and the sizing rule it has to explain is a
 *  different rule. */
function MemAdvice({ cpu }: { cpu: CpuSummary }) {
  const cell = { padding: '1px 8px 1px 0', whiteSpace: 'nowrap' as const, fontSize: 11 }
  const head = { ...cell, textAlign: 'right' as const, fontWeight: 400, opacity: 0.7 }
  return (
    <Box>
      <Box component="table" sx={{ borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={{ ...head, textAlign: 'left' }}>container</th>
            <th style={head}>declared</th>
            <th style={head}>mean</th>
            <th style={head}>peak</th>
            <th style={head}>suggested</th>
          </tr>
        </thead>
        <tbody>
          {cpu.containers.map((c) => (
            <tr key={c.container}>
              <td style={{ ...cell, textAlign: 'left' }}>{c.container}</td>
              <td style={{ ...cell, textAlign: 'right' }}>
                {c.mem.declared === null ? '—' : formatMemQuantity(c.mem.declared)}
              </td>
              <td style={{ ...cell, textAlign: 'right' }}>{formatBytes(c.mem.mean)}</td>
              <td style={{ ...cell, textAlign: 'right' }}>{formatBytes(c.mem.peak)}</td>
              <td style={{ ...cell, textAlign: 'right', fontWeight: 600 }}>
                {c.mem.suggested === null ? '—' : formatMemQuantity(c.mem.suggested)}
              </td>
            </tr>
          ))}
          {cpu.declaredMemPod !== null || cpu.suggestedMemPod !== null ? (
            <tr style={{ borderTop: '1px solid rgba(255,255,255,0.2)' }}>
              <td style={{ ...cell, textAlign: 'left', fontWeight: 600 }}>pod</td>
              <td style={{ ...cell, textAlign: 'right' }}>
                {cpu.declaredMemPod === null ? '—' : formatMemQuantity(cpu.declaredMemPod)}
              </td>
              <td style={cell} colSpan={2} />
              <td style={{ ...cell, textAlign: 'right', fontWeight: 600 }}>
                {cpu.suggestedMemPod === null ? '—' : formatMemQuantity(cpu.suggestedMemPod)}
              </td>
            </tr>
          ) : null}
        </tbody>
      </Box>
      <Box component="p" sx={{ mt: 0.5, mb: 0, maxWidth: 340, fontSize: 11, opacity: 0.85 }}>
        peak + {Math.round((MEM_HEADROOM - 1) * 100)}% headroom, rounded up to 128Mi, for{' '}
        <code>execution.containers.&lt;name&gt;.resources.memory</code>. Sized on the peak, not a
        percentile: a memory over-run is an OOM kill, not throttling. RSS is summed per process
        name, so these are upper bounds.
      </Box>
    </Box>
  )
}

/** One figure in the strip. */
function Stat({ value, label, tip }: { value: string; label: string; tip?: string }) {
  const body = (
    <Stack direction="row" spacing={0.5} alignItems="baseline">
      <Typography variant="caption" sx={{ fontWeight: 600 }}>
        {value}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
    </Stack>
  )
  return tip ? <Tooltip title={tip}>{body}</Tooltip> : body
}

function Column({
  title,
  meta,
  metaTip,
  children,
}: {
  title: string
  meta?: string
  /** Hover for the meta text. Underlined when present, so there is something to aim at. */
  metaTip?: React.ReactNode
  children: React.ReactNode
}) {
  const label = (
    <Typography
      variant="caption"
      color={metaTip ? 'text.primary' : 'text.secondary'}
      noWrap
      sx={
        metaTip
          ? { cursor: 'help', textDecoration: 'underline dotted', textUnderlineOffset: 3 }
          : undefined
      }
    >
      {meta}
    </Typography>
  )
  return (
    <Box sx={{ minWidth: 0 }}>
      <Stack direction="row" spacing={1} alignItems="baseline" sx={{ mb: 0.25 }}>
        <Typography variant="caption" sx={{ fontWeight: 600 }}>
          {title}
        </Typography>
        <Box flexGrow={1} />
        {meta ? (
          metaTip ? (
            <Tooltip title={metaTip} placement="top">
              {label}
            </Tooltip>
          ) : (
            label
          )
        ) : null}
      </Stack>
      {children}
    </Box>
  )
}

export function DetailsBox({
  campaignId,
  quotaCpu,
  postprocessed = false,
}: {
  campaignId: string
  /** Lane CPU capacity, for the "jobs in flight" estimate. Omitted when unknown, and then
   *  the estimate is simply not shown -- there is no default worth inventing. */
  quotaCpu?: number | null
  /** Whether the campaign's metric tables exist yet. Part of the query key, not a display flag:
   *  a campaign can be FINISHED and not yet postprocessed, and then its `resource_usage` rows
   *  appear minutes later. Without this the first answer -- correctly "not postprocessed" -- was
   *  cached for the session and the columns never filled in. */
  postprocessed?: boolean
}) {
  // Closed by default, always: the campaign list holds every campaign, and this panel's three
  // queries include one that scans every 1 Hz sample of every run. Opening it on mount would
  // charge a page of twenty cards for twenty campaigns nobody asked about.
  const [open, setOpen] = useState(false)
  const { data, isLoading, isError, error } = useDetails(campaignId, open, postprocessed)
  // Its own query rather than a fourth SQL statement in `useDetails`: this one is served by the
  // service from `campaign.db` directly, which is what makes the identical chart work on a
  // campaign that is still running (a SQL query cannot answer that on the cluster lane).
  const objective = useQuery({
    queryKey: ['search-history', campaignId, 'details'],
    queryFn: () => robovast.getSearchHistory(campaignId),
    enabled: open,
    retry: false,
    staleTime: Infinity,
  })

  const model = useMemo(() => {
    if (!data) return null
    const cpu = summariseCpu(data.cpu, data.declared)
    const batches = summariseBatches(data.runs)
    return {
      cpu,
      batches,
      actions: summariseActions(data.actions),
      totals: totals(data.runs, cpu),
      histogram: durationHistogram(data.runs),
      // `summariseBatches` returns the campaign row alone when there is only one batch, so
      // this is also the "should anything be coloured by batch?" test.
      multiBatch: batches.length > 1,
    }
  }, [data])

  const all = model?.batches[0]

  // A histogram or a sweep curve drawn from one or two runs is a single full-width block with
  // an axis around it -- it looks like a finding and is none. Campaigns of one run are ordinary
  // (a pilot, a smoke test), so this is a normal case rather than an edge case. Gated on the
  // RUN count, not the chart's row count: the sweep line emits two points per run, which made a
  // 1-run campaign announce "2 runs".
  const enoughRuns = (all?.runs ?? 0) >= MIN_DISTRIBUTION_RUNS

  const inFlightNow = jobsInFlight(model?.cpu?.declaredPod ?? null, quotaCpu ?? null)
  const inFlightThen = jobsInFlight(model?.cpu?.suggestedPod ?? null, quotaCpu ?? null)

  return (
    <CollapsibleBox
      // No meta. The collapsed header carried the cpu headline, which put a RECOMMENDATION on the
      // campaign card itself -- beside the phase and the run bar, where everything else is a fact
      // about what happened. The CPU column says it, one click in, next to the evidence for it.
      title="Details"
      open={open}
      onToggle={() => setOpen((o) => !o)}
    >
      <Box sx={{ p: 1 }}>
        {isLoading ? (
          <Typography variant="caption" color="text.secondary">
            reading the campaign's runs…
          </Typography>
        ) : isError ? (
          <Typography variant="caption" color="text.secondary">
            no queryable data for this campaign ({(error as Error)?.message})
          </Typography>
        ) : model && all ? (
          <Stack spacing={1}>
            <Box
              sx={{
                display: 'grid',
                // Wraps rather than squashing: a narrow card gets two rows of two, which is
                // still shorter than four stacked full-width charts.
                gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
                gap: 1.5,
              }}
            >
              {/* How long it took, in three figures on one line, and then every run's duration as
                  a single strip. The strip replaced a column of per-run bars: one line carries the
                  same comparison (which runs were slow, and were they the failing ones) in a
                  fifth of the height, which is what let memory have a column. */}
              {/* The median rides here rather than on the histogram: this is the column of
                  numbers, and the histogram's own axis ends already state its range. */}
              <Column
                title="Overview"
                meta={
                  all.medianDuration === null
                    ? undefined
                    : `median ${formatDuration(all.medianDuration)}`
                }
              >
                <Stack
                  direction="row"
                  spacing={1.5}
                  flexWrap="wrap"
                  useFlexGap
                  sx={{ mb: 0.75 }}
                >
                  {model.totals.cpuHours !== null ? (
                    <Stat
                      value={model.totals.cpuHours.toFixed(1)}
                      label="CPU-hours"
                      tip="What the campaign actually consumed, summed over every 1 Hz sample."
                    />
                  ) : null}
                  <Stat
                    value={formatDuration(model.totals.simulatedSeconds)}
                    label="simulated"
                    tip={
                      'Summed run durations. The simulator is launched at realtime pacing, so ' +
                      'one simulated second is one wall second.'
                    }
                  />
                  {model.totals.runsPerMinute !== null ? (
                    <Stat
                      value={model.totals.runsPerMinute.toFixed(
                        model.totals.runsPerMinute < 10 ? 1 : 0,
                      )}
                      label="runs/min"
                      tip={
                        'Throughput: completed runs per minute of wall clock, from the first ' +
                        'run starting to the last one finishing' +
                        (model.totals.wallSeconds
                          ? ` (${formatDuration(model.totals.wallSeconds)})`
                          : '') +
                        '. This is the number a smaller CPU reservation moves — halve the pod ' +
                        'and twice as many fit the quota. Counted in runs, which is what the ' +
                        'data records; a packed job carries several of them.'
                      }
                    />
                  ) : null}
                  {model.multiBatch ? (
                    <Stat value={String(model.totals.batches)} label="batches" />
                  ) : null}
                  {/* Only when it happened, like every other optional stat here — but then
                      always, because a short dataset that looks complete is the failure this
                      exists to prevent. It sits beside the counts rather than with the
                      failures: nothing was learned from these runs either way. */}
                  {all.killed ? (
                    <Stat
                      value={String(all.killed)}
                      label={all.killed === 1 ? 'run stopped' : 'runs stopped'}
                      tip={
                        'Runs an operator stopped by hand while the campaign ran. They produced ' +
                        'no result and are NOT trial failures — exclude them from pass rates ' +
                        "(WHERE status <> 'killed'). Each row's failure_message says why."
                      }
                    />
                  ) : null}
                </Stack>
              </Column>

              <Column
                title="CPU"
                meta={model.cpu ? (cpuHeadline(model.cpu) ?? undefined) : undefined}
                // The recommendation itself, and the in-flight estimate with it. On the header
                // rather than on a bar, so it is reachable without hunting for the right
                // container's box first.
                metaTip={
                  model.cpu ? (
                    <CpuAdvice cpu={model.cpu} inFlightNow={inFlightNow} inFlightThen={inFlightThen} />
                  ) : undefined
                }
              >
                {model.cpu && model.cpu.containers.every((c) => c.lowSample) ? (
                  // Every bar in this chart is `suggested`, so when nothing can be suggested the
                  // chart is an axis around empty space. Sub-second runs sampled at 1 Hz hit this
                  // routinely, and the reason is the useful thing to show.
                  <Typography variant="caption" color="text.secondary">
                    too few samples ({model.cpu.containers[0].ticks}) — runs are shorter than the
                    1 Hz sampler
                  </Typography>
                ) : model.cpu ? (
                  <Charts kind="cpu" rows={model.cpu.containers} height={CHART_HEIGHT} />
                ) : (
                  <Typography variant="caption" color="text.secondary">
                    {data?.cpuError
                      ? `could not read resource usage — ${data.cpuError}`
                      : 'not postprocessed'}
                  </Typography>
                )}
              </Column>

              {/* Memory beside CPU, same shape: a reservation nobody has evidence for is the same
                  problem in a different unit, and the two are declared on the same two lines of
                  the `.vast`. */}
              {model.cpu && !model.cpu.containers.every((c) => c.lowSample) ? (
                <Column
                  title="Memory"
                  meta={memHeadline(model.cpu) ?? undefined}
                  metaTip={<MemAdvice cpu={model.cpu} />}
                >
                  <Charts kind="memory" rows={model.cpu.containers} height={CHART_HEIGHT} />
                </Column>
              ) : null}

              {/* Where the trials ended. Absent rather than empty when the campaign's runs are
                  not driven by a scenario tree -- there is then nothing to attribute to. */}
              {model.actions.length ? (
                <Column title="Ended in" meta={`top ${new Set(model.actions.map((a) => a.action)).size}`}>
                  <Charts kind="actions" rows={model.actions} height={CHART_HEIGHT} />
                </Column>
              ) : null}

              {/* The distribution the heat strip cannot show. Gated on the run count: a histogram
                  of two runs is two blocks with an axis around them, which looks like a finding
                  and is none. */}
              <Column title="Duration">
                {enoughRuns ? (
                  <Charts kind="histogram" rows={model.histogram} height={CHART_HEIGHT} />
                ) : (
                  <TooFew height={CHART_HEIGHT} runs={all.runs} />
                )}
              </Column>

              {/* The same chart the campaign card shows live, from the same route — so the
                  panel and the live view cannot disagree about a search's trajectory, which two
                  separately-derived charts eventually would. It also fixes what the local
                  derivation got wrong: the per-batch summary here hardcoded `maximize`, so a
                  MINIMIZING campaign reported its worst value as its best. Direction is the
                  service's answer now, read from the campaign's own config. */}
              {objective.data && !objective.data.unavailable ? (
                <Column
                  title="Objective"
                  meta={bestOf(objective.data)}
                >
                  <BatchObjectiveChart history={objective.data} height={CHART_HEIGHT} />
                </Column>
              ) : null}
            </Box>
          </Stack>
        ) : null}
      </Box>
    </CollapsibleBox>
  )
}
