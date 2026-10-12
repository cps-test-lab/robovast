import { describe, expect, it } from 'vitest'
import { jobNameOf } from '@/components/runLog/useJobLogStream'
import { CAMPAIGN_SEL, navFromHash, type Nav } from './hashNav'
import { DEFAULT_CAMPAIGN_SORT } from './campaignSort'
import { runOfJob, runPhaseOf, runRowSql, runViewHashOf, sceneSpecOf } from './jobRun'

describe('runOfJob', () => {
  it('is the inverse of the job name the service gives a run', () => {
    expect(runOfJob({ job_name: jobNameOf('nav-a', 3), kind: 'run' }))
      .toEqual({ configName: 'nav-a', runId: 3 })
  })

  it('keeps a config name that has a slash of its own', () => {
    expect(runOfJob({ job_name: 'group/cfg/12', kind: 'run' }))
      .toEqual({ configName: 'group/cfg', runId: 12 })
  })

  it('maps a job from a service that sends no kind', () => {
    expect(runOfJob({ job_name: 'cfg/0' } as { job_name: string; kind: string }))
      .toEqual({ configName: 'cfg', runId: 0 })
  })

  it('maps no run for a calibration probe or a name of another shape', () => {
    expect(runOfJob({ job_name: 'cfg/0', kind: 'calibration' })).toBeNull()
    expect(runOfJob({ job_name: 'calibrate-node', kind: 'run' })).toBeNull()
    expect(runOfJob({ job_name: 'cfg/latest', kind: 'run' })).toBeNull()
    expect(runOfJob({ job_name: '/4', kind: 'run' })).toBeNull()
  })
})

describe('sceneSpecOf', () => {
  it('shows the scene the run view shows, with its bindings', () => {
    const spec = sceneSpecOf([
      { type: 'playback' },
      { type: 'scene3d', motion: { poses: 'my_poses' } },
    ])
    expect(spec?.type).toBe('scene3d')
    expect(spec?.config).toEqual({ motion: { poses: 'my_poses' } })
  })

  it('shows nothing for a campaign whose panels carry no 3D scene', () => {
    expect(sceneSpecOf([{ type: 'playback' }, { type: 'camera', kind: 'scene' }])).toBeNull()
    expect(sceneSpecOf(undefined)).toBeNull()
  })

  it('shows nothing for a scene the run view hides', () => {
    expect(sceneSpecOf([{ type: 'scene3d', hidden: true }])).toBeNull()
  })
})

describe('runRowSql / runPhaseOf', () => {
  it('reads one run of run_view, quoting the config name', () => {
    expect(runRowSql({ configName: "o'brien", runId: 2 })).toBe(
      "SELECT live FROM run_view WHERE config_name = 'o''brien' AND run_id = 2")
  })

  it('says whether the run has started, records, or has finished', () => {
    expect(runPhaseOf([])).toBe('starting')
    expect(runPhaseOf([{ live: true }])).toBe('live')
    expect(runPhaseOf([{ live: false }])).toBe('finished')
  })
})

const TOPICS = [
  { id: 'execution' },
  { id: 'results', views: [{ id: 'explorer' }, { id: 'run' }, { id: 'data' }] },
]
const FROM: Nav = {
  topicId: 'execution', viewId: '', campaignId: '', configCampaignId: '', shareImport: '',
  openCampaign: '', listSort: DEFAULT_CAMPAIGN_SORT, sel: CAMPAIGN_SEL, tab: '',
}

describe('runViewHashOf', () => {
  it('opens the run view on the job\'s campaign, config and run', () => {
    const hash = runViewHashOf('camp-1', { configName: 'group/cfg', runId: 7 })
    const nav = navFromHash(hash, TOPICS, FROM)
    expect(nav.topicId).toBe('results')
    expect(nav.viewId).toBe('run')
    expect(nav.campaignId).toBe('camp-1')
    expect(nav.sel).toEqual({ level: 'run', configName: 'group/cfg', runId: 7 })
  })
})
