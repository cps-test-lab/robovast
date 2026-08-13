// Cross-page navigation, for the cases that cross pages: a campaign card jumping into a Results
// view, or into its own frozen config. Everything else navigates through App's own `select`.
//
// The hash grammar is hashNav's (App parses it and owns the Nav state); these only write one, and
// App's existing `hashchange` listener picks it up. Assigning `location.hash` *pushes* a history
// entry, which is what makes Back return to the campaign list in one press — in-view campaign
// changes replaceState instead, so they never pile up steps between here and there.

import { CAMPAIGN_SEGMENT } from './hashNav'

export type ResultsViewId = 'explorer' | 'run' | 'data'

export function openResultsView(view: ResultsViewId, campaignId: string): void {
  window.location.hash = `#/results/${view}/${campaignId}`
}

/** Open the Config view on a campaign's frozen `_config/`, read-only.
 *
 *  The only way in: that config is not a registered workspace and never appears in the workspace
 *  picker, so this link is the entire access path (see `Nav.configCampaignId`). */
export function openCampaignConfig(campaignId: string): void {
  window.location.hash = `#/config/${CAMPAIGN_SEGMENT}/${campaignId}`
}
