import { PRE_RUN_PHASES, RobovastError, isTerminalPhase } from './robovastClient'

/** A campaign freezes its project under `_config/` when its FIRST BATCH is prepared — after the
 *  controller has advanced to `running`, not when the campaign starts. So between those two points
 *  the prefix is genuinely not there yet, on either lane. */
const WHEN_STAGED =
  'A campaign freezes its configuration under _config/ when its first batch is prepared.'

/** What a failed listing of a campaign's `_config/` says about that campaign, beyond the error
 *  itself — `''` when it says nothing.
 *
 *  Absent data and a failed lookup are different answers and may not be reported as one: only a 404
 *  says the prefix is not there, and any other failure leaves what it holds unknown. Even a 404 is a
 *  fact about *now* rather than a verdict on the campaign, so which of the two absences it is turns
 *  on whether the campaign is still live — and where that is not known, this says neither.
 *
 *  *phase* is the campaign's phase, or undefined while it has not been read. */
export function campaignConfigNote(error: Error, phase: string | undefined): string {
  if (!(error instanceof RobovastError) || error.status !== 404) return ''
  if (phase === undefined) return WHEN_STAGED
  return isTerminalPhase(phase)
    ? `${WHEN_STAGED} This one ended before it did, so it has none to open.`
    : `${WHEN_STAGED} This one has not reached that point yet.`
}

/** Whether this campaign can have a frozen `_config/` at all yet — the gate on every link to
 *  one.
 *
 *  A run stages that snapshot when its FIRST BATCH is prepared; a campaign taken in from an
 *  archive has it once the bytes land, and is listed at `importing` from before the first of
 *  them arrives. Through those phases the address is provably not there, so nothing may offer
 *  it (a link to what a caller cannot reach is worse than no link).
 *
 *  A one-way gate, not a guarantee: the controller advances to `running` before that first
 *  batch is staged, and no bounded signal on the status payload separates the two — so what
 *  passes here can still be early, and `campaignConfigNote` is what says so honestly.
 */
export const mayHaveStagedConfig = (phase: string): boolean =>
  !PRE_RUN_PHASES.has(phase) && phase !== 'importing'
