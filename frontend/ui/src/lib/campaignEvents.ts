import { isTerminalPhase, type CampaignSummary } from './robovastClient'

// Turning the campaign list into "something just happened".
//
// The list stream is the only signal there is -- the service pushes the whole list whenever it
// changes and has no per-campaign event vocabulary -- so a transition is a diff of two
// consecutive frames. The phase vocabulary itself is NOT restated here: `isTerminalPhase` is the
// one copy of it (three had already drifted apart once), and this asks it rather than listing
// phases of its own.

export type CampaignEventKind = 'started' | 'finished' | 'failed' | 'stopped'

/**
 * What the campaign is busy *with*, which is not the same question as which phase it is in.
 *
 * A finished campaign is re-activated by a share upload, a postprocessing rerun or an import, and
 * each of those is an operation on the campaign rather than the campaign running again -- so
 * announcing them as "Campaign started / finished" both misnames the work and reprints the old
 * run tally as if it were the operation's result.
 */
export type CampaignActivity = 'campaign' | 'export' | 'postprocessing' | 'import'

export interface CampaignEvent {
  campaignId: string
  kind: CampaignEventKind
  /** The phase it landed in, for a caller that wants to say more than the kind does. */
  phase: string
  activity: CampaignActivity
  summary: CampaignSummary
}

/**
 * The per-campaign baseline: its last seen phase, and the phase its current live spell began at.
 *
 * `entry` is what distinguishes the activities, and it cannot be read off a single transition. A
 * re-trigger enters the running set directly at `sharing`/`postprocessing`/`importing`, but a
 * campaign ALSO ends `sharing -> finished` (the controller shares before it finishes), so the
 * previous phase alone would relabel every real campaign ending as an export. It is undefined
 * while the campaign is terminal, and while a spell that was already under way when we first saw
 * it continues -- see `activityOf`.
 */
export interface CampaignSpell {
  phase: string
  entry?: string
}

/** Phase -> what to call it. `crashed` is a failure by another name. */
function endedAs(phase: string): CampaignEventKind | null {
  if (phase === 'finished') return 'finished'
  if (phase === 'stopped') return 'stopped'
  if (phase === 'failed' || phase === 'crashed') return 'failed'
  // `unknown` is terminal, and is NOT an ending. It is what a campaign reconstructs to after a
  // service restart that lost its live driver, so announcing it would turn one restart into a
  // burst of "campaign ended" notices for campaigns that ended long ago -- or are still running
  // somewhere the service can no longer see. Silence is the honest answer to "I lost track".
  return null
}

/** The running phases a campaign only ever enters a live spell AT when the spell is not a run. */
const ACTIVITY_BY_ENTRY: Readonly<Record<string, CampaignActivity>> = {
  sharing: 'export',
  postprocessing: 'postprocessing',
  importing: 'import',
}

/**
 * An unknown or absent entry phase means `campaign`, deliberately.
 *
 * That is the case where the app opened mid-spell: the first frame seeds silently, so the entry
 * was never observed. Falling back to the generic wording is both honest and exactly what this
 * said before there was an activity at all -- and it is the safe direction if the list stream
 * ever coalesces frames and skips the entry phase entirely.
 */
function activityOf(entry: string | undefined): CampaignActivity {
  return (entry && ACTIVITY_BY_ENTRY[entry]) || 'campaign'
}

/**
 * What changed between the phases in *prev* and the campaigns in *next*, and the baseline to
 * carry into the next call.
 *
 * The two come back together because they have to agree about `entry`: a diff and a separately
 * computed baseline would drift the moment one of them learned something the other did not.
 *
 * `prev` is the caller's baseline, which it must keep across reconnects: the stream re-sends the
 * whole list on every new EventSource, and a baseline that reset with the connection would
 * re-announce every running campaign each time the socket blinked.
 *
 * A campaign absent from `prev` is **not** reported as started unless the caller has a baseline
 * at all -- see `seedPhases`, which is how the first frame is absorbed silently. Without that,
 * opening the app would announce every campaign in the list.
 */
