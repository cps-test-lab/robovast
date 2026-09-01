import { describe, expect, it } from 'vitest'
import { runMeterFailed, runMeterFailedText, runMeterSegments, runMeterText, runsFromSummary } from './runMeter'
import type { CampaignSummary, JobCounts, Status } from './robovastClient'

const status = (runs: Partial<Status['runs']>, phase?: string): Status =>
  ({
    runs: { completed: 0, total: 0, failed: 0, no_result: 0, killed: 0, invalid: 0, ...runs },
    budget: [],
    phase,
  } as unknown as Status)

const counts = (c: Partial<JobCounts>): JobCounts =>
  ({ running: 0, pending: 0, waiting: 0, failed: 0, blocked: 0, completed: 0, killed: 0, total: 0, ...c } as JobCounts)

const summary = (s: Partial<CampaignSummary>): CampaignSummary =>
  ({ num_runs: 0, num_passed: 0, num_failed: 0, num_no_sample: 0, num_composition_failed: 0, ...s } as CampaignSummary)

describe('runMeterSegments', () => {
  it('is empty when nothing is planned, so the track paints bare', () => {
    expect(runMeterSegments(status({ total: 0, completed: 0 }))).toEqual([])
  })

  it('paints successes green — completed minus the failing verdicts inside it', () => {
    const [green, red] = runMeterSegments(status({ total: 10, completed: 10, failed: 4 }))
    expect(green).toMatchObject({ fraction: 0.6, color: 'success.main' })
    expect(red).toMatchObject({ fraction: 0.4, color: 'error.main' })
  })

  it('keeps the two failure axes apart: a failing verdict is solid, no result is dim', () => {
    const segments = runMeterSegments(status({ total: 10, completed: 6, failed: 2, no_result: 4 }))
    expect(segments[1]).toMatchObject({ fraction: 0.2, color: 'error.main' })
    expect(segments[1].opacity).toBeUndefined()
    expect(segments[2]).toMatchObject({ fraction: 0.4, color: 'error.main', opacity: 0.45 })
  })

  it('prefers the live job failures over the batch-end no_result while runs are in flight', () => {
    const segments = runMeterSegments(status({ total: 10, completed: 4, no_result: 0 }), counts({ failed: 3 }))
    expect(segments[2].fraction).toBe(0.3)
  })

  it('puts running last and stripes it, so a transient overflow clips the least final part', () => {
    const segments = runMeterSegments(status({ total: 10, completed: 4 }), counts({ running: 2 }))
    expect(segments.at(-1)).toMatchObject({ fraction: 0.2, color: 'info.main', striped: true })
  })
})

describe('runMeterText', () => {
  it('counts a run that delivered nothing as done, so the share reaches 100%', () => {
    expect(runMeterText(status({ total: 40, completed: 27, no_result: 13 }))).toBe('100.0%')
  })

  it('never states more than 100% while the two counters settle', () => {
    expect(runMeterText(status({ total: 40, completed: 40 }), counts({ failed: 1 }))).toBe('100.0%')
  })

  it('states the share done to a tenth, so a long campaign visibly moves', () => {
    // 31/300 = 10.333%. Deliberately not a value that lands on a rounding boundary: with a
    // total of 400 every share is a whole quarter-percent, so such an example asserts the
    // rounding MODE rather than the one-decimal format this test is about.
    expect(runMeterText(status({ total: 300, completed: 31 }))).toBe('10.3%')
  })

  it('rounds a half up, like every other percentage in the app', () => {
    // 41/400 = 10.25% exactly, and `toFixed` rounds half away from zero. Pinned because the
    // boundary is easy to land on by accident and easy to guess wrong about.
    expect(runMeterText(status({ total: 400, completed: 41 }))).toBe('10.3%')
  })

  it('says nothing with no denominator rather than claiming 0%', () => {
    expect(runMeterText(status({ total: 0 }))).toBe('')
  })

  it('drops the share once the campaign is over and states its size', () => {
    expect(runMeterText(status({ total: 40, completed: 38, failed: 3 }, 'finished'))).toBe('40')
  })

  it('reports a stopped campaign the same way -- terminal is terminal', () => {
    expect(runMeterText(status({ total: 40, completed: 12 }, 'stopped'))).toBe('40')
  })
})

describe('runMeterFailed', () => {
  it('sums both reds, so the label matches what the bar paints', () => {
    expect(runMeterFailed(status({ total: 10, completed: 6, failed: 2, no_result: 4 }))).toBe(6)
  })

  it('is zero on a clean campaign, which is how the label stays off', () => {
    expect(runMeterFailed(status({ total: 10, completed: 10 }))).toBe(0)
  })

  it('prefers the live job counts for the no-result axis', () => {
    expect(runMeterFailed(status({ total: 10, completed: 5, failed: 1, no_result: 9 }), counts({ failed: 2 }))).toBe(3)
  })
})

describe('runMeterFailedText', () => {
  it('states a running campaign\'s failures as a share, in the label\'s own unit', () => {
    // 5 of 156, beside a label reading 28.3%: both percentages of the same campaign.
    expect(runMeterFailedText(status({ total: 156, completed: 44, failed: 5 }, 'running'))).toBe('3.2% \u2717')
  })

  it('measures that share against the whole campaign, so it equals the red on the bar', () => {
    const failedShare = runMeterSegments(status({ total: 200, completed: 50, failed: 20 }, 'running'))
      .filter((s) => s.color === 'error.main')
      .reduce((sum, s) => sum + s.fraction, 0)
    expect(runMeterFailedText(status({ total: 200, completed: 50, failed: 20 }, 'running'))).toBe(
      `${(failedShare * 100).toFixed(1)}% \u2717`,
    )
  })

  it('never rounds a real failure away to 0.0%', () => {
    expect(runMeterFailedText(status({ total: 5000, completed: 100, failed: 1 }, 'running'))).toBe('<0.1% \u2717')
  })

  it('counts runs once the campaign is over, because the label beside it is a count too', () => {
    expect(runMeterFailedText(status({ total: 40, completed: 40, failed: 5 }, 'completed'))).toBe('5 failed')
  })

  it('falls back to the count while there is no denominator to take a share of', () => {
    expect(runMeterFailedText(status({ total: 0, completed: 0, failed: 2 }, 'running'))).toBe('2 failed')
  })

  it('is empty on a clean campaign, so the label carries no reassuring zero', () => {
    expect(runMeterFailedText(status({ total: 10, completed: 4 }, 'running'))).toBe('')
  })

  it('includes the no-result axis, like the number it formats', () => {
    expect(runMeterFailedText(status({ total: 100, completed: 20, failed: 2, no_result: 3 }, 'running'))).toBe(
      '5.0% \u2717',
    )
  })
})

describe('runsFromSummary', () => {
  it('maps the listing tallies onto the meter axes', () => {
    expect(runsFromSummary(summary({ num_runs: 210, num_passed: 190, num_failed: 10 }))).toMatchObject({
      completed: 200, total: 210, failed: 10, no_result: 10,
    })
  })

  it('reads a stopped campaign as complete — the listing has no planned total', () => {
    // Documented limit of the first paint, asserted so it stays a known one: `num_runs` counts
    // runs that happened. The live status carries the planned total and supersedes this.
    const runs = runsFromSummary(summary({ num_runs: 41, num_passed: 41 }))
    expect(runs.total).toBe(41)
    expect(runMeterText({ runs } as Status)).toBe('100.0%')
  })
})
