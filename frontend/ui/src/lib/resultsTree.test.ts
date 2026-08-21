// The Results tree's pure model. Tested here rather than through the UI because the node **ids**
// are a contract between two components that build them independently: ResultsTree renders them,
// and the Run view reconstructs one to highlight the current run. A drift between the two shows up
// only as "the picker no longer opens on the selected run", which is invisible to `tsc` and easy to
// miss by hand.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import {
  ancestorIds,
  buildCampaignChildren,
  firstRunSelection,
  objectiveDirection,
  resolveSelection,
  runNodeId,
  selectionNodeId,
  selectionOf,
} from './resultsTree'
import { CAMPAIGN_SEL, type ResultsSel } from './hashNav'

const CID = 'nav-2026-08-12'

/** One `run_view` row, with the columns the tree selects. */
function row(
  configName: string,
  runId: number | null,
  status: string,
  extra: { batch?: number | null; objective?: number | null } = {},
) {
  return {
    config_name: configName,
    run_id: runId,
    status,
    passed: status === 'passed' ? 1 : 0,
    objective: extra.objective ?? null,
    ...('batch' in extra ? { batch: extra.batch } : {}),
  }
}

describe('buildCampaignChildren', () => {
  it('groups runs under their config and spells the ids as <campaign>//cfg/<name>//run/<id>', () => {
    const children = buildCampaignChildren(CID, [
      row('nav_slow', 0, 'passed'),
      row('nav_slow', 1, 'passed'),
      row('nav_fast', 0, 'passed'),
    ])

    expect(children.map((c) => c.id)).toEqual([`${CID}//cfg/nav_slow`, `${CID}//cfg/nav_fast`])
    expect(children[0].children?.map((r) => r.id)).toEqual([
      `${CID}//cfg/nav_slow//run/0`,
      `${CID}//cfg/nav_slow//run/1`,
    ])
    // Configs keep first-seen order, not alphabetical: the query orders the rows.
    expect(children.map((c) => c.label)).toEqual(['nav_slow', 'nav_fast'])
    expect(children[0].children?.map((r) => r.label)).toEqual(['run 0', 'run 1'])
  })

  it('rolls a config up from its runs and counts the passes', () => {
    const [allPassed, oneFailed] = buildCampaignChildren(CID, [
      row('good', 0, 'passed'),
      row('good', 1, 'passed'),
      row('bad', 0, 'passed'),
      row('bad', 1, 'failed'),
    ])

    expect(allPassed.status).toBe('passed')
    expect(allPassed.count).toBe('2/2')
    // One failure decides the config, however many runs passed.
    expect(oneFailed.status).toBe('failed')
    expect(oneFailed.count).toBe('1/2')
  })

  it('renders a composition-failed draw as a disabled placeholder, not a run', () => {
    const [config] = buildCampaignChildren(CID, [row('c1f4a2', null, 'composition_failed')])

    // `skipped`, not `failed`: the draw was never realizable, so there is no run that lost.
    expect(config.status).toBe('skipped')
    // "0/0 passed" would be a verdict on something that never ran.
    expect(config.count).toBe('skipped')

    const [child] = config.children ?? []
    expect(child.kind).toBe('placeholder')
    expect(child.disabled).toBe(true)
    expect(child.runId).toBeUndefined()
    expect(child.id).toBe(`${CID}//cfg/c1f4a2//not-composed`)
  })

  it('marks an error as failed and an unrecognised status as unknown', () => {
    const [errored, odd] = buildCampaignChildren(CID, [
      row('errored', 0, 'error'),
      row('odd', 0, 'something-else'),
    ])

    expect(errored.children?.[0].status).toBe('failed')
    expect(odd.children?.[0].status).toBe('unknown')
    expect(odd.status).toBe('unknown')
  })
})

describe('ancestorIds', () => {
  it('walks a run id back up to its campaign', () => {
    const [run] = buildCampaignChildren(CID, [row('nav_slow', 3, 'passed')])[0].children ?? []

    // What opens the tree on the current selection: every ancestor must be a real node id.
    expect(ancestorIds(run.id)).toEqual([CID, `${CID}//cfg/nav_slow`])
  })
})

