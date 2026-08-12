// The Details panel's pure model. Tested here rather than through the UI because every number
// the panel shows is a claim about what a campaign cost, and a wrong one is invisible: a
// histogram binned from the wrong origin, or a p95 taken over process rows instead of ticks, still
// renders as a plausible chart. `tsc` cannot catch any of it.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import {
  CPU_MIN_TICKS,
  MEM_HEADROOM,
  ceilToMemUnit,
  ceilToQuarter,
  formatMemQuantity,
  formatCpu,
  cpuHeadline,
  HISTOGRAM_BINS,
  HISTOGRAM_MIN_SPAN,
  durationHistogram,
  jobsInFlight,
  summariseBatches,
  summariseActions,
  summariseCpu,
  totals,
  outlierRuns,
  TOP_ACTIONS,
  type RunRow,
} from './campaignDetails'

/** One `run_view` row, with the columns the panel selects. */
function run(
  status: string,
  extra: {
    batch?: number | null
    duration?: number | null
    start?: string | null
    objective?: number | null
  } = {},
): RunRow {
  return {
    status,
    batch: extra.batch ?? 0,
    duration_s: extra.duration === undefined ? 60 : extra.duration,
    start_time: extra.start === undefined ? '2026-08-12T10:00:00+00:00' : extra.start,
    objective: extra.objective ?? null,
  }
}

describe('ceilToQuarter', () => {
  it('rounds up to the next quarter core and never below the granularity', () => {
    expect(ceilToQuarter(3.8 * 1.25)).toBe(4.75)
    expect(ceilToQuarter(2.2 * 1.25)).toBe(2.75)
    expect(ceilToQuarter(0.3 * 1.25)).toBe(0.5)
    expect(ceilToQuarter(0)).toBe(0.25)
    expect(ceilToQuarter(-1)).toBe(0.25)
  })

  it('leaves a value already on a quarter alone, so nobody adds a float-error guard', () => {
    // Dividing by 0.25 is an exponent shift and so exact for every finite input: a value on a
    // quarter cannot ceil up to the next one. Asserted because the opposite is easy to assume,
    // and "defensively" rounding the quotient would cost every container a spare quarter core.
    expect(ceilToQuarter(4.75)).toBe(4.75)
    expect(ceilToQuarter(1)).toBe(1)
    expect(ceilToQuarter(0.25)).toBe(0.25)
  })
})

