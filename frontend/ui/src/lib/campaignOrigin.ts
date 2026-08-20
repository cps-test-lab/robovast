import type { CampaignOrigin } from '@/lib/robovastClient'

/**
 * How a campaign's origin reads on a card, and what its hover lists.
 *
 * Here rather than in the component because these are decisions, not markup: which field
 * wins, what a re-run is called, and when there is nothing honest to show. They are also
 * the parts a regression would break silently.
 */

/** A label→value pair for the hover. An empty value means the row is not shown. */
export type OriginFact = { label: string; value: string }

/** True for a re-run of an earlier campaign. */
export function isRerun(origin: CampaignOrigin): boolean {
  // `kind` is the single authority. Deliberately NOT derived from `from_campaign` being
  // set: it happens to imply a re-run today, and would be wrong the day a third kind exists.
  return origin.kind === 'retrigger'
}

/**
 * The short label for the card, or `''` when there is nothing worth showing.
 *
 * A re-run is named after what it came from — that is the answer to "where is this from?"
 * for a campaign that was not launched from a workspace at all. Everything else is
 * `<workspace> / <file>`, using the file's basename because the card is a scanning
 * surface; the full path is one hover away.
 */
export function originLabel(origin?: CampaignOrigin | null): string {
  if (!origin) return ''
  if (isRerun(origin)) {
    return origin.from_campaign ? `rerun of ${origin.from_campaign}` : ''
  }
  // The name is what a person recognises; the id is the fallback for a workspace whose
  // name was never recorded, which beats showing the file with no context at all.
  const workspace = origin.workspace_name || origin.workspace_id
  const file = origin.config_path.split('/').pop() ?? ''
  return [workspace, file].filter(Boolean).join(' / ')
}

/** The rows of the hover panel, in reading order. Empty values are dropped downstream. */
export function originFacts(origin: CampaignOrigin): OriginFact[] {
  return [
    { label: 'Rerun of', value: isRerun(origin) ? origin.from_campaign : '' },
    { label: 'Workspace', value: origin.workspace_name },
    { label: 'ID', value: origin.workspace_id },
    { label: 'File', value: origin.config_path },
  ]
}
