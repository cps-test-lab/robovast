import { isTerminalPhase, type CampaignSummary } from './robovastClient'

// Turning the campaign list into "something just happened".
//
// The list stream is the only signal there is -- the service pushes the whole list whenever it
// changes and has no per-campaign event vocabulary -- so a transition is a diff of two
// consecutive frames. The phase vocabulary itself is NOT restated here: `isTerminalPhase` is the
// one copy of it (three had already drifted apart once), and this asks it rather than listing
// phases of its own.

export type CampaignEventKind = 'started' | 'finished' | 'failed' | 'stopped'

export interface CampaignEvent {
  campaignId: string
  kind: CampaignEventKind
  /** The phase it landed in, for a caller that wants to say more than the kind does. */
  phase: string
  summary: CampaignSummary
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

/**
 * What changed between the phases in *prev* and the campaigns in *next*.
 *
 * `prev` is the caller's baseline, which it must keep across reconnects: the stream re-sends the
 * whole list on every new EventSource, and a baseline that reset with the connection would
 * re-announce every running campaign each time the socket blinked.
 *
 * A campaign absent from `prev` is **not** reported as started unless the caller has a baseline
 * at all -- see `seedPhases`, which is how the first frame is absorbed silently. Without that,
 * opening the app would announce every campaign in the list.
 */
export function diffCampaignPhases(
  prev: ReadonlyMap<string, string>,
  next: readonly CampaignSummary[],
): CampaignEvent[] {
  const events: CampaignEvent[] = []
  for (const c of next) {
    const before = prev.get(c.campaign_id)
    if (before === undefined || before === c.phase) continue

    const wasRunning = !isTerminalPhase(before)
    const isRunning = !isTerminalPhase(c.phase)

    if (!wasRunning && isRunning) {
      // Terminal -> live is a real start, and it happens to an existing campaign too: a
      // postprocessing rerun, a share upload or an import re-activates a finished one. It is
      // deliberately not suppressed -- the campaign really is working again.
      events.push({ campaignId: c.campaign_id, kind: 'started', phase: c.phase, summary: c })
      continue
    }
    if (wasRunning && !isRunning) {
      const kind = endedAs(c.phase)
      if (kind) events.push({ campaignId: c.campaign_id, kind, phase: c.phase, summary: c })
    }
  }
  return events
}

/** The phase map to carry into the next diff. */
export function seedPhases(next: readonly CampaignSummary[]): Map<string, string> {
  return new Map(next.map((c) => [c.campaign_id, c.phase]))
}

/** What to say about each kind of transition. */
export function describeCampaignEvent(evt: CampaignEvent): { message: string; note?: string } {
  const runs = evt.summary.num_runs
    ? `${evt.summary.num_runs} runs` +
      (evt.summary.num_failed ? ` · ${evt.summary.num_failed} failed` : '')
    : undefined
  switch (evt.kind) {
    case 'started':
      return { message: `Campaign started`, note: evt.campaignId }
    case 'finished':
      return { message: `Campaign finished`, note: [evt.campaignId, runs].filter(Boolean).join(' · ') }
    case 'stopped':
      return { message: `Campaign stopped`, note: [evt.campaignId, runs].filter(Boolean).join(' · ') }
    case 'failed':
      // The reason, when the listing carries one. Without it this notice could only name the
      // campaign -- "Campaign failed", and go and look -- because the failure reason lived in
      // the per-campaign Status, which this stream never fetches. `summary.error` is the first
      // line of it; the card has the rest.
      return {
        message: `Campaign failed`,
        note: [evt.campaignId, evt.summary.error].filter(Boolean).join(' · '),
      }
  }
}
