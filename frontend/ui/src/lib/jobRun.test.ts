import { describe, expect, it } from 'vitest'
import { CAMPAIGN_SEL, navFromHash, type Nav } from './hashNav'
import { DEFAULT_CAMPAIGN_SORT } from './campaignSort'
import type { JobSummary } from './robovastClient'
import { jobOfRun, runOfJob, runPhaseOf, runRowSql, runViewHashOf, sceneSpecOf } from './jobRun'

/** A listed job in the cluster's shape: a Kubernetes Job's name, and the run it executes beside it. */
const clusterJob = (over: Partial<JobSummary> = {}): JobSummary => ({
  job_name: 'basicn-15180331-batch0dd05f94958dd31b7-0',
  kind: 'run',
  config_name: 'goal-1',
  run_id: 0,
  status: 'running',
  display_name: 'batch-0-job-0',
  detail: null,
  node: null,
  started_at: null,
  usage: null,
  ...over,
})

describe('runOfJob', () => {
  it('is the run the service says a cluster job executes, whatever the job is named', () => {
    expect(runOfJob(clusterJob())).toEqual({ configName: 'goal-1', runId: 0 })
  })

  it('reads nothing from the job name', () => {
    // A name of the run-key shape names no run: only `config_name` / `run_id` do.
    expect(runOfJob(clusterJob({ job_name: 'cfg/3', config_name: null, run_id: null })))
      .toBeNull()
  })

  it('maps no run for a calibration probe or a job not yet tied to its run', () => {
    expect(runOfJob(clusterJob({ kind: 'calibration' }))).toBeNull()
    expect(runOfJob(clusterJob({ status: 'waiting', config_name: null, run_id: null })))
      .toBeNull()
  })

  it('maps no run from a service that sends neither field', () => {
    const { config_name: _c, run_id: _r, ...older } = clusterJob()
    expect(runOfJob(older as JobSummary)).toBeNull()
  })
})

describe('jobOfRun', () => {
  const jobs = [
    clusterJob({ job_name: 'probe', kind: 'calibration' }),
    clusterJob({ job_name: 'k8s-a', config_name: 'goal-1', run_id: 0 }),
    clusterJob({ job_name: 'k8s-b', config_name: 'goal-1', run_id: 1 }),
  ]

  it("finds the listed job that executes a run, by the service's mapping", () => {
    expect(jobOfRun(jobs, { configName: 'goal-1', runId: 1 })?.job_name).toBe('k8s-b')
  })

  it('finds none for a run no listed job executes', () => {
    expect(jobOfRun(jobs, { configName: 'goal-1', runId: 2 })).toBeNull()
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