describe('summariseCpu', () => {
  const cpuRows = [
    { container: 'simulation', p95: 3.8, peak: 4.4, core_seconds: 1_440_000, ticks: 400 },
    { container: 'sut', p95: 2.2, peak: 2.4, core_seconds: 320_400, ticks: 400 },
  ]
  const declaredRows = [
    { fullkey: '$.execution.containers.simulation.resources.cpu', value: 6 },
    { fullkey: '$.execution.containers.sut.resources.cpu', value: 6 },
  ]

  it('sizes on p95 with headroom and sums the pod across containers', () => {
    const cpu = summariseCpu(cpuRows, declaredRows)
    expect(cpu?.containers.map((c) => [c.container, c.suggested])).toEqual([
      ['simulation', 4.75],
      ['sut', 2.75],
    ])
    expect(cpu?.declaredPod).toBe(12)
    expect(cpu?.suggestedPod).toBe(7.5)
  })

  it('flags a burst only when peak is at least double sustained', () => {
    // `peak > suggested` was the obvious test and was useless: with a reservation of p95 x 1.25
    // it fired on every container of every real campaign, whose peak/p95 runs 1.3-2.5.
    const bursty = summariseCpu([{ container: 'sut', p95: 1, peak: 6, ticks: 400 }], [])
    expect(bursty?.containers[0].bursty).toBe(true)
    // Real values from experiment-random-recovery: peak/p95 = 1.33, ordinary variation.
    const ordinary = summariseCpu([{ container: 'sut', p95: 1.98, peak: 2.64, ticks: 400 }], [])
    expect(ordinary?.containers[0].bursty).toBe(false)
    expect(ordinary?.containers[0].peak).toBeGreaterThan(ordinary?.containers[0].suggested as number)
  })

  it('withholds a suggestion when there were too few samples to take a percentile', () => {
    // The quadrotor campaigns run sub-second scenarios, so 32 runs yield 7 in-window ticks in
    // total -- a p95 over 7 points is the maximum wearing a percentile's name, and it produced a
    // confident "0.5 cpu, 192 jobs in flight".
    const thin = summariseCpu([{ container: 'robovast', p95: 0.283, peak: 0.319, ticks: 7 }], [])
    expect(thin?.containers[0]).toMatchObject({ lowSample: true, suggested: null, peak: 0.319 })
    expect(thin?.suggestedPod).toBeNull()
    expect(cpuHeadline(thin)).toBeNull()
    const enough = summariseCpu(
      [{ container: 'robovast', p95: 0.283, peak: 0.319, ticks: CPU_MIN_TICKS }], [])
    expect(enough?.containers[0].lowSample).toBe(false)
    expect(enough?.suggestedPod).toBe(0.5)
  })

  it('reads millicore strings, and treats an unparseable declaration as absent', () => {
    const cpu = summariseCpu(
      [
        { container: 'scenario', p95: 0.3, peak: 0.4, ticks: 400 },
        { container: 'sut', p95: 2.2, peak: 2.4, ticks: 400 },
      ],
      [
        { fullkey: '$.execution.containers.scenario.resources.cpu', value: '500m' },
        { fullkey: '$.execution.containers.sut.resources.cpu', value: 'lots' },
      ],
    )
    const scenario = cpu?.containers.find((c) => c.container === 'scenario')
    expect(scenario?.declared).toBe(0.5)
    expect(cpu?.containers.find((c) => c.container === 'sut')?.declared).toBeNull()
    // One container missing a declaration makes the pod total a different quantity from the
    // suggestion it would be compared against, so it reads as unknown rather than as a partial sum.
    expect(cpu?.declaredPod).toBeNull()
  })

  it('pairs the measured main container with its declared name', () => {
    // Real names from experiment-random-recovery-robosito-2026-08-12-13151291: `resource_usage`
    // records the MAIN container under the fixed role name `robovast`, while the .vast declares
    // it as `scenario` (see expected_container_files in results_processing/resource_usage.py).
    // A plain name join leaves it undeclared, which nulls the pod total and hides the headline
    // on every real campaign.
    const cpu = summariseCpu(
      [
        { container: 'robovast', p95: 0.695, peak: 1.216, ticks: 400 },
        { container: 'simulation', p95: 0.409, peak: 0.982, ticks: 400 },
        { container: 'sut', p95: 1.981, peak: 2.639, ticks: 400 },
      ],
      [
        { fullkey: '$.execution.containers.scenario.resources.cpu', value: 4 },
        { fullkey: '$.execution.containers.sut.resources.cpu', value: 4 },
        { fullkey: '$.execution.containers.simulation.resources.cpu', value: 1 },
      ],
    )
    const main = cpu?.containers.find((c) => c.container === 'scenario')
    expect(main?.declared).toBe(4)
    expect(cpu?.containers.some((c) => c.container === 'robovast')).toBe(false)
    // 0.695/0.409/1.981 p95 -> 1.0/0.75/2.5 suggested. This campaign reserved 9 cpu per pod and
    // needed 4.25.
    expect(cpu?.declaredPod).toBe(9)
    expect(cpu?.suggestedPod).toBe(4.25)
  })

  it('names the main container even when the .vast declares no cpu at all', () => {
    // The case the panel matters most for: nothing is reserved, so this hover is the only place
    // saying what to set -- and it must name a container the reader can find in their file.
    // `robovast` is an internal role name that appears in no .vast, so the bare container
    // declarations are queried alongside the cpu values precisely to resolve it here.
    const cpu = summariseCpu(
      [
        { container: 'robovast', p95: 0.71, peak: 1.24, ticks: 591 },
        { container: 'sut', p95: 1.32, peak: 2.48, ticks: 393 },
        { container: 'simulation', p95: 0.4, peak: 0.77, ticks: 591 },
      ],
      [
        { fullkey: '$.execution.containers.scenario', value: null },
        { fullkey: '$.execution.containers.sut', value: null },
        { fullkey: '$.execution.containers.simulation', value: null },
      ],
    )
    expect(cpu?.containers.map((c) => c.container).sort()).toEqual([
      'scenario',
      'simulation',
      'sut',
    ])
    // Nothing declared, so there is nothing to compare against -- but the suggestion stands on
    // the measurement alone, and the headline says so rather than going quiet.
    expect(cpu?.declaredPod).toBeNull()
    expect(cpu?.suggestedPod).toBe(1 + 1.75 + 0.5)
    expect(cpuHeadline(cpu)).toBe('3.25 cpu suggested')
  })

  it('declines to pair the main container when the leftovers are ambiguous', () => {
    // A declared container that produced no samples leaves two names unclaimed; guessing would
    // compare one container's use against another's reservation.
    const cpu = summariseCpu(
      [{ container: 'robovast', p95: 1, peak: 1, ticks: 400 }],
      [
        { fullkey: '$.execution.containers.scenario.resources.cpu', value: 4 },
        { fullkey: '$.execution.containers.simulation.resources.cpu', value: 8 },
      ],
    )
    expect(cpu?.containers[0]).toMatchObject({ container: 'robovast', declared: null })
    expect(cpu?.declaredPod).toBeNull()
  })

  it('returns null when the campaign was never postprocessed', () => {
    // "No measurement" must not render as "used nothing" -- the panel omits the column instead.
    expect(summariseCpu([], [])).toBeNull()
  })

  it('reports core-seconds as CPU-hours', () => {
    expect(summariseCpu(cpuRows, declaredRows)?.cpuHours).toBeCloseTo(489, 0)
  })
})

