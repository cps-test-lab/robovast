// Which campaigns the list shows, given what the search panel holds.
//
// A struct rather than a bare string because the panel is meant to grow: a phase, an owner, a
// date range are all filters over the same list, and each one added as another argument would
// mean touching every call site again. Callers construct a `CampaignFilter`, extend it field by
// field, and `matchCampaigns` stays the one place that knows how the fields combine (AND — a
// filter narrows).
//
// Pure and here rather than in `Monitor`, because the frontend's tests only cover `lib/` (see
// docs/developer_guide.rst): which fields a typed word is matched against is a rule worth
// pinning, and an empty filter returning the SAME array is what keeps the memo downstream from
// re-rendering every card on a keystroke that changed nothing.

import type { CampaignSummary } from './robovastClient'

export interface CampaignFilter {
  /** Free text, matched case-insensitively against the campaign's identifying prose. */
  text: string
}

/** A filter that hides nothing — the state the panel opens in, and what closing it restores. */
export const NO_CAMPAIGN_FILTER: CampaignFilter = { text: '' }

/** Whether *filter* would show the whole list, so the header can say so without filtering. */
export function campaignFilterIsEmpty(filter: CampaignFilter): boolean {
  return !filter.text.trim()
}

/** The fields a typed word is matched against: what a card shows as its own name.
 *
 *  Deliberately not the phase or the mode. Those are single values out of a known set, and a
 *  set is a control of its own — matching them here would make "failed" typed into a free-text
 *  box quietly mean two different things once that control exists. */
function haystack(c: CampaignSummary): string {
  return `${c.campaign_id}\n${c.description}\n${c.created_by}`.toLowerCase()
}

/** Those of *campaigns* the filter admits, in the order they arrived.
 *
 *  An empty filter returns *campaigns* itself, not a copy: the list is re-derived on every
 *  stream push, and handing back a fresh array for a filter nobody set would defeat the
 *  memoisation of a view whose cards are not cheap. */
export function matchCampaigns(
  campaigns: CampaignSummary[],
  filter: CampaignFilter,
): CampaignSummary[] {
  if (campaignFilterIsEmpty(filter)) return campaigns
  // Split on whitespace so several words narrow rather than having to be typed contiguously:
  // an id fragment and a word from the description is the search somebody actually has.
  const terms = filter.text.trim().toLowerCase().split(/\s+/)
  return campaigns.filter((c) => {
    const hay = haystack(c)
    return terms.every((t) => hay.includes(t))
  })
}
