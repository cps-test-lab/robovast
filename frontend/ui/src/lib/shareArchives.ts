// What the share's listing means to the import dialog: one row per campaign, and which of
// that campaign's archives a click on Import will actually fetch.
//
// A campaign can be on the share TWICE. The campaign-end upload runs before postprocessing
// and writes `<id>.raw.tar.gz`; a later export writes `<id>.postprocessed.tar.gz`, and
// nothing removes the first. Two rows differing only by a small chip is a misclick waiting
// to happen, so they collapse into one and the row states which archive it means.
//
// That statement has to be kept, which is why a row carries the archive's NAME and not just
// its campaign id: the service resolves either, but resolving an id returns whichever of the
// two archives its listing hits first — so a row promising `postprocessed` could quietly
// fetch the raw one and leave the campaign running a postprocessing pass nobody asked for.
//
// Pure and here rather than in the dialog because the frontend's tests only cover `lib/`
// (see docs/developer_guide.rst): a rule with two archives, two orderings and a preference
// in it is exactly the kind that has to be testable.

import type { ShareArchive } from './robovastClient'

/** The complete archive, so a campaign that has one arrives ready to query. */
const POSTPROCESSED = 'postprocessed'

export interface ShareCampaignRow {
  campaignId: string
  /** The variant that will be imported: `postprocessed` where the share has one, else `raw`. */
  variant: string
  size: number
  /** That archive's basename — what an import names, so the row cannot promise one variant
   *  and fetch another. Basename rather than the object name because a provider may prefix
   *  its keys (GCS does) and the archive-name parser refuses anything with a separator. */
  archive: string
  /** This deployment already has the campaign, so there is nothing to import. */
  present: boolean
}

/** Which of *a* and *b* an import should take: the postprocessed one, else either.
 *
 *  Exported because the campaign card's "copy share link" asks the same question of the same
 *  listing, and two answers to it would mean the link a card copies and the archive the
 *  dialog imports could be different files. */
export function preferredArchive(a: ShareArchive, b: ShareArchive): ShareArchive {
  return b.variant === POSTPROCESSED ? b : a
}

/** The archive an import names: the object's own basename. */
export function archiveName(a: ShareArchive): string {
  return a.object_name.split('/').pop() ?? a.object_name
}

/** One row per campaign, in the order *archives* arrived.
 *
 *  Deliberately not re-sorted. The service answers newest-campaign-first, keyed on the
 *  timestamp inside each campaign id; re-deriving that here would be the same rule written a
 *  second time, in a second language, free to disagree with the first. */
export function shareRows(
  archives: ShareArchive[],
  presentIds: Set<string>,
): ShareCampaignRow[] {
  const chosen = new Map<string, ShareArchive>()
  for (const a of archives) {
    const seen = chosen.get(a.campaign_id)
    chosen.set(a.campaign_id, seen ? preferredArchive(seen, a) : a)
  }
  return [...chosen.values()].map((a) => ({
    campaignId: a.campaign_id,
    variant: a.variant,
    size: a.size,
    archive: archiveName(a),
    present: presentIds.has(a.campaign_id),
  }))
}

/** Those of *rows* whose campaign id contains *query*, case-insensitively.
 *
 *  Separate from `shareRows` rather than an argument to it, because the dialog says "showing
 *  N of M" and a filtering call can only answer N — recovering M would mean running the
 *  filter a second time to ask a question about the unfiltered set. */
export function matchRows(rows: ShareCampaignRow[], query: string): ShareCampaignRow[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return rows
  return rows.filter((r) => r.campaignId.toLowerCase().includes(needle))
}
