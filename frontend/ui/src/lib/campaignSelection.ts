// The campaign list's selection mode: which campaigns may be picked for a multi-campaign delete,
// and how the service's per-id answer is read back. Pure, so the rules are testable without the
// page that renders them.

import {
  isTerminalPhase,
  type CampaignDeletion,
  type CampaignSummary,
} from '@/lib/robovastClient'

// A running campaign is refused by the service, so it is not offered: a checkbox that can only
// produce a refusal is a control that lies about what it does. The service still decides — a
// campaign that starts again between the pick and the delete comes back as `running`.
export function isSelectable(summary: CampaignSummary): boolean {
  return isTerminalPhase(summary.phase)
}

// What is still selected once the list has moved on: a campaign that disappeared (deleted
// elsewhere) or became busy again (a retriggered postprocessing, an upload-to-share) drops out,
// so the count on the delete action is always the count that would be sent. Returns the same set
// when nothing changed, so a caller can skip the state update.
export function pruneSelection(
  selected: ReadonlySet<string>,
  campaigns: readonly CampaignSummary[],
): ReadonlySet<string> {
  const selectable = new Set(campaigns.filter(isSelectable).map((c) => c.campaign_id))
  const kept = [...selected].filter((cid) => selectable.has(cid))
  return kept.length === selected.size ? selected : new Set(kept)
}

// The ids to send, in the order the list shows them — which is the order the results come back
// in, so the result list reads top to bottom like the list it was picked from.
export function selectedInListOrder(
  selected: ReadonlySet<string>,
  campaigns: readonly CampaignSummary[],
): string[] {
  return campaigns.map((c) => c.campaign_id).filter((cid) => selected.has(cid))
}

export const OUTCOME_LABEL: Record<CampaignDeletion['outcome'], string> = {
  deleted: 'deleted',
  not_found: 'already gone',
  partial: 'not fully deleted',
  running: 'refused: still running',
  invalid: 'refused: not a campaign id',
}

// One line for the whole answer. `ok` is the service's own verdict per id (nothing of it is left
// here), so a `not_found` counts with the deleted ones rather than as a failure.
export function deletionSummary(results: readonly CampaignDeletion[]): {
  ok: number
  failed: number
  text: string
} {
  const ok = results.filter((r) => r.ok).length
  const failed = results.length - ok
  const text = failed
    ? `${ok} of ${results.length} deleted, ${failed} not`
    : `${ok} campaign${ok === 1 ? '' : 's'} deleted`
  return { ok, failed, text }
}
