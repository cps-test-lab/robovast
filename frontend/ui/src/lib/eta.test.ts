import { describe, expect, it, vi, afterEach } from 'vitest'
import type { BudgetItem, JobCounts, Status } from './robovastClient'
import { batchesBudget, campaignEtaSeconds, estimateBatchesEtaSeconds, estimateEtaSeconds, finishedRuns, isBatchesBudget, isFractionableBudget, noResultRuns, ringBudget } from './eta'

const NOW = 1_700_000_000_000

/** A live status whose current batch started `elapsed` seconds ago. */
function status(runs: Partial<Status['runs']>, over: Partial<Status> = {}): Status {
  return {
    runs: { completed: 0, total: 40, no_result: 0, failed: 0, killed: 0, invalid: 0, ...runs },
    batch_since: NOW / 1000 - 600,
    batch: 7,
    ...over,
  } as Status
}

const counts = (over: Partial<JobCounts> = {}) =>
  ({ running: 0, pending: 0, waiting: 0, completed: 0, failed: 0, killed: 0,
     blocked: 0, total: 40, ...over }) as JobCounts

afterEach(() => vi.useRealTimers())
const freeze = () => vi.useFakeTimers({ now: NOW })

// The one number the runs line, the meter and the estimate all read. They disagreed
// before it existed: a job that died without delivering was terminal to the meter and
// still pending to the other two.
describe('finishedRuns', () => {
  it('counts dead jobs from the LIVE count while the batch runs', () => {
    // runs.no_result is still 0 here — it is only written when the batch closes — so
    // without the job counts these five runs would be waited for to the end of the batch.
    expect(finishedRuns(status({ completed: 27 }), counts({ failed: 5 }))).toBe(32)
  })

  it('falls back to the status counter once the batch has closed', () => {
    expect(finishedRuns(status({ completed: 27, no_result: 13 }))).toBe(40)
  })

  it('clamps to the batch total', () => {
    // The two sources settle a moment apart; that moment must not print 41/40.
    expect(finishedRuns(status({ completed: 40 }), counts({ failed: 1 }))).toBe(40)
  })

  it('prefers a live zero over a stale status count', () => {
    expect(noResultRuns(status({ no_result: 3 }), counts({ failed: 0 }))).toBe(0)
  })
})

describe('estimateEtaSeconds', () => {
  it('estimates for a search past its first batch', () => {
    freeze()
    // The regression this exists for: the old guard bailed out unless batch === 0, so a
    // search lost its estimate from the second round on.
    expect(estimateEtaSeconds(status({ completed: 20 }), undefined, false)).toBe(600)
  })

  it('is shorter when runs are failing than when they are ignored', () => {
    freeze()
    const s = status({ completed: 20 })
    const withFailures = estimateEtaSeconds(s, counts({ failed: 10 }), false)!
    const ignoringThem = estimateEtaSeconds(s, undefined, false)!
    expect(withFailures).toBeLessThan(ignoringThem)
    expect(withFailures).toBe(200) // 30 done in 600s, 10 left
  })

  it('declines to guess with no clock, no finished run, or a finished campaign', () => {
    freeze()
    expect(estimateEtaSeconds(status({ completed: 20 }), undefined, true)).toBeNull()
    expect(estimateEtaSeconds(status({ completed: 0 }), undefined, false)).toBeNull()
    expect(
      estimateEtaSeconds(status({ completed: 20 }, { batch_since: null }), undefined, false),
    ).toBeNull()
  })
})

const budget = (over: Partial<BudgetItem> = {}) =>
  ({ label: 'batches', kind: 'batches', current: 7, limit: 50, done: false, ...over }) as BudgetItem

describe('estimateBatchesEtaSeconds', () => {
  it('adds a projected batch for every round after the one in flight', () => {
    freeze()
    const s = status({ completed: 20 })
    const runsEta = estimateEtaSeconds(s, undefined, false)!
    // 30s per run × 40 runs = 1200s per batch; 50 - 7 - 1 = 42 rounds after this one.
    expect(estimateBatchesEtaSeconds(s, undefined, budget(), runsEta)).toBe(runsEta + 42 * 1200)
  })

  it('is just the current batch on the final round', () => {
    freeze()
    const s = status({ completed: 20 })
    const runsEta = estimateEtaSeconds(s, undefined, false)!
    expect(estimateBatchesEtaSeconds(s, undefined, budget({ current: 49 }), runsEta)).toBe(runsEta)
  })

  it('ignores a metric that merely calls itself batches', () => {
    freeze()
    const s = status({ completed: 20 })
    const runsEta = estimateEtaSeconds(s, undefined, false)!
    // Why the estimate keys on `kind` and not on the printed label.
    const impostor = budget({ label: 'batches', kind: 'metric' })
    expect(estimateBatchesEtaSeconds(s, undefined, impostor, runsEta)).toBeNull()
  })

  it('says nothing when the batch itself cannot be estimated', () => {
    freeze()
    expect(estimateBatchesEtaSeconds(status({ completed: 0 }), undefined, budget(), null)).toBeNull()
  })
})

