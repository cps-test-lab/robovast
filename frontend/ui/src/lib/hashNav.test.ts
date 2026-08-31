// The hash grammar: what a URL addresses, and above all the two campaign scopes' opposite memory
// rules. Tested here rather than through the UI because a mistake in `configCampaignId` does not
// misplace a view — it *exposes* one. A campaign's frozen config is reachable only by the link on
// its card, and the whole of that guarantee is `nextNav` dropping the field; nothing about the
// rendered page would look wrong if it stopped.
//
// The node grammar earns tests for the neighbouring reason: a link to a result is a promise that
// the same URL comes back to the same run, so every form has to survive a round trip, and a URL
// naming something impossible (a run with no config, a batch beside one) must resolve to a real
// node rather than to a plausible wrong one.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import {
  CAMPAIGN_SEGMENT,
  CAMPAIGN_SEL,
  LOG_TAB_SLUG,
  hashFor,
  navFromHash,
  nextNav,
  type Nav,
  type ResultsSel,
} from './hashNav'

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
  shareImport: '',
  openCampaign: '',
  sel: CAMPAIGN_SEL,
  tab: '',
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
      shareImport: '',
      openCampaign: '',
      sel: CAMPAIGN_SEL,
      tab: '',
    })
  })

  it('reads the share-import deep link', () => {
    expect(at('#/execution?import=nav-2026-08-12').shareImport).toBe('nav-2026-08-12')
    expect(at('#/execution?import=my%20campaign').shareImport).toBe('my campaign')
  })

  it('refuses a share-import request addressed to another topic', () => {
    // Not fastidiousness: every page stays mounted (App's KeepAlive), so a `shareImport` set
    // while the hash names Config would have the campaign view open a dialog over a page
    // nobody is looking at. `tab` is parsed unconditionally because a tab a view ignores is
    // inert; this one is not.
    expect(at('#/config?import=nav-2026-08-12').shareImport).toBe('')
    expect(at('#/results/explorer/nav-1?import=nav-2026-08-12').shareImport).toBe('')
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
      shareImport: '',
      openCampaign: '',
      sel: CAMPAIGN_SEL,
      tab: '',
    })
  })

  it('reads a topic with views, defaulting the view and taking the campaign verbatim', () => {
    expect(at('#/results')).toMatchObject({ viewId: 'explorer', campaignId: '' })
    expect(at('#/results/run/nav-2026-08-12')).toMatchObject({
      topicId: 'results',
      viewId: 'run',
      campaignId: 'nav-2026-08-12',
      configCampaignId: '',
      shareImport: '',
      openCampaign: '',
    })
    // An unknown view falls back to the topic's first, rather than rejecting the hash.
    expect(at('#/results/nope').viewId).toBe('explorer')
  })
})