describe('jobsInFlight', () => {
  it('floors the quota by the pod request', () => {
    expect(jobsInFlight(14, 96)).toBe(6)
    expect(jobsInFlight(7.5, 96)).toBe(12)
  })

  it('declines to guess when either side is unknown', () => {
    expect(jobsInFlight(null, 96)).toBeNull()
    expect(jobsInFlight(8, null)).toBeNull()
    expect(jobsInFlight(0, 96)).toBeNull()
  })
})

describe('summariseBatches', () => {
  it('omits the per-batch row for a batch-mode campaign, which has exactly one batch', () => {
    const summaries = summariseBatches([run('passed'), run('failed')])
    expect(summaries).toHaveLength(1)
    expect(summaries[0].batch).toBeNull()
  })

  it('puts the campaign row first, then one row per batch in order', () => {
    const summaries = summariseBatches([
      run('passed', { batch: 1 }),
      run('failed', { batch: 0 }),
      run('passed', { batch: 0 }),
    ])
    expect(summaries.map((s) => s.batch)).toEqual([null, 0, 1])
    expect(summaries[0].runs).toBe(3)
    expect(summaries[1].runs).toBe(2)
  })

  it('counts error as failed and keeps the three tallies summing to runs', () => {
    const [all] = summariseBatches([
      run('passed'),
      run('failed'),
      run('error'),
      run('unknown'),
      run('composition_failed', { duration: null, start: null }),
    ])
    expect([all.passed, all.failed, all.other]).toEqual([1, 2, 2])
    expect(all.passed + all.failed + all.other).toBe(all.runs)
  })

  it('takes the best objective by the campaign direction', () => {
    const rows = [run('passed', { objective: 0.2 }), run('passed', { objective: 0.9 })]
    expect(summariseBatches(rows, true)[0].bestObjective).toBe(0.9)
    expect(summariseBatches(rows, false)[0].bestObjective).toBe(0.2)
  })

  it('medians only the runs that recorded a duration', () => {
    const [all] = summariseBatches([
      run('passed', { duration: 10 }),
      run('passed', { duration: 30 }),
      run('unknown', { duration: null }),
    ])
    expect(all.medianDuration).toBe(20)
    expect(summariseBatches([run('unknown', { duration: null })])[0].medianDuration).toBeNull()
  })
})