// A search campaign's tree gains the round that proposed each config. Everything above stays
// true unchanged -- that is what says the batch level did not disturb the ungrouped shape.
describe('buildCampaignChildren, grouped by batch', () => {
  const searchRows = [
    row('c1f4a2', 0, 'passed', { batch: 0, objective: 0.31 }),
    row('c1f4a2', 1, 'failed', { batch: 0, objective: 0.31 }),
    row('c9b30e', 0, 'passed', { batch: 0, objective: 0.84 }),
    row('c77de1', 0, 'passed', { batch: 1, objective: 0.913 }),
    row('c77de1', 1, 'passed', { batch: 1, objective: 0.913 }),
  ]

  it('nests configs under their round, in round order', () => {
    const children = buildCampaignChildren(CID, searchRows, { grouped: true })

    expect(children.map((c) => c.kind)).toEqual(['batch', 'batch'])
    expect(children.map((c) => c.id)).toEqual([`${CID}//batch/0`, `${CID}//batch/1`])
    expect(children[0].children?.map((c) => c.id)).toEqual([
      `${CID}//batch/0//cfg/c1f4a2`,
      `${CID}//batch/0//cfg/c9b30e`,
    ])
  })

  it('counts runs rather than configs, so a batch chip sums to the campaign', () => {
    const [batch0, batch1] = buildCampaignChildren(CID, searchRows, { grouped: true })

    expect(batch0.count).toBe('2/3')
    expect(batch1.count).toBe('2/2')
    // One failed run in round 0 decides that round.
    expect(batch0.status).toBe('failed')
    expect(batch1.status).toBe('passed')
  })

  it('labels the objective per config and the best per round, honouring the direction', () => {
    const [maxBatch] = buildCampaignChildren(CID, searchRows, {
      grouped: true,
      direction: 'maximize',
    })
    const [minBatch] = buildCampaignChildren(CID, searchRows, {
      grouped: true,
      direction: 'minimize',
    })

    expect(maxBatch.children?.map((c) => c.label)).toEqual(['c1f4a2  [0.31]', 'c9b30e  [0.84]'])
    // The same rows, opposite ends: taking the max of a minimised objective would report the
    // worst draw of the round as its best.
    expect(maxBatch.label).toBe('batch 0  best 0.84')
    expect(minBatch.label).toBe('batch 0  best 0.31')
  })

  it('agrees with runNodeId, and with ancestorIds all the way up', () => {
    const [, batch1] = buildCampaignChildren(CID, searchRows, { grouped: true })
    const run = batch1.children?.[0].children?.[0]

    // The Run view rebuilds this id from (campaign, batch, config, run) to highlight the run it
    // is showing; if the two ever disagree the picker silently stops opening on the selection.
    expect(run?.id).toBe(runNodeId(CID, 1, 'c77de1', 0))
    expect(ancestorIds(run!.id)).toEqual([
      CID,
      `${CID}//batch/1`,
      `${CID}//batch/1//cfg/c77de1`,
    ])
  })

  it('keeps a re-proposed paramset distinct per round', () => {
    // A search may draw the same parameters twice; they share a config_name (a content hash) and
    // a result directory, but they are two units and must be two nodes.
    const children = buildCampaignChildren(
      CID,
      [
        row('c1f4a2', 0, 'passed', { batch: 0, objective: 0.5 }),
        row('c1f4a2', 0, 'passed', { batch: 2, objective: 0.5 }),
      ],
      { grouped: true },
    )

    expect(children.map((c) => c.children?.[0].id)).toEqual([
      `${CID}//batch/0//cfg/c1f4a2`,
      `${CID}//batch/2//cfg/c1f4a2`,
    ])
  })

  it('files an uncomposable draw under its own round', () => {
    const [batch0, batch1] = buildCampaignChildren(
      CID,
      [
        row('c1f4a2', 0, 'passed', { batch: 0, objective: 0.5 }),
        row('cdead1', null, 'composition_failed', { batch: 1 }),
      ],
      { grouped: true },
    )

    expect(batch0.status).toBe('passed')
    // A round whose every draw was unrealizable is skipped, not failed: nothing ran and lost.
    expect(batch1.status).toBe('skipped')
    expect(batch1.count).toBe('1 skipped')
    expect(batch1.children?.[0].children?.[0].id).toBe(
      `${CID}//batch/1//cfg/cdead1//not-composed`,
    )
  })

  it('hangs a row with no recorded batch off the campaign instead of losing it', () => {
    // An orphan unit.batch_id, or a store predating the batch table: run_view reports NULL.
    // Inventing a round would claim history the store does not have.
    const children = buildCampaignChildren(
      CID,
      [
        row('c1f4a2', 0, 'passed', { batch: 0, objective: 0.5 }),
        row('corphan', 0, 'passed', { batch: null }),
      ],
      { grouped: true },
    )

    expect(children.map((c) => c.kind)).toEqual(['batch', 'config'])
    expect(children[1].id).toBe(`${CID}//cfg/corphan`)
  })
})

describe('objectiveDirection', () => {
  it('reads the campaign direction and defaults to maximize', () => {
    expect(objectiveDirection([{ objective_direction: 'minimize' }])).toBe('minimize')
    expect(objectiveDirection([{ objective_direction: 'maximize' }])).toBe('maximize')
    // A batch campaign has no search block, so the subselect yields NULL. `maximize` matches
    // the .vast schema's own default for an objective that does not name a direction.
    expect(objectiveDirection([{ objective_direction: null }])).toBe('maximize')
    expect(objectiveDirection([])).toBe('maximize')
  })
})