describe('navFromHash — which node of the campaign', () => {
  const C = '#/results/explorer/nav-2026-08-12'

  it('reads the four levels', () => {
    expect(at(C).sel).toEqual({ level: 'campaign' })
    expect(at(`${C}?batch=3`).sel).toEqual({ level: 'batch', batch: 3 })
    expect(at(`${C}/goal-1`).sel).toEqual({ level: 'config', configName: 'goal-1' })
    expect(at(`${C}/goal-1/2`).sel)
      .toEqual({ level: 'run', configName: 'goal-1', runId: 2 })
  })

  it('percent-decodes a config name', () => {
    expect(at(`${C}/goal%201`).sel).toEqual({ level: 'config', configName: 'goal 1' })
  })

  it('reads a malformed escape as written rather than throwing', () => {
    // A hash is user-editable text; one bad byte must not take the page down.
    expect(at(`${C}/goal%zz`).sel).toEqual({ level: 'config', configName: 'goal%zz' })
  })

  it('takes a run index only when it is one', () => {
    // `parseInt` would read `2x` as 2 and invent a run the campaign does not have.
    expect(at(`${C}/goal-1/2x`).sel).toEqual({ level: 'config', configName: 'goal-1' })
    expect(at(`${C}/goal-1/-1`).sel).toEqual({ level: 'config', configName: 'goal-1' })
    expect(at(`${C}/goal-1/0`).sel)
      .toEqual({ level: 'run', configName: 'goal-1', runId: 0 })
  })

  it('ignores a batch named beside a config', () => {
    // The two are different levels of one tree, and a config already implies its round — the
    // union cannot hold both, so the config wins and the index is dropped.
    expect(at(`${C}/goal-1?batch=3`).sel).toEqual({ level: 'config', configName: 'goal-1' })
  })

  it('ignores a batch that is not an index, and unknown query keys', () => {
    expect(at(`${C}?batch=later`).sel).toEqual({ level: 'campaign' })
    expect(at(`${C}?zoom=4`).sel).toEqual({ level: 'campaign' })
    expect(at(`${C}?zoom=4`).tab).toBe('')
  })

  it('ignores segments past the run', () => {
    expect(at(`${C}/goal-1/2/nonsense`).sel)
      .toEqual({ level: 'run', configName: 'goal-1', runId: 2 })
  })

  it('reads the open tab, including the built-in log', () => {
    expect(at(`${C}/goal-1/2?tab=nav_report`).tab).toBe('nav_report')
    expect(at(`${C}/goal-1/2?tab=${LOG_TAB_SLUG}`).tab).toBe(LOG_TAB_SLUG)
  })
})

describe('hashFor — what each view is allowed to spell', () => {
  const run = { level: 'run', configName: 'goal-1', runId: 2 } as const
  const nav = (viewId: string, sel: ResultsSel, tab = ''): Nav => ({
    topicId: 'results',
    viewId,
    campaignId: 'c1',
    sel,
    tab,
    configCampaignId: '',
    shareImport: '',
    openCampaign: '',
  })

  it('gives the Explorer the whole selection and its tab', () => {
    expect(hashFor(nav('explorer', run, 'nav_report')))
      .toBe('/results/explorer/c1/goal-1/2?tab=nav_report')
    expect(hashFor(nav('explorer', { level: 'batch', batch: 3 }))).toBe('/results/explorer/c1?batch=3')
  })

  it('gives the Run view a run, and nothing it cannot replay', () => {
    expect(hashFor(nav('run', run))).toBe('/results/run/c1/goal-1/2')
    // A config or batch node is not a run; the Run view would heal off it anyway, so its address
    // does not claim one.
    expect(hashFor(nav('run', { level: 'config', configName: 'goal-1' }))).toBe('/results/run/c1')
    // It has no notebook tabs at all, so it never spells one.
    expect(hashFor(nav('run', run, 'nav_report'))).toBe('/results/run/c1/goal-1/2')
  })

  it('gives the Data browser the campaign alone', () => {
    // Campaign-scoped: a node means nothing to it, and a hash is a view's address rather than a
    // place to stash state the view ignores.
    expect(hashFor(nav('data', run, 'nav_report'))).toBe('/results/data/c1')
  })

  it('spells a config name that needs escaping', () => {
    expect(hashFor(nav('explorer', { level: 'config', configName: 'goal 1' })))
      .toBe('/results/explorer/c1/goal%201')
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
      // The node forms. The bare ones above are the pre-node URLs, and still resolve unchanged.
      '#/results/explorer/nav-2026-08-12?batch=3',
      '#/results/explorer/nav-2026-08-12/goal-1',
      '#/results/explorer/nav-2026-08-12/goal-1/2',
      '#/results/explorer/nav-2026-08-12/goal-1/2?tab=nav_report',
      `#/results/explorer/nav-2026-08-12/goal-1/2?tab=${LOG_TAB_SLUG}`,
      '#/results/run/nav-2026-08-12/goal-1/2',
      // The share-import link. A round trip is the whole promise of it: the button copies
      // what `hashFor` writes and the recipient's browser hands it back to `navFromHash`.
      '#/execution?import=nav-2026-08-12',
    ]) {
      expect(`#${hashFor(at(hash))}`).toBe(hash)
    }
  })

  it('spells a share-import request for the campaign view only', () => {
    const asked = { ...DEFAULT_NAV, shareImport: 'nav-2026-08-12' }
    expect(hashFor(asked)).toBe('/execution?import=nav-2026-08-12')
    // Carried on any other topic it is not part of that view's address, so it is not spelled
    // — the same rule that keeps `?tab=` off every view but the Explorer.
    expect(hashFor({ ...asked, topicId: 'config' })).toBe('/config')
  })
})

