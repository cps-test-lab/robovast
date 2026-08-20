import { describe, expect, it } from 'vitest'

import { isRerun, originFacts } from '@/lib/campaignOrigin'
import type { CampaignOrigin } from '@/lib/robovastClient'

const origin = (over: Partial<CampaignOrigin> = {}): CampaignOrigin => ({
  kind: 'workspace',
  workspace_id: 'ws-abc123',
  workspace_name: 'ros2demo',
  config_path: 'nav/basic_nav.vast',
  from_campaign: '',
  ...over,
})

describe('isRerun', () => {
  it('reads kind, and does not derive it from from_campaign', () => {
    // The day a third kind exists, a reader that derived this would be wrong.
    expect(isRerun(origin({ kind: 'retrigger' }))).toBe(true)
    expect(isRerun(origin({ from_campaign: 'something' }))).toBe(false)
    expect(isRerun(origin({ kind: 'scheduled' }))).toBe(false)
  })
})

describe('originFacts', () => {
  it('lists the workspace, its id and the FULL path', () => {
    expect(originFacts(origin())).toEqual([
      { label: 'Rerun of', value: '' },
      { label: 'Workspace', value: 'ros2demo' },
      { label: 'ID', value: 'ws-abc123' },
      { label: 'File', value: 'nav/basic_nav.vast' },
    ])
  })

  it("keeps a re-run's inherited workspace, so the lineage is readable in one hover", () => {
    const rerun = origin({ kind: 'retrigger', from_campaign: 'basic-nav-1' })
    expect(originFacts(rerun)).toEqual([
      { label: 'Rerun of', value: 'basic-nav-1' },
      { label: 'Workspace', value: 'ros2demo' },
      { label: 'ID', value: 'ws-abc123' },
      { label: 'File', value: 'nav/basic_nav.vast' },
    ])
  })
})