describe('totals', () => {
  it('sums durations as simulated time and counts distinct batches', () => {
    const rows = [
      run('passed', { duration: 60, batch: 0 }),
      run('passed', { duration: 120, batch: 1 }),
      run('unknown', { duration: null, batch: 1 }),
    ]
    const t = totals(rows, null)
    expect(t).toMatchObject({ runs: 3, batches: 2, simulatedSeconds: 180, cpuHours: null })
  })
})

describe('formatCpu', () => {
  it('prints the value that will be typed into the .vast, not a rounded one', () => {
    // format.ts's formatCores is fixed at one decimal, so it renders a suggested 0.75 as "0.8"
    // and a pod total of 4.25 as "4.3" -- numbers meant to be copied into a config.
    expect(formatCpu(0.75)).toBe('0.75')
    expect(formatCpu(4.25)).toBe('4.25')
    expect(formatCpu(2.5)).toBe('2.5')
    expect(formatCpu(1)).toBe('1')
  })
})

describe('cpuHeadline', () => {
  it('shows set → suggested, or suggested alone when nothing was declared', () => {
    const cpu = summariseCpu(
      [{ container: 'sut', p95: 2.2, peak: 2.4, ticks: 400 }],
      [{ fullkey: '$.execution.containers.sut.resources.cpu', value: 6 }],
    )
    expect(cpuHeadline(cpu)).toBe('6 → 2.75 cpu')
    expect(cpuHeadline(summariseCpu([{ container: 'sut', p95: 2.2, peak: 2.4, ticks: 400 }], []))).toBe(
      '2.75 cpu suggested',
    )
    expect(cpuHeadline(null)).toBeNull()
  })
})

describe('summariseActions', () => {
  const action = (verdict: string, name: string, runs: number) => ({
    verdict,
    action: name,
    ended_as: 'RUNNING',
    runs,
  })

  it('ranks by total runs, not by failures', () => {
    // An action ending 20 passing runs and 5 failing ones is the campaign's main path with a
    // defect; one ending 5 failing runs and nothing else is a wall. Ranking by failures alone
    // shows the second and hides the first, which is the more informative of the two.
    const points = summariseActions([
      action('passed', 'nav_to_pose', 20),
      action('failed', 'nav_to_pose', 5),
      action('failed', 'wait_for_data', 6),
    ])
    expect(points[0].action).toBe('nav_to_pose')
    expect(points.filter((p) => p.action === 'nav_to_pose').map((p) => p.verdict)).toEqual([
      'failed',
      'passed',
    ])
  })

  it('folds error into failed and everything else into other, as the batch rows do', () => {
    // Two places count verdicts; if they fold differently the panel contradicts itself in one
    // screen -- a row saying 2 failed beside a bar saying 1.
    const points = summariseActions([
      action('error', 'a', 1),
      action('failed', 'a', 1),
      action('unknown', 'a', 3),
    ])
    expect(points.find((p) => p.verdict === 'failed')?.runs).toBe(2)
    expect(points.find((p) => p.verdict === 'other')?.runs).toBe(3)
  })

  it('keeps at most the top five actions', () => {
    const rows = Array.from({ length: 9 }, (_, i) => action('passed', `a${i}`, 9 - i))
    expect(new Set(summariseActions(rows).map((p) => p.action)).size).toBe(TOP_ACTIONS)
  })
})

