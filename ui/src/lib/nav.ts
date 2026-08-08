// Cross-page navigation, for the one case that crosses pages: a campaign card jumping into a
// Results view. Everything else navigates through App's own `select`.
//
// The hash is App's (it parses #/topic/view/campaign and owns the Nav state); this only writes one,
// and App's existing `hashchange` listener picks it up. Assigning `location.hash` *pushes* a history
// entry, which is what makes Back return to the campaign list in one press — in-view campaign
// changes replaceState instead, so they never pile up steps between here and there.

export type ResultsViewId = 'explorer' | 'run' | 'data'

export function openResultsView(view: ResultsViewId, campaignId: string): void {
  window.location.hash = `#/results/${view}/${campaignId}`
}