describe('nextNav — which campaign survives a navigation', () => {
  const withResults: Nav = {
    topicId: 'results',
    viewId: 'run',
    campaignId: 'c1',
    configCampaignId: '',
    shareImport: '',
    openCampaign: '',
    sel: CAMPAIGN_SEL,
    tab: '',
  }
  const withConfig: Nav = {
    topicId: 'config',
    viewId: '',
    campaignId: '',
    configCampaignId: 'c1',
    shareImport: '',
    openCampaign: '',
    sel: CAMPAIGN_SEL,
    tab: '',
  }

  it('drops a share-import request', () => {
    // It belongs to the link it arrived on. Carried, it would re-open the share dialog every
    // time somebody clicked Campaigns in the sidebar.
    const asked: Nav = { ...withResults, topicId: 'execution', viewId: '', shareImport: 'c9' }
    expect(nextNav(asked, TOPICS, 'execution').shareImport).toBe('')
    expect(nextNav(asked, TOPICS, 'results', 'explorer').shareImport).toBe('')
  })

  it('carries the results campaign across topics', () => {
    // A change of lens on one campaign, not a request for a different one.
    expect(nextNav(withResults, TOPICS, 'results', 'data').campaignId).toBe('c1')
    expect(nextNav(withResults, TOPICS, 'execution').campaignId).toBe('c1')
  })

  it('carries the node and the tab with it', () => {
    // One level down, the same rule and for the same reason: it is what makes the Explorer's and
    // the Run view's cross-links land on the run that was on screen. Which of it a given view
    // *spells* is hashFor's decision, tested above.
    const onRun: Nav = {
      ...withResults,
      sel: { level: 'run', configName: 'goal-1', runId: 2 },
      tab: 'nav_report',
    }
    expect(nextNav(onRun, TOPICS, 'results', 'explorer')).toMatchObject({
      sel: { level: 'run', configName: 'goal-1', runId: 2 },
      tab: 'nav_report',
    })
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
      shareImport: '',
      openCampaign: '',
    })
  })
})

describe('#/execution?campaign=', () => {
  it('carries which campaign the view should open', () => {
    const nav = at('#/execution?campaign=nav-2026-08-30-1916')
    expect(nav.topicId).toBe('execution')
    expect(nav.openCampaign).toBe('nav-2026-08-30-1916')
  })

  it('round-trips through hashFor', () => {
    const nav = at('#/execution?campaign=a%2Fb')
    expect(nav.openCampaign).toBe('a/b')
    expect(at(`#${hashFor(nav)}`).openCampaign).toBe('a/b')
  })

  it('is refused when addressed at another page', () => {
    // Same rule as `import`, for the same reason: every page stays mounted under KeepAlive, so a
    // request one page would act on must not be readable from another page's address.
    expect(at('#/config?campaign=x').openCampaign).toBe('')
  })

  it('is dropped by a sidebar click, because that means "the list"', () => {
    const nav = at('#/execution?campaign=x')
    expect(nextNav(nav, TOPICS, 'execution').openCampaign).toBe('')
  })

  it('does not compete with an import request in one link', () => {
    const nav = { ...DEFAULT_NAV, topicId: 'execution', shareImport: 's', openCampaign: 'c' }
    expect(hashFor(nav)).toBe('/execution?import=s')
  })
})
