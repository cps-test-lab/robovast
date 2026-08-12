// The Results tree's pure model. Tested here rather than through the UI because the node **ids**
// are a contract between two components that build them independently: ResultsTree renders them,
// and the Run view reconstructs one to highlight the current run. A drift between the two shows up
// only as "the picker no longer opens on the selected run", which is invisible to `tsc` and easy to
// miss by hand.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { ancestorIds, buildCampaignChildren } from './resultsTree'

const CID = 'nav-2026-08-12'

/** One `run_view` row, with the four columns the tree selects. */
function row(configName: string, runId: number | null, status: string) {
  return { config_name: configName, run_id: runId, status, passed: status === 'passed' ? 1 : 0 }
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