describe('isBatchesBudget', () => {
  const row = (over: Partial<BudgetItem>): BudgetItem =>
    ({ label: 'batches', current: 1, limit: 10, done: false, ...over }) as BudgetItem

  it('reads the batch counter off `kind`', () => {
    expect(isBatchesBudget(row({ kind: 'batches' }))).toBe(true)
  })

  it('refuses a metric somebody named `batches` when kind says otherwise', () => {
    // The whole reason this is not a label match: `label` is the user's own metric or
    // objective name for every criterion except batches and time.
    expect(isBatchesBudget(row({ label: 'batches', kind: 'metric' }))).toBe(false)
  })

  it('falls back to the label when kind is absent', () => {
    // `kind` is younger than the campaigns that have to render: a finished campaign replays
    // the budget its controller recorded, so everything that ran before the field shipped
    // reports null forever. Without this those cards lose the ETA and the objective chart.
    expect(isBatchesBudget(row({ kind: null }))).toBe(true)
    expect(isBatchesBudget(row({ label: 'coverage', kind: null }))).toBe(false)
  })
})

describe('batchesBudget', () => {
  const search = (budget: Partial<BudgetItem>[]): Status =>
    ({ mode: 'search', batches_done: 4, budget } as Status)

  it('finds the criterion that bounds the rounds', () => {
    const b = { label: 'batches', current: 3, limit: 6, done: false, kind: 'batches' }
    expect(batchesBudget(search([{ label: 'time', kind: 'time' }, b]))?.limit).toBe(6)
  })

  it('says nothing bounds them when the search is bounded by runs', () => {
    // The case that had no batch counter, no estimate and no objective chart at all. The
    // card must still render the rounds — `nav_search_halton` is bounded by runs on purpose,
    // and six of the eight shipped nav_search examples are bounded by something other than
    // batches, so this is the common case rather than the edge one.
    expect(batchesBudget(search([{ label: 'runs', current: 24, limit: 144, kind: 'runs' }]))).toBeNull()
  })

  it('is null for a search that declared no budget at all', () => {
    expect(batchesBudget(search([]))).toBeNull()
  })

  it('refuses a metric the user happened to name `batches`', () => {
    // Same reason isBatchesBudget keys on `kind`: every label except batches and time is the
    // user's own metric or objective name, so a text match would put the objective chart on a
    // metric row and read that metric's value as a round count.
    expect(batchesBudget(search([{ label: 'batches', kind: 'metric' }]))).toBeNull()
  })
})

// The estimate a collapsed running row prints. Everything else in this module is scoped to the
// current BATCH; this is the only function that answers for the campaign, and most of its job is
// knowing when it cannot.
describe('campaignEtaSeconds', () => {
  // perRun = 600s elapsed / 20 finished = 30s; the current batch has 20 of 40 left => 600s.
  const search = (budget: BudgetItem[]) =>
    status({ completed: 20 }, { mode: 'search', budget })
  const batches = (current: number, limit: number): BudgetItem =>
    ({ label: 'batches', kind: 'batches', current, limit, done: false }) as BudgetItem
  const runs = (current: number, limit: number): BudgetItem =>
    ({ label: 'runs', kind: 'runs', current, limit, done: false }) as BudgetItem
  const stale: BudgetItem =
    ({ label: 'stale_batches', kind: 'no_improvement', current: 1, limit: 3, done: false }) as BudgetItem

  it('is the batch estimate for a batch campaign — it has exactly one batch', () => {
    freeze()
    const s = status({ completed: 20 }, { mode: 'batch' })
    expect(campaignEtaSeconds(s, undefined, false)).toBe(estimateEtaSeconds(s, undefined, false))
  })

  it('projects the rounds still to come when batches bound the search', () => {
    freeze()
    // 600s to finish this round, then 2 whole rounds of 40 runs at 30s each.
    expect(campaignEtaSeconds(search([batches(1, 4)]), undefined, false)).toBe(600 + 2 * 40 * 30)
  })

  it('projects the runs still to come when RUNS bound the search', () => {
    freeze()
    // 60 campaign runs left at 30s each — not this round's 600s, which describes 20 runs.
    expect(campaignEtaSeconds(search([runs(90, 150)]), undefined, false)).toBe(60 * 30)
  })

  it('takes the smaller when both bound it — the campaign stops at whichever fires first', () => {
    freeze()
    const both = campaignEtaSeconds(search([batches(1, 4), runs(90, 150)]), undefined, false)
    expect(both).toBe(Math.min(600 + 2 * 40 * 30, 60 * 30))
    expect(both).toBe(60 * 30)
  })

  it('never reports the current round as the campaign when nothing bounds the work', () => {
    freeze()
    // `no_improvement` fires on a value nothing can project, so it converts to no time at all.
    // The batch estimate exists and is deliberately NOT used: a search on its sixth round would
    // otherwise claim the same remaining time as one on its first.
    const s = search([stale])
    expect(estimateEtaSeconds(s, undefined, false)).not.toBeNull()
    expect(campaignEtaSeconds(s, undefined, false)).toBeNull()
  })

  it('says nothing before the first run finishes, and nothing once terminal', () => {
    freeze()
    expect(campaignEtaSeconds(status({ completed: 0 }, { mode: 'batch' }), undefined, false)).toBeNull()
    expect(campaignEtaSeconds(status({ completed: 20 }, { mode: 'batch' }), undefined, true)).toBeNull()
    expect(campaignEtaSeconds(search([runs(90, 150)]), undefined, true)).toBeNull()
  })
})

