// Which campaigns the Results topic (Explorer / Run / Data) will show: those with recorded runs,
// whatever their phase. A run's tables are built from its recording as it grows and `run_view`
// answers while the campaign runs, so neither the end of the campaign nor its postprocessing is
// a reason to withhold it.
import { describe, expect, it } from 'vitest'

import { hasResults, type CampaignSummary } from './robovastClient'

const summary = (over: Partial<CampaignSummary>): CampaignSummary =>
  ({
    campaign_id: 'c1', phase: 'finished', postprocessed: true,
    num_runs: 4, num_passed: 4, num_failed: 0,
    ...over,
  }) as CampaignSummary

describe('hasResults admits every campaign with recorded runs', () => {
  it.each(['running', 'finishing', 'postprocessing', 'finished', 'stopped', 'crashed', 'failed'])(
    'admits %s with runs, postprocessed or not', (phase) => {
      expect(hasResults(summary({ phase }))).toBe(true)
      expect(hasResults(summary({ phase, postprocessed: false }))).toBe(true)
    })

  it('withholds a campaign that has recorded nothing', () => {
    // No store to read: the campaign never started, or ended before writing one.
    expect(hasResults(summary({ num_runs: 0 }))).toBe(false)
    expect(hasResults(summary({ phase: 'building', num_runs: 0 }))).toBe(false)
  })

  it('admits a campaign whose trials are running before their first verdict', () => {
    // A run directory exists from the moment a trial starts; run_view lists it live.
    expect(hasResults(summary({ phase: 'running', num_runs: 0 }))).toBe(true)
    expect(hasResults(summary({ phase: 'finishing', num_runs: 0 }))).toBe(true)
  })

  it('admits a search whose every draw failed to compose', () => {
    // Zero runs, and yet fully recorded: the one campaign most in need of inspection.
    expect(hasResults(summary({ num_runs: 0, num_composition_failed: 3 }))).toBe(true)
  })
})
