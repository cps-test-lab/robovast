import { describe, expect, it, vi, afterEach } from 'vitest'
import type { Status } from './robovastClient'
import { stallVerdict } from './stall'

const NOW = 1_700_000_000_000
const NO_VERDICT_SHAPE = { stalled: null, ageS: null }

/** A live `running` status whose progress last advanced `ageS` seconds ago. */
function status(ageS: number, over: Partial<Status> = {}): Status {
  return {
    phase: 'running',
    progress_since: NOW / 1000 - ageS,
    progress_deadline_s: 600,
    waiting_for_capacity: false,
    ...over,
  } as Status
}

afterEach(() => vi.useRealTimers())
const freeze = () => vi.useFakeTimers({ now: NOW })

// One case per gate of `stall_report` (robovast/client/status.py), in its order. A gate
// added there needs one added here -- that is what this file is for.
describe('stallVerdict', () => {
  it('asserts a stall past the declared per-run budget', () => {
    freeze()
    expect(stallVerdict(status(900)).stalled).toBe(true)
  })

  it('is false inside the declared budget', () => {
    freeze()
    expect(stallVerdict(status(300)).stalled).toBe(false)
  })

  // THE REGRESSION. Ten campaigns launched together against one lane leaves the tail of
  // the group queued for longer than one run's budget, and the card called that stalled
  // while the MCP and CLI both reported "queued for cluster capacity". No run is running,
  // so none can complete: the per-run budget has nothing to measure.
  it('refuses a verdict while the whole batch is queued for capacity', () => {
    freeze()
    expect(stallVerdict(status(900, { waiting_for_capacity: true })).stalled).toBeNull()
  })

  // Suppressing the verdict must not hide how long the wait has been -- that is the number
  // an operator acts on, and the server reports it for the same reason.
  it('still reports the age while queued', () => {
    freeze()
    expect(stallVerdict(status(900, { waiting_for_capacity: true })).ageS).toBe(900)
  })

  // The budget is per-RUN, and so is the signal it measures: `progress_since` restarts when
  // a phase begins and nothing outside `running` advances it. Converting a large campaign's
  // rosbags legitimately outlasts any single run, and this painted it red for the half-hour
  // it spent working correctly.
  it.each(['postprocessing', 'sharing', 'finishing', 'building', 'variation'])(
    'refuses a verdict in %s, where no run executes',
    (phase) => {
      freeze()
      expect(stallVerdict(status(9000, { phase })).stalled).toBeNull()
    },
  )

  // Progress stopped advancing because the campaign is over, which is not a stall.
  it.each(['finished', 'failed', 'stopped', 'crashed', 'unknown'])(
    'refuses a verdict on a terminal campaign (%s)',
    (phase) => {
      freeze()
      expect(stallVerdict(status(9000, { phase }))).toEqual({ stalled: null, ageS: null })
    },
  )

  // No budget to compare against. Never a substituted default: the cluster's force-kill
  // exists so a run cannot hang forever, not to certify a two-minute pilot healthy.
  it.each([null, 0])('refuses a verdict with no declared timeout (%s)', (deadline) => {
    freeze()
    const s = status(9000, { progress_deadline_s: deadline as number | null })
    expect(stallVerdict(s).stalled).toBeNull()
  })

  it('refuses a verdict on a status with no progress clock', () => {
    freeze()
    expect(stallVerdict(status(900, { progress_since: 0 }))).toEqual(NO_VERDICT_SHAPE)
  })

  it('refuses a verdict on no status at all', () => {
    expect(stallVerdict(undefined)).toEqual(NO_VERDICT_SHAPE)
  })

  // The clock cannot run backwards into a negative age if the browser's is behind the
  // server's -- `formatDuration` would render that as a nonsense label.
  it('clamps the age at zero when the browser clock is behind the server', () => {
    freeze()
    expect(stallVerdict(status(-120)).ageS).toBe(0)
  })
})