export function trackCampaignPhases(
  prev: ReadonlyMap<string, CampaignSpell>,
  next: readonly CampaignSummary[],
): { events: CampaignEvent[]; baseline: Map<string, CampaignSpell> } {
  const events: CampaignEvent[] = []
  const baseline = new Map<string, CampaignSpell>()

  for (const c of next) {
    const before = prev.get(c.campaign_id)
    const isRunning = !isTerminalPhase(c.phase)
    const wasRunning = before !== undefined && !isTerminalPhase(before.phase)

    // A spell keeps the entry it began at for as long as it stays live; a new one takes the
    // phase it entered at, which is the whole discriminator.
    const entry = isRunning ? (wasRunning ? before.entry : c.phase) : undefined
    baseline.set(c.campaign_id, { phase: c.phase, entry })

    if (before === undefined || before.phase === c.phase) continue

    if (!wasRunning && isRunning) {
      // Terminal -> live is a real start, and it happens to an existing campaign too: a
      // postprocessing rerun, a share upload or an import re-activates a finished one. It is
      // deliberately not suppressed -- the campaign really is working again -- but it is
      // announced as the operation it is rather than as a campaign run.
      events.push({
        campaignId: c.campaign_id, kind: 'started', phase: c.phase,
        activity: activityOf(entry), summary: c,
      })
      continue
    }
    if (wasRunning && !isRunning) {
      const kind = endedAs(c.phase)
      // The ending is named after how its spell BEGAN, not the phase it passed through last.
      if (kind) {
        events.push({
          campaignId: c.campaign_id, kind, phase: c.phase,
          activity: activityOf(before.entry), summary: c,
        })
      }
    }
  }
  return { events, baseline }
}

/**
 * The baseline for the first frame of a session, absorbed silently.
 *
 * No campaign gets an `entry`: whatever any of them is doing, it started before we were looking.
 */
export function seedPhases(next: readonly CampaignSummary[]): Map<string, CampaignSpell> {
  return new Map(next.map((c) => [c.campaign_id, { phase: c.phase }]))
}

/** How each activity names itself, and where it records why it failed. */
const ACTIVITY_NOUN: Readonly<Record<CampaignActivity, string>> = {
  campaign: 'Campaign',
  export: 'Export',
  postprocessing: 'Postprocessing',
  import: 'Import',
}

function reasonFor(evt: CampaignEvent): string | null | undefined {
  if (evt.activity === 'export') return evt.summary.share_error ?? evt.summary.error
  if (evt.activity === 'postprocessing') return evt.summary.postprocessing_error ?? evt.summary.error
  return evt.summary.error
}

/** What to say about each kind of transition. */
export function describeCampaignEvent(evt: CampaignEvent): { message: string; note?: string } {
  const noun = ACTIVITY_NOUN[evt.activity]
  // The run tally belongs to the CAMPAIGN. On an export or a rerun it is the campaign's old
  // count, which read as that operation's result -- "16 runs · 10 failed" for an upload that
  // ran nothing at all.
  const runs = evt.activity === 'campaign' && evt.summary.num_runs
    ? `${evt.summary.num_runs} runs` +
      (evt.summary.num_failed ? ` · ${evt.summary.num_failed} failed` : '')
    : undefined
  switch (evt.kind) {
    case 'started':
      return { message: `${noun} started`, note: evt.campaignId }
    case 'finished':
      return { message: `${noun} finished`, note: [evt.campaignId, runs].filter(Boolean).join(' · ') }
    case 'stopped':
      return { message: `${noun} stopped`, note: [evt.campaignId, runs].filter(Boolean).join(' · ') }
    case 'failed':
      // The reason, when the listing carries one. Without it this notice could only name the
      // campaign -- "Campaign failed", and go and look -- because the failure reason lived in
      // the per-campaign Status, which this stream never fetches. The summary's error is the
      // first line of it; the card has the rest.
      return {
        message: `${noun} failed`,
        note: [evt.campaignId, reasonFor(evt)].filter(Boolean).join(' · '),
      }
  }
}
