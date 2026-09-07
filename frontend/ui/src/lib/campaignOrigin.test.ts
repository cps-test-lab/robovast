import { describe, expect, it } from 'vitest'

import { configVersionFact, isRerun, originFacts } from '@/lib/campaignOrigin'
import type { CampaignOrigin } from '@/lib/robovastClient'

const origin = (over: Partial<CampaignOrigin> = {}): CampaignOrigin => ({
  kind: 'workspace',
  workspace_id: 'ws-abc123',
  workspace_name: 'ros2demo',
  config_path: 'nav/basic_nav.vast',
  from_campaign: '',
  config_version_from: null,
  config_migration_steps: [],
  ...over,
})

const rerunOf = (over: Partial<CampaignOrigin> = {}): CampaignOrigin =>
  origin({ kind: 'retrigger', from_campaign: 'basic-nav-1', ...over })

describe('isRerun', () => {
  it('reads kind, and does not derive it from from_campaign', () => {
    // The day a third kind exists, a reader that derived this would be wrong.
    expect(isRerun(origin({ kind: 'retrigger' }))).toBe(true)
    expect(isRerun(origin({ from_campaign: 'something' }))).toBe(false)
    expect(isRerun(origin({ kind: 'scheduled' }))).toBe(false)
  })
})

describe('configVersionFact', () => {
  it('says which version a migrated re-run came from, and which it reached', () => {
    // Two runs of "the same campaign" that read different config versions are not the same
    // experiment, so the hover has to say it rather than leave it in a service log.
    const migrated = rerunOf({
      config_version_from: 1,
      config_migration_steps: ['1_to_2', '2_to_3', '3_to_4'],
    })
    expect(configVersionFact(migrated)).toBe('v1 → v4, migrated')
  })

  it('distinguishes a re-run that migrated nothing from one that recorded nothing', () => {
    expect(configVersionFact(rerunOf({ config_version_from: 4 }))).toBe('v4, as written')
    expect(configVersionFact(rerunOf())).toBe('')
  })

  it('says nothing for a launch that is not a re-run', () => {
    // The field is only meaningful for a re-run; `kind` is the authority on which this is.
    expect(configVersionFact(origin({ config_version_from: 4 }))).toBe('')
  })
})

describe('originFacts', () => {
  it('lists the workspace, its id and the FULL path', () => {
    expect(originFacts(origin())).toEqual([
      { label: 'Rerun of', value: '' },
      { label: 'Config', value: '' },
      { label: 'Workspace', value: 'ros2demo' },
      { label: 'ID', value: 'ws-abc123' },
      { label: 'File', value: 'nav/basic_nav.vast' },
    ])
  })

  it("keeps a re-run's inherited workspace, so the lineage is readable in one hover", () => {
    const rerun = rerunOf({ config_version_from: 1, config_migration_steps: ['1_to_2'] })
    expect(originFacts(rerun)).toEqual([
      { label: 'Rerun of', value: 'basic-nav-1' },
      { label: 'Config', value: 'v1 → v2, migrated' },
      { label: 'Workspace', value: 'ros2demo' },
      { label: 'ID', value: 'ws-abc123' },
      { label: 'File', value: 'nav/basic_nav.vast' },
    ])
  })
})
