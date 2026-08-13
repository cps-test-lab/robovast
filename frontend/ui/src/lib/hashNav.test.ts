// The hash grammar and, above all, the two campaign scopes' opposite memory rules. Tested here
// rather than through the UI because a mistake in `configCampaignId` does not misplace a view — it
// *exposes* one. A campaign's frozen config is reachable only by the link on its card, and the whole
// of that guarantee is `nextNav` dropping the field; nothing about the rendered page would look
// wrong if it stopped.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { CAMPAIGN_SEGMENT, hashFor, navFromHash, nextNav, type Nav } from './hashNav'

const TOPICS = [
  { id: 'config' },
  { id: 'execution' },
  { id: 'results', views: [{ id: 'explorer' }, { id: 'run' }, { id: 'data' }] },
]

const DEFAULT_NAV: Nav = {
  topicId: 'execution',
  viewId: '',
  campaignId: '',
  configCampaignId: '',
}

const at = (hash: string) => navFromHash(hash, TOPICS, DEFAULT_NAV)

describe('navFromHash', () => {
  it('falls back to the default for an empty or unknown topic', () => {
    expect(at('')).toEqual(DEFAULT_NAV)
    expect(at('#/nope')).toEqual(DEFAULT_NAV)
  })

  it('reads a leaf topic with no scope', () => {
    expect(at('#/config')).toEqual({
      topicId: 'config',
      viewId: '',
      campaignId: '',
      configCampaignId: '',
    })
  })

  it('reads the campaign-config deep link', () => {
    expect(at(`#/config/${CAMPAIGN_SEGMENT}/nav-2026-08-12`).configCampaignId)
      .toBe('nav-2026-08-12')
  })

  it('leaves a stale sub-view bookmark on plain Config', () => {
    // `#/config/files` was a real URL before the Editor/Files split became an in-page tab bar.
    // Reading its second segment as a campaign id would ask for a campaign named `files`.
    expect(at('#/config/files')).toEqual({
      topicId: 'config',
      viewId: '',
      campaignId: '',
      configCampaignId: '',
    })
  })

  it('reads a topic with views, defaulting the view and taking the campaign verbatim', () => {
    expect(at('#/results')).toMatchObject({ viewId: 'explorer', campaignId: '' })
    expect(at('#/results/run/nav-2026-08-12')).toMatchObject({
      topicId: 'results',
      viewId: 'run',
      campaignId: 'nav-2026-08-12',
      configCampaignId: '',
    })
    // An unknown view falls back to the topic's first, rather than rejecting the hash.
    expect(at('#/results/nope').viewId).toBe('explorer')
  })
})

describe('hashFor', () => {
  it('round-trips every form', () => {
    for (const hash of [
      '#/config',
      `#/config/${CAMPAIGN_SEGMENT}/nav-2026-08-12`,
      '#/execution',
      '#/results/explorer',
      '#/results/run/nav-2026-08-12',
    ]) {
      expect(`#${hashFor(at(hash))}`).toBe(hash)
    }
  })
})

describe('nextNav — which campaign survives a navigation', () => {
  const withResults: Nav = {
    topicId: 'results',
    viewId: 'run',
    campaignId: 'c1',
    configCampaignId: '',
  }
  const withConfig: Nav = {
    topicId: 'config',
    viewId: '',
    campaignId: '',
    configCampaignId: 'c1',
  }

  it('carries the results campaign across topics', () => {
    // A change of lens on one campaign, not a request for a different one.
    expect(nextNav(withResults, TOPICS, 'results', 'data').campaignId).toBe('c1')
    expect(nextNav(withResults, TOPICS, 'execution').campaignId).toBe('c1')
  })

  it('never carries the results campaign into the config scope', () => {
    expect(nextNav(withResults, TOPICS, 'config').configCampaignId).toBe('')
  })

  it('drops the config campaign when Config is selected in the sidebar', () => {
    // The click means "my workspaces". Keeping the id here would reopen a campaign's read-only
    // config from an ordinary sidebar click, which is exactly what must not happen.
    expect(nextNav(withConfig, TOPICS, 'config').configCampaignId).toBe('')
  })

  it('never turns a config campaign into a results campaign', () => {
    expect(nextNav(withConfig, TOPICS, 'results', 'explorer')).toMatchObject({
      campaignId: '',
      configCampaignId: '',
    })
  })
})
