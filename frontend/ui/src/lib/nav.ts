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
    shareImport: '',
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

/** The absolute URL that opens the campaign view's share-import dialog on *search*.
 *
 *  Not a navigation but a link to *hand somebody*: the import dialog offers one per campaign so
 *  that "here, take this one" is a thing you can paste, and the recipient only has to press
 *  Import. `vast share import` reads the same string, so it works in a terminal too.
 *
 *  Absolute, and therefore **deployment-scoped** — it carries this deployment's origin, which is
 *  right for colleagues on the same one and wrong for anybody else. The origin comes off the
 *  current URL rather than being composed, so a deployment served under a sub-path is included
 *  for free; the hash comes from `hashFor` for the reason `openResultsView` gives — one grammar,
 *  not two. */
export function shareImportLink(search: string): string {
  const nav: Nav = {
    topicId: 'execution',
    viewId: '',
    campaignId: '',
    sel: { level: 'campaign' },
    tab: '',
    configCampaignId: '',
    shareImport: search,
  }
  return `${window.location.href.split('#')[0]}#${hashFor(nav)}`
}
