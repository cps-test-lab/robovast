// What a running job is consuming, shaped for the two meters the Jobs list draws.
//
// Every number the row shows is decided here and none of it in the component, for the reason
// `lib/jobKind.ts` states: the repo tests pure modules and not markup, and the rules below are
// exactly the kind that go quietly wrong. A meter has three quantities in it -- measured,
// requested, limit -- and the interesting cases are the ones where some of them are missing.
//
// The shape it produces is the one `MeterBar` takes: the track is the ceiling, the fill is what
// is being used, and the marker is the reservation. So fill against marker reads as "did we
// reserve the right amount?" and fill against the track end as "is this near being throttled or
// OOM-killed?" -- both off one bar, without the reader doing arithmetic.

import type { Fact } from '@/components/HoverFacts'
import { formatBytes, formatBytesPair, formatCores } from './format'
import type { JobSummary } from './robovastClient'

/** One resource's meter: what to draw, what to write in it, and what the hover spells out. */
export interface UsageMeter {
  /** Share of the track that is filled. Clamped: a job may exceed its ceiling briefly. */
  fraction: number
  /** Where the reservation sits, 0..1, or `null` when the service did not state one. */
  marker: number | null
  /** The compact pair rendered inside the track. */
  text: string
  /** Full figures for the tooltip — the third number lives here, not in the track. */
  facts: Fact[]
  /** True when the track is the REQUEST because no limit was stated. */
  trackIsRequest: boolean
}

/** Both meters for one job, each `null` where there is nothing honest to draw. */
export interface JobMeters {
  cpu: UsageMeter | null
  memory: UsageMeter | null
}

function clamp01(x: number): number {
  return Math.min(1, Math.max(0, x))
}

/** One resource's meter, or `null` when it cannot be drawn honestly.
 *
 * Three ways it comes back `null`, and they are different facts that happen to render the same:
 * nothing was measured (an empty track reads as an idle job, which is a stronger claim than
 * "unmeasured"); neither a request nor a limit was stated, so there is no track to scale
 * against; or the track would be zero, which no fraction divides by.
 *
 * The limit is the track when there is one. Falling back to the request is deliberate and not a
 * fudge: an absent cpu limit means the container may use the whole node, so there is no finite
 * ceiling to draw, and a bar scaled to the reservation at least answers the sizing question.
 * `trackIsRequest` is returned so the caller can say which it is rather than let the reader
 * assume. */
function meter(
  measured: number | null | undefined,
  request: number | null | undefined,
  limit: number | null | undefined,
  format: (used: number, track: number) => string,
  one: (v: number) => string,
): UsageMeter | null {
  if (measured == null) return null
  const track = limit ?? request
  if (track == null || !(track > 0)) return null
  const trackIsRequest = limit == null
  return {
    fraction: clamp01(measured / track),
    // No marker when the request IS the track: a line hard against the right edge states
    // nothing the track's end does not already say.
    marker: request != null && !trackIsRequest ? clamp01(request / track) : null,
    text: format(measured, track),
    facts: [
      { label: 'using', value: one(measured) },
      { label: 'requested', value: request != null ? one(request) : null },
      { label: 'limit', value: limit != null ? one(limit) : null },
    ],
    trackIsRequest,
  }
}

/** The two meters for a job row. Both `null` on anything the service did not measure — which is
 * every job that is not running, and every lane that measures nothing. */
export function jobMeters(job: JobSummary): JobMeters {
  const u = job.usage
  if (!u) return { cpu: null, memory: null }
  return {
    cpu: meter(
      u.cpu_cores,
      u.cpu_request,
      u.cpu_limit,
      (used, track) => `${formatCores(used)}/${formatCores(track)}`,
      formatCores,
    ),
    memory: meter(
      u.memory_bytes,
      u.memory_request_bytes,
      u.memory_limit_bytes,
      // Both numbers scaled by the track's unit, so the pair reads as two magnitudes in one
      // unit rather than two labelled numbers in a ~110px track.
      formatBytesPair,
      formatBytes,
    ),
  }
}

/** Seconds this job has been going, or `null` when the service did not say when it started.
 *
 * Takes `now` so it is testable and so a caller with a fetch timestamp can use that instead of
 * the wall clock. Negative ages are clamped away: a browser clock a few seconds behind the
 * service should read as "just started", not as a countdown. */
export function jobAgeSeconds(job: JobSummary, now: number = Date.now()): number | null {
  if (job.started_at == null) return null
  return Math.max(0, now / 1000 - job.started_at)
}
