// The order the campaign list is asked for in. Its own module, free of any request code, because
// the URL hash carries it too (see hashNav) and that grammar has to stay testable on its own.

import type { operations } from './api.generated'

type ListQuery = NonNullable<operations['list_campaigns_campaigns_get']['parameters']['query']>

/** The service's `sort` and `order` query parameters, typed from its own schema so the two
 *  vocabularies cannot drift. Only the service can apply them, because it orders the whole list
 *  before it cuts the page. */
export interface CampaignListSort {
  sort: NonNullable<ListQuery['sort']>
  order: NonNullable<ListQuery['order']>
}

/** The service's own default: newest first. */
export const DEFAULT_CAMPAIGN_SORT: CampaignListSort = { sort: 'recent', order: 'desc' }

export function isDefaultCampaignSort(s: CampaignListSort): boolean {
  return s.sort === DEFAULT_CAMPAIGN_SORT.sort && s.order === DEFAULT_CAMPAIGN_SORT.order
}

/** The query string for *s*, empty for the default so the default address stays the plain one. */
export function campaignSortQuery(s: CampaignListSort): string {
  return isDefaultCampaignSort(s) ? '' : `sort=${s.sort}&order=${s.order}`
}

/** The order a query names, or null when either value is outside the vocabulary.
 *
 *  Null rather than the default: a caller that got something it cannot read decides what to do
 *  about it, instead of being handed an order nobody asked for as though it had been. An absent
 *  parameter is not such a case -- it means the default, as it does to the service. */
export function campaignSortFromQuery(query: URLSearchParams): CampaignListSort | null {
  const sort = query.get('sort') ?? DEFAULT_CAMPAIGN_SORT.sort
  const order = query.get('order') ?? DEFAULT_CAMPAIGN_SORT.order
  if (sort !== 'recent' && sort !== 'size') return null
  if (order !== 'desc' && order !== 'asc') return null
  return { sort, order }
}

/** The four orders the campaign view offers, each with the words it is offered in. */
export const CAMPAIGN_SORT_CHOICES: readonly { sort: CampaignListSort; label: string }[] = [
  { sort: { sort: 'recent', order: 'desc' }, label: 'Newest first' },
  { sort: { sort: 'recent', order: 'asc' }, label: 'Oldest first' },
  { sort: { sort: 'size', order: 'desc' }, label: 'Largest first' },
  { sort: { sort: 'size', order: 'asc' }, label: 'Smallest first' },
]

/** A stable key for *s*, for a select's value. */
export function campaignSortKey(s: CampaignListSort): string {
  return `${s.sort}-${s.order}`
}