// The bridge between a URL and the tree. Tested for the same reason as the ids themselves, one step
// further out: the URL is a third builder of node ids, and a selection carried in it has to select
// the very node the tree rendered — a near-miss shows up only as a tree that opens collapsed on a
// pasted link, which nothing else would catch.
describe('selectionNodeId', () => {
  const RUN: ResultsSel = { level: 'run', configName: 'nav_slow', runId: 1 }

  it('agrees with runNodeId, grouped and ungrouped', () => {
    expect(selectionNodeId(CID, RUN, null)).toBe(runNodeId(CID, null, 'nav_slow', 1))
    expect(selectionNodeId(CID, RUN, 3)).toBe(runNodeId(CID, 3, 'nav_slow', 1))
  })

  it('addresses every level, and its ancestors are the levels above it', () => {
    expect(selectionNodeId(CID, CAMPAIGN_SEL, null)).toBe(CID)
    expect(selectionNodeId(CID, { level: 'batch', batch: 3 }, null)).toBe(`${CID}//batch/3`)
    expect(selectionNodeId(CID, { level: 'config', configName: 'nav_slow' }, 3))
      .toBe(`${CID}//batch/3//cfg/nav_slow`)
    // What actually makes the tree expand onto a deep-linked run.
    expect(ancestorIds(selectionNodeId(CID, RUN, 3)))
      .toEqual([CID, `${CID}//batch/3`, `${CID}//batch/3//cfg/nav_slow`])
  })

  it('is the inverse of selectionOf, so a click and a link agree', () => {
    const [config] = buildCampaignChildren(CID, [row('nav_slow', 1, 'passed')])
    const runNode = config.children![0]
    expect(selectionNodeId(CID, selectionOf(runNode), null)).toBe(runNode.id)
    expect(selectionNodeId(CID, selectionOf(config), null)).toBe(config.id)
  })

  it('reads a placeholder as no node at all', () => {
    // A unit whose configuration could not be built has no run behind it, so there is nothing to
    // address — and it is not selectable in the tree either.
    const [config] = buildCampaignChildren(CID, [row('nope', null, 'composition_failed')])
    expect(selectionOf(config.children![0])).toEqual(CAMPAIGN_SEL)
  })
})

describe('resolveSelection', () => {
  const ROWS = [
    row('nav_slow', 0, 'passed', { batch: 0 }),
    row('nav_slow', 1, 'passed', { batch: 0 }),
    row('nav_fast', 0, 'passed', { batch: 1 }),
  ]

  it('finds the round a search campaign proposed the run in', () => {
    // The batch is never in the URL — it is derivable from the config, so carrying it would be a
    // second copy of a fact that could then disagree. This lookup is what replaces it.
    expect(resolveSelection(ROWS, true, { level: 'run', configName: 'nav_fast', runId: 0 }))
      .toEqual({ sel: { level: 'run', configName: 'nav_fast', runId: 0 }, batch: 1 })
    expect(resolveSelection(ROWS, true, { level: 'config', configName: 'nav_slow' }).batch).toBe(0)
  })

  it('keeps the batch out of an ungrouped campaign entirely', () => {
    // A batch-mode campaign has exactly one round, so its tree is flat and `row.batch` (a real 0)
    // must not reach the ids — the same rule buildCampaignChildren applies.
    expect(resolveSelection(ROWS, false, { level: 'run', configName: 'nav_fast', runId: 0 }).batch)
      .toBeNull()
    // ...and it has no batch node to address, so a link naming one falls back.
    expect(resolveSelection(ROWS, false, { level: 'batch', batch: 1 }).sel).toEqual(CAMPAIGN_SEL)
  })

  it('falls back to the campaign for a node this campaign does not have', () => {
    // A wrong link, not a stale one: a finished campaign's runs are fixed, so there is nothing to
    // wait for and nothing to keep re-checking.
    expect(resolveSelection(ROWS, true, { level: 'config', configName: 'gone' }).sel)
      .toEqual(CAMPAIGN_SEL)
    expect(resolveSelection(ROWS, true, { level: 'run', configName: 'nav_slow', runId: 99 }).sel)
      .toEqual(CAMPAIGN_SEL)
    expect(resolveSelection(ROWS, true, { level: 'batch', batch: 7 }).sel).toEqual(CAMPAIGN_SEL)
    expect(resolveSelection([], true, { level: 'config', configName: 'nav_slow' }).sel)
      .toEqual(CAMPAIGN_SEL)
  })

  it('does not resolve a run that was never composed', () => {
    const rows = [row('nope', null, 'composition_failed', { batch: 0 })]
    expect(resolveSelection(rows, true, { level: 'run', configName: 'nope', runId: 0 }).sel)
      .toEqual(CAMPAIGN_SEL)
    // The config node above it is still real — the draw exists, it just never ran.
    expect(resolveSelection(rows, true, { level: 'config', configName: 'nope' }).sel)
      .toEqual({ level: 'config', configName: 'nope' })
  })
})

describe('firstRunSelection', () => {
  it('takes the first replayable run in the tree\'s own order', () => {
    expect(firstRunSelection([
      row('nope', null, 'composition_failed'),
      row('nav_slow', 0, 'passed'),
      row('nav_slow', 1, 'passed'),
    ])).toEqual({ level: 'run', configName: 'nav_slow', runId: 0 })
  })

  it('has no answer when nothing ran', () => {
    expect(firstRunSelection([])).toBeNull()
    expect(firstRunSelection([row('nope', null, 'composition_failed')])).toBeNull()
  })
})