describe('outlierRuns', () => {
  const timed = (config: string, id: number, duration: number, status = 'passed'): RunRow => ({
    config_name: config,
    run_id: id,
    status,
    duration_s: duration,
    batch: 0,
    start_time: '2026-08-12T10:00:00+00:00',
    objective: null,
  })

  it('compares a run against its own configuration, never across the campaign', () => {
    // The slow cell's runs are not outliers: they are what that goal costs. Only the run that
    // deviates from ITS OWN siblings is.
    const found = outlierRuns([
      timed('near', 0, 10),
      timed('near', 1, 10),
      timed('near', 2, 30),
      timed('far', 0, 100),
      timed('far', 1, 100),
      timed('far', 2, 100),
    ])
    expect(found.map((r) => r.run)).toEqual(['near/2'])
    expect(found[0].median).toBe(10)
  })

  it('reports a run that finished far too early as well as one that dragged', () => {
    // A trial that gave up in seconds and one that ran long are the same question -- why did
    // this run not do what its siblings did -- and only one of them is slow.
    const found = outlierRuns([timed('c', 0, 100), timed('c', 1, 100), timed('c', 2, 5)])
    expect(found.map((r) => r.run)).toEqual(['c/2'])
    expect(found[0].ratio).toBeLessThan(1)
  })

  it('says nothing about a configuration with too few siblings to be unlike', () => {
    // Two runs have no majority to deviate from: the median is their mean, and each is exactly
    // as far from it as the other.
    expect(outlierRuns([timed('c', 0, 10), timed('c', 1, 100)])).toEqual([])
  })
})

describe('memory sizing', () => {
  it('rounds a suggestion up to 128Mi and writes it as a .vast would', () => {
    expect(formatMemQuantity(ceilToMemUnit(1))).toBe('128Mi')
    expect(formatMemQuantity(ceilToMemUnit(600 * 1024 ** 2))).toBe('640Mi')
    expect(formatMemQuantity(ceilToMemUnit(1024 ** 3))).toBe('1Gi')
    // Not `0.5Gi`: it parses, but nobody writes it, and Mi is exact for every 128Mi multiple.
    expect(formatMemQuantity(512 * 1024 ** 2)).toBe('512Mi')
  })

  it('sizes memory on the PEAK, not a percentile', () => {
    // Exceeding a cpu reservation throttles; exceeding a memory limit is an OOM kill and the run
    // is lost. Sizing memory on p95 would be choosing how often a run survives.
    const cpu = summariseCpu(
      [
        {
          container: 'sut', p95: 1, peak: 1.2, ticks: 400,
          // Chosen so the two rules land on DIFFERENT 128Mi steps: p95 x 1.25 = 500Mi -> 512Mi,
          // peak x 1.25 = 625Mi -> 640Mi. With a narrower gap both round to the same step and the
          // test would pass whichever rule was implemented.
          m50: 380 * 1024 ** 2, m95: 400 * 1024 ** 2, m_peak: 500 * 1024 ** 2,
          byte_seconds: 380 * 1024 ** 2 * 400,
        },
      ],
      [],
    )
    const mem = cpu?.containers[0].mem
    expect(mem?.suggested).toBe(640 * 1024 ** 2)
    expect(formatMemQuantity(mem?.suggested as number)).toBe('640Mi')
    // What sizing on p95 would have given, a step lower.
    expect(ceilToMemUnit(400 * 1024 ** 2 * MEM_HEADROOM)).toBe(512 * 1024 ** 2)
  })

  it('reads a declared memory quantity in either suffix family', () => {
    // 2G and 2Gi differ by 7%, so they are read as what they mean rather than as aliases.
    const cpu = summariseCpu(
      [
        { container: 'sut', p95: 1, peak: 1, ticks: 400, m_peak: 1024 ** 3, byte_seconds: 400 },
        { container: 'sim', p95: 1, peak: 1, ticks: 400, m_peak: 1024 ** 3, byte_seconds: 400 },
      ],
      [
        { fullkey: '$.execution.containers.sut.resources.memory', value: '2Gi' },
        { fullkey: '$.execution.containers.sim.resources.memory', value: '2G' },
      ],
    )
    const byName = new Map(cpu?.containers.map((c) => [c.container, c.mem.declared]))
    expect(byName.get('sut')).toBe(2 * 1024 ** 3)
    expect(byName.get('sim')).toBe(2e9)
  })

  it('reports the mean per container, which is what the ring is drawn from', () => {
    // The ring is a part-to-whole of what was USED. A median would answer "the typical tick" and
    // the shares would not add up to what the campaign consumed.
    const cpu = summariseCpu(
      [
        {
          container: 'sut', p95: 2, peak: 3, ticks: 100, core_seconds: 150,
          m_peak: 1024 ** 3, byte_seconds: 100 * 1024 ** 3,
        },
      ],
      [],
    )
    expect(cpu?.containers[0].meanCores).toBe(1.5)
    expect(cpu?.containers[0].mem.mean).toBe(1024 ** 3)
  })
})

