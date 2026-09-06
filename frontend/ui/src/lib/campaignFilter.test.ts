// What the campaign list's search box narrows on — and, just as much, what it does not.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { NO_CAMPAIGN_FILTER, campaignFilterIsEmpty, matchCampaigns } from './campaignFilter'
import type { CampaignSummary } from './robovastClient'

const campaign = (c: Partial<CampaignSummary>): CampaignSummary =>
  ({ campaign_id: 'c1', description: '', created_by: '', phase: 'running', ...c } as CampaignSummary)

const rows = [
  campaign({ campaign_id: 'tour-20260901-1200', description: 'Warehouse waypoint tour', created_by: 'ada' }),
  campaign({ campaign_id: 'nav-20260830-0900', description: 'Nav2 clearance sweep', created_by: 'lin' }),
  campaign({ campaign_id: 'nav-20260831-1500', description: 'Nav2 clearance sweep', created_by: 'ada', phase: 'failed' }),
]

const ids = (cs: CampaignSummary[]) => cs.map((c) => c.campaign_id)

describe('matchCampaigns', () => {
  it('hands back the very same array when nothing is typed, so no card re-renders', () => {
    expect(matchCampaigns(rows, NO_CAMPAIGN_FILTER)).toBe(rows)
    expect(matchCampaigns(rows, { text: '   ' })).toBe(rows)
  })

  it('matches an id fragment, case-insensitively', () => {
    expect(ids(matchCampaigns(rows, { text: 'NAV-2026' }))).toEqual([
      'nav-20260830-0900',
      'nav-20260831-1500',
    ])
  })

  it('matches the description and who launched it, not just the id', () => {
    expect(ids(matchCampaigns(rows, { text: 'clearance' }))).toHaveLength(2)
    expect(ids(matchCampaigns(rows, { text: 'lin' }))).toEqual(['nav-20260830-0900'])
  })

  it('narrows with each word, wherever in the row each one sits', () => {
    expect(ids(matchCampaigns(rows, { text: 'clearance ada' }))).toEqual(['nav-20260831-1500'])
  })

  it('leaves the phase to a control of its own — a typed word never means a phase', () => {
    expect(matchCampaigns(rows, { text: 'failed' })).toEqual([])
  })

  it('keeps the order it was given', () => {
    expect(ids(matchCampaigns(rows, { text: 'a' }))).toEqual(ids(rows.filter((r) =>
      `${r.campaign_id}${r.description}${r.created_by}`.toLowerCase().includes('a'))))
  })
})

describe('campaignFilterIsEmpty', () => {
  it('reads whitespace as nothing typed', () => {
    expect(campaignFilterIsEmpty({ text: ' \t ' })).toBe(true)
    expect(campaignFilterIsEmpty({ text: 'x' })).toBe(false)
  })
})
