// The preview picker's pure model: campaign output directories -> run rows.
//
// Tested here rather than through the UI for the same reason `resultsTree.test.ts` is: the ROW SHAPE
// is a contract with `CAMPAIGN_RUNS_SQL` that nothing type-checks. These rows are fed to
// `buildCampaignChildren`, which reads its columns by name, so a column renamed in the SQL leaves
// this producer silently emitting the old one — a preview tree that renders empty, with nothing to
// point at.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { configDirs, declaresScene3d, previewRunRows, runIds } from './previewRuns'
import { buildCampaignChildren, CAMPAIGN_RUNS_SQL, campaignItem } from './resultsTree'
import { hasResults, isPreviewable, type CampaignSummary } from './robovastClient'

const summary = (s: Partial<CampaignSummary>): CampaignSummary =>
  ({
    campaign_id: 'camp-1', phase: 'finished', postprocessed: true, num_runs: 0, num_passed: 0,
    num_failed: 0, num_no_sample: 0, num_composition_failed: 0, ...s,
  } as CampaignSummary)

describe('configDirs', () => {
  it('takes the directories and leaves the files', () => {
    // This space lists both at one level, directories suffixed `/`.
    expect(configDirs(['nav_fast/', 'nav_slow/', 'campaign.db', 'metadata.yaml'])).toEqual([
      'nav_fast', 'nav_slow',
    ])
  })

  it('drops the campaign machinery that is not a configuration', () => {
    expect(configDirs(['_config/', '_execution/', '_transient/', '_jobs/', '.cache/', 'nav/']))
      .toEqual(['nav'])
  })

  it('is empty for a campaign that has produced nothing yet', () => {
    expect(configDirs([])).toEqual([])
  })
})

describe('runIds', () => {
  it('keeps only the numeric run directories', () => {
    // A configuration directory holds more than its runs; only the numbered ones are runs.
    expect(runIds(['0/', '1/', 'notebook.ipynb', 'summary/'])).toEqual([0, 1])
  })

  it('orders numerically, not by name', () => {
    // Lexical order puts 10 between 1 and 2, which would disagree with every other listing
    // of the same runs — including the one the Explorer draws once the campaign finishes.
    expect(runIds(['0/', '1/', '2/', '10/', '11/'])).toEqual([0, 1, 2, 10, 11])
  })
})

// The listings a real campaign answers with, copied verbatim from a cluster campaign's
// `/results/<id>/` and `/results/<id>/<config>/`. The parser above is written against a documented
// layout; this is the layout itself, so a change to what a campaign writes fails here rather than
// in a picker that quietly lists nothing.
describe('a real campaign listing', () => {
  const ROOT = ['_config/', '_execution/', '_jobs/', '_transient/', 'campaign.db',
                'goal-1/', 'metadata.prov.json', 'metadata.yaml']
  const CONFIG = ['0/', '1/', '2/', '3/', '_config/']

  it('finds the configuration among the machinery and the loose files', () => {
    expect(configDirs(ROOT)).toEqual(['goal-1'])
  })

  it('finds the runs, and not the per-config _config directory beside them', () => {
    expect(runIds(CONFIG)).toEqual([0, 1, 2, 3])
  })
})

describe('previewRunRows', () => {
  const rows = previewRunRows(new Map([['nav_slow', [0, 1]], ['nav_fast', [0]]]))

  it('names the columns CAMPAIGN_RUNS_SQL selects', () => {
    // Asserted against the SQL text itself: these rows stand in for that query's result, and the
    // tree reads the columns by name. Renaming one there must fail here rather than in a blank UI.
    for (const column of ['config_name', 'run_id', 'status', 'passed', 'objective', 'batch']) {
      expect(CAMPAIGN_RUNS_SQL).toContain(column)
      expect(rows[0]).toHaveProperty(column)
    }
  })

  it('carries no verdict, because a running campaign has none to read', () => {
    expect(rows.map((r) => r.status)).toEqual(['unknown', 'unknown', 'unknown'])
    expect(rows.every((r) => r.passed === null)).toBe(true)
  })

  it('carries no batch: rounds are recorded in the index, not on disk', () => {
    expect(rows.every((r) => r.batch === null)).toBe(true)
  })

  it('builds the same tree shape the Explorer builds from SQL rows', () => {
    const children = buildCampaignChildren('camp-1', rows)
    expect(children.map((c) => c.label)).toEqual(['nav_fast', 'nav_slow'])
    expect(children[1].children?.length).toBe(2)
  })
})

