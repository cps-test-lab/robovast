import type { CampaignOrigin } from '@/lib/robovastClient'

/**
 * What a campaign's origin hover lists.
 *
 * Here rather than in the component because these are decisions, not markup: which rows
 * there are, in what order, and what counts as a re-run. They are also the parts a
 * regression would break silently.
 */

/** A label→value pair for the hover. An empty value means the row is not shown. */
export type OriginFact = { label: string; value: string }

/** True for a re-run of an earlier campaign. */
export function isRerun(origin: CampaignOrigin): boolean {
  // `kind` is the single authority. Deliberately NOT derived from `from_campaign` being
  // set: it happens to imply a re-run today, and would be wrong the day a third kind exists.
  return origin.kind === 'retrigger'
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