const crit = (kind: string, current: number | null, limit: number, over: Partial<BudgetItem> = {}) =>
  ({ kind, label: kind, current, limit, done: false, ...over }) as BudgetItem

// What the rounds ring measures. The whole point is that it is NOT the `batches` row: hanging it
// there left every search bounded by runs, time or evaluations with an unfilled circle.
describe('ringBudget', () => {
  const of = (...budget: BudgetItem[]) => ringBudget(status({}, { budget }))

  it('draws each of the four budget kinds', () => {
    // The `search.budget` vocabulary in full — these are monotone resource caps, so `current/limit`
    // is a share of something.
    expect(of(crit('runs', 120, 180))?.share).toBeCloseTo(120 / 180)
    expect(of(crit('batches', 4, 10))?.share).toBeCloseTo(0.4)
    expect(of(crit('evaluations', 40, 200))?.share).toBeCloseTo(0.2)
    expect(of(crit('time', 1800, 3600))?.share).toBeCloseTo(0.5)
  })

  it('refuses every stopping kind, at any position', () => {
    // target_objective: `current` is the objective, so the quotient is not a share and is
    // direction-dependent. A minimize search on a negative objective makes that obvious.
    expect(of(crit('target_objective', -1.42, -2))).toBeNull()
    // no_improvement: resets to 0 on an improvement, so an arc driven by it runs backwards.
    expect(of(crit('no_improvement', 2, 3, { label: 'stale_batches' }))).toBeNull()
    // metric: the `op` deciding which side satisfies is not on the wire. This row is SATISFIED and
    // would have drawn as 12%.
    expect(of(crit('metric', 0.1, 0.8, { label: 'coverage' }))).toBeNull()
  })

  it('picks the criterion closest to exhausting, since that is the one that fires', () => {
    // 5/6 batches beats 120/180 runs: the campaign ends on the batches row, and the runs row
    // describes a moment it will never reach.
    expect(of(crit('runs', 120, 180), crit('batches', 5, 6))?.item.kind).toBe('batches')
    expect(of(crit('runs', 170, 180), crit('batches', 1, 6))?.item.kind).toBe('runs')
  })

  it('ignores a stopping row even when it would be the largest fraction', () => {
    // The max is taken over the fractionable rows only — otherwise a satisfied metric at 40/1
    // would win and drive the arc.
    expect(of(crit('runs', 120, 180), crit('metric', 40, 1))?.item.kind).toBe('runs')
  })

  it('is null when only stopping criteria bound the search', () => {
    // Legal: validation requires one criterion across budget AND stopping together. The ring draws
    // its bare track rather than inventing a denominator.
    expect(of(crit('no_improvement', 1, 3), crit('target_objective', 0.4, 0.9))).toBeNull()
    expect(ringBudget(status({}, { budget: [] }))).toBeNull()
  })

  it('clamps past-cap counts to 1', () => {
    // A `runs` budget counts what each batch ASKS FOR, before it runs, so the last batch can carry
    // the count past the cap. 103% of a ring is a dash array that wraps.
    expect(of(crit('runs', 184, 180))?.share).toBe(1)
  })

  it('skips a row with no position yet rather than drawing it at zero', () => {
    // NaN reaches the wire as null (see controller._budget_item). Drawing 0% would claim the
    // campaign has spent nothing, which is a different statement from "not known yet".
    expect(of(crit('runs', null, 180))).toBeNull()
    expect(of(crit('runs', null, 180), crit('batches', 2, 10))?.item.kind).toBe('batches')
  })

  it('reads a pre-kind status by label, for the two kinds whose label IS the type', () => {
    // `kind` is younger than the campaigns that have to render, and a finished campaign replays the
    // budget its controller wrote at the time — so these report null forever.
    expect(isFractionableBudget(crit('batches', 4, 10, { kind: null }))).toBe(true)
    expect(isFractionableBudget(crit('time', 60, 600, { kind: null }))).toBe(true)
    // A legacy `runs` row is indistinguishable from a user metric named `runs`, so it is refused
    // rather than guessed at — the same trade isBatchesBudget makes.
    expect(isFractionableBudget(crit('runs', 120, 180, { kind: null }))).toBe(false)
    expect(isFractionableBudget(crit('metric', 2, 3, { kind: null, label: 'coverage' }))).toBe(false)
  })
})
