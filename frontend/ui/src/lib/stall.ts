// Has this campaign's run progress stopped advancing, and may we say so?
//
// A PARALLEL IMPLEMENTATION of `stall_report` in the Python status contract
// (`robovast/client/status.py`), and deliberately named as one. The REST status endpoint
// serves the raw `Status` model, so the derived verdict is not on the wire and the card has
// to re-derive it here. The two must gate identically, in the same order.
//
// It lives in its own module for one reason: the same drift has now happened three times,
// each time because the derivation sat inline in the card where nothing tested it. A phase
// that runs no runs was judged against a per-run budget and painted a red "stalled" on a
// campaign that spent half an hour postprocessing correctly; excluding only the pre-run
// phases was that rule half-applied; and a batch queued for cluster capacity was called
// stalled while the MCP and CLI both refused to. Each was fixed by adding one more
// condition to an untested copy. Whoever adds a gate to `stall_report` must add it here,
// and `stall.test.ts` is what makes that visible.

import type { Status } from './robovastClient'

/** The verdict, plus the clock it was made against.
 *
 * `stalled` is TRI-STATE, matching the status contract: `true` past the declared budget,
 * `false` inside it, `null` when no verdict is possible. Only `true` may render — showing
 * `null` as "not stalled" would put a reassuring label on a run that may already be dead.
 *
 * `ageS` is reported even when the verdict is suppressed, for the same reason the server
 * reports it: suppressing a verdict must not hide how long the wait has been, which is the
 * number an operator acts on. */
export interface StallVerdict {
  stalled: boolean | null
  ageS: number | null
}

const NO_VERDICT: StallVerdict = { stalled: null, ageS: null }

/** Derive the stall verdict, mirroring `stall_report`'s gates in its order.
 *
 * Gates, and why each one refuses rather than answering:
 *
 *  1. No `progress_since`, or a TERMINAL campaign — its progress stopped advancing because
 *     it is over, which is not a stall. Subsumed by gate 2 (`finished` is not `running`),
 *     kept distinct only because the contract states it separately.
 *  2. Any live phase OTHER than `running`. The budget is per-*run* and so is the signal it
 *     measures: `progress_since` restarts when a phase begins and nothing outside `running`
 *     can advance it, so the clock could only ever run out. Passing it would say no more
 *     than "this phase outlasted one run" — arithmetic, not a stall.
 *  3. `waiting_for_capacity` — every job of the batch is queued for capacity the campaign
 *     does not control, so no run is running and none CAN complete. Same argument as gate 2,
 *     applied inside `running`. This is the gate that was missing.
 *  4. No declared `execution.timeout`: no budget to compare against, so no verdict. Never a
 *     substituted default — the cluster's force-kill exists so a run cannot hang forever,
 *     which is a fine reason to kill at an hour and a terrible reason to call a two-minute
 *     pilot healthy for the first fifty-nine.
 *
 * The clock is read with `Date.now()`, as `eta.ts` does. That compares the browser's clock
 * against a server timestamp, so skew shifts the age — unavoidable while the verdict is
 * derived client-side, since the payload carries an absolute `progress_since` and no server
 * `now`. Serving the verdict from the server is the only real fix and is a separate change. */
export function stallVerdict(status: Status | undefined): StallVerdict {
  if (!status?.progress_since) return NO_VERDICT
  if (status.phase !== 'running') return NO_VERDICT
  const ageS = Math.max(0, Date.now() / 1000 - status.progress_since)
  if (status.waiting_for_capacity) return { stalled: null, ageS }
  // Falsy rather than null-checked, mirroring the contract's `if not deadline`: a zero
  // budget is not a budget.
  if (!status.progress_deadline_s) return { stalled: null, ageS }
  return { stalled: ageS > status.progress_deadline_s, ageS }
}
