import { describe, expect, it } from 'vitest'
import {
  CAMPAIGN_SORT_CHOICES,
  campaignSortFromQuery,
  campaignSortKey,
  campaignSortQuery,
  DEFAULT_CAMPAIGN_SORT,
} from './campaignSort'

const q = (s: string) => new URLSearchParams(s)

describe('the campaign list order on the wire', () => {
  it('spells nothing for the default, so the default address is the plain one', () => {
    expect(campaignSortQuery(DEFAULT_CAMPAIGN_SORT)).toBe('')
    expect(campaignSortQuery({ sort: 'size', order: 'asc' })).toBe('sort=size&order=asc')
  })

  it('reads back every order it offers', () => {
    for (const { sort } of CAMPAIGN_SORT_CHOICES) {
      expect(campaignSortFromQuery(q(campaignSortQuery(sort)))).toEqual(sort)
    }
  })

  it('refuses a value outside the vocabulary rather than guessing', () => {
    expect(campaignSortFromQuery(q('sort=name'))).toBeNull()
    expect(campaignSortFromQuery(q('sort=size&order=up'))).toBeNull()
  })

  it('offers each order once', () => {
    const keys = CAMPAIGN_SORT_CHOICES.map((c) => campaignSortKey(c.sort))
    expect(new Set(keys).size).toBe(4)
  })
})
