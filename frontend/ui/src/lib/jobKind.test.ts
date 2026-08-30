// The one thing here that is not obvious from reading `jobKind.ts`: `kind` is typed as a required
// string, so nothing but a test can pin what happens when it is absent -- which is what a service
// older than the field sends, and the case that decides whether an old deployment's whole job list
// renders as calibration probes.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import type { JobSummary } from './robovastClient'
import { calibrationFirst, isCalibrationJob } from './jobKind'

function job(job_name: string, kind?: string): JobSummary {
  // `kind` omitted models a service older than the field, which the generated type says cannot
  // happen; the cast is the point of the test rather than a convenience.
  return { job_name, status: 'running', display_name: null, detail: null,
           ...(kind === undefined ? {} : { kind }) } as unknown as JobSummary
}

describe('isCalibrationJob', () => {
  it('is true only for a calibration probe', () => {
    expect(isCalibrationJob(job('a', 'calibration'))).toBe(true)
    expect(isCalibrationJob(job('b', 'run'))).toBe(false)
  })

  it('reads a job from a service older than the field as a run', () => {
    expect(isCalibrationJob(job('c'))).toBe(false)
  })

  it('reads an unfamiliar kind as a run, not as a probe', () => {
    expect(isCalibrationJob(job('d', 'something-later'))).toBe(false)
  })
})

describe('calibrationFirst', () => {
  it('hoists probes and leaves the runs in the order the service returned them', () => {
    const jobs = [job('r1', 'run'), job('p1', 'calibration'), job('r2', 'run'),
                  job('p2', 'calibration'), job('r3', 'run')]
    expect(calibrationFirst(jobs).map((j) => j.job_name))
      .toEqual(['p1', 'p2', 'r1', 'r2', 'r3'])
  })

  it('does not mutate its argument', () => {
    const jobs = [job('r1', 'run'), job('p1', 'calibration')]
    calibrationFirst(jobs)
    expect(jobs.map((j) => j.job_name)).toEqual(['r1', 'p1'])
  })
})
