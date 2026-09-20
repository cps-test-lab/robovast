// The preview log's addressing contract: a preview run row -> the job whose log the service serves.
//
// Tested as a pure function for the same reason `previewRuns.test.ts` tests the row shape: nothing
// type-checks the agreement between the two ends. The service's job-log endpoint takes a
// `job_name` of `<config>/<run>` (`LocalService.get_job_log`, and the job-link manifest keyed
// `<config>/<run>/job`), and the preview tree already carries exactly those two fields. If either
// side changes its spelling, a preview log 404s at runtime with nothing pointing at why.
//
// Scope is deliberately narrow -- see the testing convention in docs/developer_guide.rst.

import { describe, expect, it } from 'vitest'
import { jobNameOf } from './PreviewRunLog'
import { previewRunRows } from '../previewRuns'

describe('jobNameOf', () => {
  it('addresses a job the way the service names one', () => {
    expect(jobNameOf('nominal', 3)).toBe('nominal/3')
  })

  it('names the job of every row the preview tree offers', () => {
    // Built from the rows themselves rather than from literals: this is the pairing that has to
    // hold, and restating the config name here would let the two drift together.
    const rows = previewRunRows(new Map([['fast', [0, 2]], ['slow', [1]]]))

    expect(rows.map((r) => jobNameOf(String(r.config_name), Number(r.run_id))))
      .toEqual(['fast/0', 'fast/2', 'slow/1'])
  })
})
