// Copyright (C) 2026 Frederik Pasch
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Which campaigns the Results topic (Explorer / Run / Data) will show.
 *
 * The gate asks one question — did this campaign END with its derived data — and the phases
 * that answer yes are not only `finished`. A stopped campaign's completed runs are on disk and
 * are postprocessed like any other's, but it can never report `finished`: `record_step_outcome`
 * deliberately preserves `stopped` across a re-postprocess. Gating on `finished` therefore hid
 * exactly the campaigns whose partial results someone had a reason to open.
 */

import { describe, expect, it } from 'vitest'

import { hasResults, type CampaignSummary } from './robovastClient'

const summary = (over: Partial<CampaignSummary>): CampaignSummary =>
  ({
    campaign_id: 'c1', phase: 'finished', postprocessed: true,
    num_runs: 4, num_passed: 4, num_failed: 0,
    ...over,
  }) as CampaignSummary

describe('hasResults admits every campaign that ended with derived data', () => {
  it.each(['finished', 'stopped', 'crashed'])('admits %s once postprocessed', (phase) => {
    expect(hasResults(summary({ phase }))).toBe(true)
  })

  it.each(['finished', 'stopped', 'crashed'])('withholds %s until postprocessed', (phase) => {
    // The end is reached before postprocessing chains, and a campaign that defines none never
    // grows the data these views query.
    expect(hasResults(summary({ phase, postprocessed: false }))).toBe(false)
  })

  it('never admits a failed campaign, postprocessed or not', () => {
    // Not tidiness — a failed campaign never finished projecting its results, so its root is
    // missing pieces postprocessing needs, which is why the controller skips it by design.
    expect(hasResults(summary({ phase: 'failed' }))).toBe(false)
    expect(hasResults(summary({ phase: 'failed', postprocessed: false }))).toBe(false)
  })

  it.each(['running', 'finishing', 'postprocessing', 'building', 'initializing'])(
    'never admits %s, which is still live', (phase) => {
      expect(hasResults(summary({ phase }))).toBe(false)
    })
})
