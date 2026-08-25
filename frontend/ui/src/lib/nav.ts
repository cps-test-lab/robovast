// Cross-page navigation, for the cases that cross pages: a campaign card jumping into a Results
// view, one Results view handing its selected node to another, or a card opening its own frozen
// config.  Everything else navigates through App's own `select`.
//
// The hash grammar is hashNav's (App parses it and owns the Nav state); these only write one, and
// App's existing `hashchange` listener picks it up. Assigning `location.hash` *pushes* a history
// entry, which is what makes Back return to the campaign list — or to the view you came from — in
// one press; in-view selection changes replaceState instead, so they never pile up steps between
// here and there.

import { CAMPAIGN_SEGMENT, hashFor, type Nav, type ResultsSel, type ResultsViewId } from './hashNav'

export type { ResultsViewId }

/** Open a Results view on a campaign, optionally on one node of it.
 *
 *  Spelled through `hashFor` rather than a template literal so there is one grammar and not two:
 *  which segments a node adds, and which of them a given view is allowed to carry, are decisions
 *  that belong in `hashNav` and would otherwise be duplicated here to drift later. */
export function openResultsView(
  view: ResultsViewId,
  campaignId: string,
  sel: ResultsSel = { level: 'campaign' },
): void {
  const nav: Nav = {
    topicId: 'results',
    viewId: view,
    campaignId,
    sel,
    // The destination view decides its own tab; a node arriving from elsewhere brings none. (The
    // Run view has no notebook tabs at all, so there would be nothing to bring.)
    tab: '',
    configCampaignId: '',
  }
  window.location.hash = `#${hashFor(nav)}`
}

/** Open the Config view on a campaign's frozen `_config/`, read-only.
 *
 *  The only way in: that config is not a registered workspace and never appears in the workspace
 *  picker, so this link is the entire access path (see `Nav.configCampaignId`). */
export function openCampaignConfig(campaignId: string): void {
  window.location.hash = `#/config/${CAMPAIGN_SEGMENT}/${campaignId}`
}
