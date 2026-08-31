// The URL ↔ tree round trip, over *every* node a campaign's tree contains.
//
// The narrow tests next door check each piece: `hashNav.test.ts` that a hash parses and spells back,
// `resultsTree.test.ts` that `selectionNodeId` agrees with the id builders. This one closes the loop
// the user actually depends on — click a node, copy the address bar, paste it, land on that node —
// and it does so exhaustively rather than on hand-picked cases. That is the point: a level added to
// the tree later, or a field left out of `hashFor`, fails here without anyone having to remember to
// write a case for it, which is exactly the mistake the narrow tests cannot catch.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import {
  buildCampaignChildren,
  indexById,
  resolveSelection,
  selectionNodeId,
  selectionOf,
} from './resultsTree'
import { CAMPAIGN_SEL, hashFor, navFromHash, type Nav } from './hashNav'

const TOPICS = [
  { id: 'config' },
  { id: 'execution' },
  { id: 'results', views: [{ id: 'explorer' }, { id: 'run' }, { id: 'data' }] },
]

const FALLBACK: Nav = {
  topicId: 'execution',
  viewId: '',
  campaignId: '',
  sel: CAMPAIGN_SEL,
  tab: '',
  configCampaignId: '',
  shareImport: '',
  openCampaign: '',
}

const CID = 'tb4-office-nav-local-2026-08-21-10395724'

/** One real campaign's `run_view` rows, as `CAMPAIGN_RUNS_SQL` returns them. Kept verbatim from a
 *  campaign on disk so the batch-mode case is the shape the service actually produces — including
 *  `batch: 0`, which must *not* reach an ungrouped campaign's node ids. */
const BATCH_MODE = [
  { config_name: 'office-nav-local', run_id: 0, status: 'passed', passed: 1, objective: null, batch: 0 },
]

/** A search campaign: two rounds, a config with repetitions, and a draw that never composed. No
 *  local campaign has this shape, and it is the one where the batch is load-bearing. */
const SEARCH = [
  { config_name: 'goal-1', run_id: 0, status: 'passed', passed: 1, objective: 0.4, batch: 0 },
  { config_name: 'goal-2', run_id: 0, status: 'failed', passed: 0, objective: 0.9, batch: 1 },
  { config_name: 'goal-2', run_id: 1, status: 'passed', passed: 1, objective: 0.9, batch: 1 },
  { config_name: 'goal-3', run_id: null, status: 'composition_failed', passed: 0, objective: null, batch: 1 },
]

const CASES = [
  { label: 'a batch-mode campaign', rows: BATCH_MODE, grouped: false },
  { label: 'a search campaign, grouped by round', rows: SEARCH, grouped: true },
] as const

describe.each(CASES)('every node of $label survives a URL', ({ rows, grouped }) => {
  const tree = buildCampaignChildren(CID, rows, { grouped })
  // Placeholders are not selectable and have no address, which `selectionOf` also reports.
  const nodes = [...indexById(tree).values()].filter((i) => i.kind !== 'placeholder')

  it('has nodes at more than one level to check', () => {
    expect(new Set(nodes.map((n) => n.kind)).size).toBeGreaterThan(1)
  })

  it.each(nodes.map((n) => [n.kind, n.id, n] as const))(
    'the Explorer comes back to the same %s',
    (_kind, _id, item) => {
      const hash = `#${hashFor({
        ...FALLBACK,
        topicId: 'results',
        viewId: 'explorer',
        campaignId: CID,
        sel: selectionOf(item),
        tab: 'nav_report',
      })}`
      const back = navFromHash(hash, TOPICS, FALLBACK)
      expect(back.campaignId).toBe(CID)
      expect(back.tab).toBe('nav_report')
      const resolved = resolveSelection(rows, grouped, back.sel)
      expect(selectionNodeId(CID, resolved.sel, resolved.batch)).toBe(item.id)
    },
  )

  it('the Run view comes back to a run, and to nothing it cannot replay', () => {
    for (const item of nodes) {
      const hash = `#${hashFor({
        ...FALLBACK,
        topicId: 'results',
        viewId: 'run',
        campaignId: CID,
        sel: selectionOf(item),
        tab: 'nav_report',
      })}`
      const back = navFromHash(hash, TOPICS, FALLBACK)
      // It has no notebook tabs, so it never carries one however it was linked to.
      expect(back.tab).toBe('')
      const resolved = resolveSelection(rows, grouped, back.sel)
      if (item.kind === 'run') expect(selectionNodeId(CID, resolved.sel, resolved.batch)).toBe(item.id)
      else expect(resolved.sel).toEqual(CAMPAIGN_SEL)
    }
  })
})
