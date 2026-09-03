// Which of a campaign's listed jobs are its own runs, and which only look like them.
//
// The cluster lane lists two other things beside the campaign's trials: node-calibration probes,
// and the postprocessing conversion that turns its rosbags into CSV. Both are real work holding
// real capacity, so every selector that counts or cleans up has to keep seeing them, and both
// reach the job listing through the same path. Neither is one of the campaign's runs -- the
// service keeps both out of `JobCounts` for that reason -- and the Jobs list has to say so, or a
// probe reads as a duplicate row carrying another job's name and a conversion reads as a run.
//
// Markup stays in `components/`; this is the same split `lib/eta.ts` and `lib/detailsGeometry.ts`
// already make, and the reason those have tests while the components do not.

import type { JobSummary } from './robovastClient'

/** Whether a row is a node-calibration probe rather than one of the campaign's trials.
 *
 * Tests for the calibration value, and deliberately NOT for `!== 'run'`. `kind` is generated as a
 * required string -- `tools/dump_openapi.py` marks defaulted response fields required, because the
 * server always serializes them -- but a service older than the field sends none at all, so the
 * value is `undefined` at runtime there. Written the other way round, every job of every older
 * deployment would render as a probe. Open vocabulary either way: an unfamiliar kind is not one. */
export function isCalibrationJob(job: JobSummary): boolean {
  return job.kind === 'calibration'
}

/** Whether a row is the campaign's postprocessing conversion rather than one of its trials.
 *
 * Same closed test as `isCalibrationJob`, for the same reason: a service that predates the kind
 * sends none, and an unfamiliar kind is not this one. */
export function isPostprocessingJob(job: JobSummary): boolean {
  return job.kind === 'postprocessing'
}

/** Whether a row is something the campaign is doing other than one of its trials. */
export function isNonRunJob(job: JobSummary): boolean {
  return isCalibrationJob(job) || isPostprocessingJob(job)
}

/** The jobs with everything that is not a trial hoisted to the front, the rest in the order it
 * arrived.
 *
 * The listing is capped (`JOBS_RENDER_CAP`), and a wide batch is exactly the case where a probe is
 * both the reason nothing has started yet and the row that falls off the end. There is at most one
 * probe per node and one conversion per campaign, so hoisting them costs the runs nothing, and
 * `Array#sort` is stable, so the runs keep the order the service returned them in. */
export function nonRunsFirst(jobs: JobSummary[]): JobSummary[] {
  return [...jobs].sort((a, b) => Number(isNonRunJob(b)) - Number(isNonRunJob(a)))
}