describe('isPreviewable', () => {
  it('is a running campaign that has recorded a run', () => {
    expect(isPreviewable(summary({ phase: 'running', num_runs: 1 }))).toBe(true)
  })

  it('is not a campaign that has produced nothing yet', () => {
    // Nothing to list, so the picker would be empty for a reason nobody could act on.
    expect(isPreviewable(summary({ phase: 'running', num_runs: 0 }))).toBe(false)
  })

  it('is not a search whose every draw failed to compose', () => {
    // `hasRecordedRuns` counts those as something to show, because the *reason* is worth reading.
    // There is no run behind them, so there is nothing to replay.
    expect(isPreviewable(summary({ phase: 'running', num_composition_failed: 3 }))).toBe(false)
  })

  it('is never true once the campaign is over, whatever its results', () => {
    // The preview exists only for the window where the index has no rows yet. A finished campaign
    // that was never postprocessed has none either, but it will not grow any — it is not a preview,
    // it is a campaign with nothing to show, and the two must not read the same.
    expect(isPreviewable(summary({ num_runs: 4 }))).toBe(false)
    expect(isPreviewable(summary({ postprocessed: false, num_runs: 4 }))).toBe(false)
  })

  it('is the complement of hasResults, never an overlap', () => {
    // The Results container admits `hasResults(c) || isPreviewable(c)`; a campaign satisfying both
    // would be offered two different sets of rows for the same id.
    for (const phase of ['running', 'finishing', 'postprocessing', 'finished', 'failed']) {
      const c = summary({ phase, num_runs: 2 })
      expect(hasResults(c) && isPreviewable(c)).toBe(false)
    }
  })
})

describe('the preview is named where the campaign is chosen', () => {
  it('marks a previewable campaign in the picker tree', () => {
    expect(campaignItem(summary({ phase: 'running', num_runs: 2 })).count).toContain('preview')
  })

  it('leaves a finished campaign count alone', () => {
    expect(campaignItem(summary({ num_runs: 2, num_passed: 2 })).count).toBe('2/2')
  })
})

describe('a config of verdict-less runs', () => {
  it('does not claim 0 of them passed', () => {
    // "0/3" is what a config whose every run FAILED looks like. A preview's runs have no readable
    // verdict at all, and saying the same thing about both would report a healthy campaign as a
    // total loss.
    const [config] = buildCampaignChildren('camp-1', previewRunRows(new Map([['nav', [0, 1, 2]]])))
    expect(config.count).toBe('3 runs')
  })

  it('still reports the ratio once verdicts exist', () => {
    const rows = [
      { config_name: 'nav', run_id: 0, status: 'passed' },
      { config_name: 'nav', run_id: 1, status: 'failed' },
    ]
    expect(buildCampaignChildren('camp-1', rows)[0].count).toBe('1/2')
  })
})

describe('declaresScene3d', () => {
  it('is true when the campaign has a scene to replay', () => {
    expect(declaresScene3d([{ type: 'playback' }, { type: 'scene3d' }])).toBe(true)
  })

  it('is false for a simulator that records none', () => {
    // Then a preview would be an empty window: every other panel reads the index, which holds
    // nothing for a running campaign. Such a campaign is not offered until it finishes.
    expect(declaresScene3d([{ type: 'playback' }, { type: 'timeseries' }])).toBe(false)
  })

  it('is undefined until the panel list has been read, so neither guess is made', () => {
    // Guessing true offers a campaign that then shows nothing; guessing false hides one that
    // appears a moment later. Both are visible to somebody, so the caller waits instead.
    expect(declaresScene3d(undefined)).toBeUndefined()
  })
})
