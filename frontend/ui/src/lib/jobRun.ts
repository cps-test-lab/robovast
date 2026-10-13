// Which run a listed job executes, and whether its campaign has a 3D view to show beside the job's
// live log. Pure, so the Jobs list's 3D pane decides from data it can test.
//
// Markup stays in `components/`; this is the same split `lib/jobKind.ts` makes.

import type { PanelSpec } from '@robovast/panel-kit'
import type { JobSummary } from './robovastClient'
import { isNonRunJob } from './jobKind'
import { resultsViewHash } from './nav'
import { parsePanels } from './panels/parsePanels'

/** One run of a campaign, addressed the way its tables key it. */
export interface JobRun {
  configName: string
  runId: number
}

/** The run a job executes, as the service reports it (`config_name`, `run_id`). Null for a job
 *  that executes none (a calibration probe) and for one the service has not tied to its run yet (a
 *  job still queued for capacity). Never read from `job_name`: on the cluster that is the
 *  Kubernetes Job's name, which says nothing about the run. */
export function runOfJob(
  job: Pick<JobSummary, 'kind' | 'config_name' | 'run_id'>,
): JobRun | null {
  if (isNonRunJob(job)) return null
  // `== null` rather than a type test: a service without these fields sends nothing at all.
  if (job.config_name == null || job.run_id == null) return null
  return { configName: job.config_name, runId: job.run_id }
}

/** The listed job that executes `run`, or null when none does -- the run has not started, or its
 *  job is no longer listed. How a run addresses the job operations that take a listed job only. */
export function jobOfRun(jobs: readonly JobSummary[], run: JobRun): JobSummary | null {
  return (
    jobs.find((j) => {
      const r = runOfJob(j)
      return r !== null && r.configName === run.configName && r.runId === run.runId
    }) ?? null
  )
}

/** The 3D scene panel a campaign's run view shows, or null when it shows none.
 *
 *  `/panels` is the run view's own answer: the panels the `.vast` declares, merged with what its
 *  simulator backend contributes -- a backend whose runs record scene state contributes a
 *  `scene3d` (its `default_panels`). So "this campaign has a 3D view" is read from the same list
 *  the run view mounts, and a declared panel's bindings (which pose and joint tables) come with it.
 *  A `hidden` one is not shown there, so it is not shown beside the log either. */
export function sceneSpecOf(rawPanels: Record<string, unknown>[] | undefined): PanelSpec | null {
  if (!rawPanels) return null
  return parsePanels(rawPanels).find((p) => p.type === 'scene3d' && !p.hidden) ?? null
}

/** Quote a value as a SQL string literal (single-quote escaped). */
const sqlStr = (v: string) => `'${v.replace(/'/g, "''")}'`

/** The query for one run's `run_view` row: whether it exists yet, and whether it is still
 *  recording. `run_view` lists a run from the moment its directory exists, so a job that has no row
 *  has not started its run. */
export function runRowSql(run: JobRun): string {
  if (!Number.isInteger(run.runId)) throw new Error(`run_id must be an integer, got ${run.runId}`)
  return (
    'SELECT live FROM run_view ' +
    `WHERE config_name = ${sqlStr(run.configName)} AND run_id = ${run.runId}`
  )
}

/** What a job's run is, read from its `run_view` rows: not started, still recording, or finished. */
export type RunPhase = 'starting' | 'live' | 'finished'

export function runPhaseOf(rows: Record<string, unknown>[]): RunPhase {
  if (!rows.length) return 'starting'
  return rows[0].live ? 'live' : 'finished'
}

/** The run view's address for one job's run: that campaign, config and run selected. The run view
 *  follows the edge of a run that is still recording on its own. */
export function runViewHashOf(campaignId: string, run: JobRun): string {
  return resultsViewHash('run', campaignId, {
    level: 'run', configName: run.configName, runId: run.runId,
  })
}
