import { describe, expect, it } from 'vitest'
import { jobAgeSeconds, jobMeters } from './jobUsage'
import type { JobSummary } from './robovastClient'

const job = (over: Partial<JobSummary> = {}): JobSummary =>
  ({ job_name: 'j-1', kind: 'run', status: 'running', ...over }) as JobSummary

// The generated type marks every field required — `tools/dump_openapi.py` does that to
// response fields with defaults, because the server always serialises them — so a fixture
// states the whole record and each case overrides the part it is about. Absent is `null`
// here for the same reason it is on the wire: it means "not known", never zero.
const usage = (over: Partial<NonNullable<JobSummary['usage']>>) => ({
  cpu_cores: null,
  cpu_request: null,
  cpu_limit: null,
  memory_bytes: null,
  memory_request_bytes: null,
  memory_limit_bytes: null,
  ...over,
})

const GiB = 1024 ** 3

describe('jobMeters', () => {
  it('draws nothing for a job the service did not measure', () => {
    // Every job that is not running, and every lane that measures nothing per container.
    expect(jobMeters(job())).toEqual({ cpu: null, memory: null })
  })

  it('scales the track to the limit and marks the request inside it', () => {
    const { cpu } = jobMeters(
      job({ usage: usage({ cpu_cores: 1.2, cpu_request: 4.9, cpu_limit: 6.5 }) }),
    )
    expect(cpu?.fraction).toBeCloseTo(1.2 / 6.5)
    expect(cpu?.marker).toBeCloseTo(4.9 / 6.5)
    expect(cpu?.text).toBe('1.2/6.5')
    expect(cpu?.trackIsRequest).toBe(false)
  })

  it('keeps the request visible when the job is using more than it reserved', () => {
    // The case a `buffer` cannot show: painted under the fill, it would be swallowed exactly
    // when the second value is most worth seeing.
    const { cpu } = jobMeters(
      job({ usage: usage({ cpu_cores: 5, cpu_request: 1, cpu_limit: 6 }) }),
    )
    expect(cpu?.marker).toBeCloseTo(1 / 6)
    expect(cpu!.marker!).toBeLessThan(cpu!.fraction)
  })

  it('falls back to the request as the track when no limit was set', () => {
    // An open cpu limit means the whole node, so there is no finite ceiling to draw. A bar
    // scaled to the reservation still answers the sizing question; the flag is what lets the
    // caller say so instead of letting the reader assume a ceiling.
    const { cpu } = jobMeters(job({ usage: usage({ cpu_cores: 1, cpu_request: 4 }) }))
    expect(cpu?.fraction).toBeCloseTo(0.25)
    expect(cpu?.trackIsRequest).toBe(true)
    // No marker: a line hard against the right edge says nothing the track's end does not.
    expect(cpu?.marker).toBeNull()
  })

  it('draws nothing when neither a request nor a limit is known', () => {
    // There is no track to scale against, and a full bar would be a fabricated ceiling.
    expect(jobMeters(job({ usage: usage({ cpu_cores: 1.2 }) })).cpu).toBeNull()
  })

  it('clamps a job that has gone past its ceiling', () => {
    const { memory } = jobMeters(
      job({ usage: usage({ memory_bytes: 9 * GiB, memory_limit_bytes: 8 * GiB }) }),
    )
    expect(memory?.fraction).toBe(1)
  })

  it('decides cpu and memory independently', () => {
    // A missing cpu limit must not hide a memory ceiling that is known — they are separate
    // facts and the service reports them separately.
    const m = jobMeters(
      job({
        usage: usage({
          cpu_cores: 1,
          memory_bytes: 2 * GiB,
          memory_request_bytes: 4 * GiB,
          memory_limit_bytes: 4 * GiB,
        }),
      }),
    )
    expect(m.cpu).toBeNull()
    expect(m.memory?.text).toBe('2.0/4.0 GiB')
  })

  it('puts every figure on the hover, including the one the track cannot show', () => {
    const { memory } = jobMeters(
      job({
        usage: usage({
          memory_bytes: 3 * GiB,
          memory_request_bytes: 4 * GiB,
          memory_limit_bytes: 8 * GiB,
        }),
      }),
    )
    expect(memory?.facts).toEqual([
      { label: 'using', value: '3.0 GiB' },
      { label: 'requested', value: '4.0 GiB' },
      { label: 'limit', value: '8.0 GiB' },
    ])
  })
})

describe('jobAgeSeconds', () => {
  it('is null when the service did not say when the job started', () => {
    // Absent, never zero: an epoch-zero start renders as decades of runtime.
    expect(jobAgeSeconds(job())).toBeNull()
  })

  it('measures from the start the service reported', () => {
    expect(jobAgeSeconds(job({ started_at: 1_000 }), 1_120_000)).toBe(120)
  })

  it('reads a clock skewed behind the service as just started, not as a countdown', () => {
    expect(jobAgeSeconds(job({ started_at: 2_000 }), 1_000_000)).toBe(0)
  })
})