describe('durationHistogram', () => {
  const timed = (duration: number, status = 'passed'): RunRow => ({
    config_name: 'c',
    run_id: 0,
    status,
    duration_s: duration,
    batch: 0,
    start_time: '2026-08-12T10:00:00+00:00',
    objective: null,
  })

  it('bins over the data range, not from zero', () => {
    // Six runs of about 100s binned from zero is one bar against eleven empty ones: honest, and
    // it shows nothing. The panel labels both ends, so the range is stated rather than assumed.
    const bins = durationHistogram([timed(80), timed(105), timed(130)], 5)
    expect(bins).toHaveLength(5)
    expect(bins[0].from).toBe(80)
    expect(bins[bins.length - 1].to).toBe(130)
  })

  it('never shows a range narrower than the minimum span', () => {
    // Runs agreeing to within 40ms would otherwise be spread across the full width as if that
    // were a distribution -- 24 bins of scheduling noise, drawn exactly like a real spread.
    const bins = durationHistogram([timed(100), timed(100.02), timed(100.04)])
    const width = bins[bins.length - 1].to - bins[0].from
    expect(width).toBe(HISTOGRAM_MIN_SPAN)
    // Centred on the data, so the spike sits in the middle rather than against an edge.
    expect(bins[0].from).toBeCloseTo(95.02, 1)
    expect(bins.reduce((n, b) => n + b.runs, 0)).toBe(3)
  })

  it('does not start the axis below zero for a very fast campaign', () => {
    // Centring a 10s span on 0.3s would put a quarter of the width on impossible durations.
    const bins = durationHistogram([timed(0.3), timed(0.31)])
    expect(bins[0].from).toBe(0)
    expect(bins[bins.length - 1].to).toBe(HISTOGRAM_MIN_SPAN)
    expect(bins[0].runs).toBe(2)
  })

  it('gives the top bin its upper bound, so the slowest run is counted', () => {
    // floor((max - min) / width) indexes one past the end for the maximum; without the clamp the
    // slowest run of every campaign silently vanishes.
    const bins = durationHistogram([timed(10), timed(20)], 4)
    expect(bins.reduce((n, b) => n + b.runs, 0)).toBe(2)
    expect(bins[bins.length - 1].runs).toBe(1)
  })

  it('splits each bin by verdict', () => {
    // A second mode made of failures is a timeout; the same mode made of passes is a slower path
    // through the scenario. A plain count cannot tell those apart.
    const [bin] = durationHistogram([timed(10), timed(10, 'failed'), timed(10, 'unknown')], 1)
    expect(bin).toMatchObject({ runs: 3, passed: 1, failed: 1, other: 1 })
  })

  it('survives a campaign whose runs all took the same time', () => {
    // A fixed-duration scenario is an ordinary campaign, and its zero span must not divide. The
    // minimum span carries it: one spike in the middle of a 10s axis, which is what happened.
    const bins = durationHistogram([timed(60), timed(60), timed(60)])
    expect(bins).toHaveLength(HISTOGRAM_BINS)
    expect(bins.reduce((n, b) => n + b.runs, 0)).toBe(3)
    expect(bins.filter((b) => b.runs).length).toBe(1)
  })

  it('ignores runs that recorded no duration', () => {
    expect(durationHistogram([timed(0), { ...timed(1), duration_s: null }])).toEqual([])
  })
})
