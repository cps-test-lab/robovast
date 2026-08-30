import { describe, expect, it } from 'vitest'
import { runMeterSegments, runMeterText, runsFromSummary } from './runMeter'
import type { CampaignSummary, JobCounts, Status } from './robovastClient'

const status = (runs: Partial<Status['runs']>): Status =>
  ({
    runs: { completed: 0, total: 0, failed: 0, no_result: 0, killed: 0, invalid: 0, ...runs },
    budget: [],
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
  it('counts a run that delivered nothing as done, so the label reaches 100%', () => {
    expect(runMeterText(status({ total: 40, completed: 27, no_result: 13 }))).toBe('100%')
  })

  it('never prints more than 100% while the two counters settle', () => {
    expect(runMeterText(status({ total: 40, completed: 40 }), counts({ failed: 1 }))).toBe('100%')
  })

  it('states the share done, rounded to a whole percent', () => {
    expect(runMeterText(status({ total: 40, completed: 27 }), counts({ running: 3 }))).toBe('68%')
  })

  it('says nothing with no denominator rather than claiming 0%', () => {
    expect(runMeterText(status({ total: 0 }))).toBe('')
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
    expect(runMeterText({ runs } as Status)).toBe('100%')
  })
})
