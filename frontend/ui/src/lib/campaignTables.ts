// The campaign menu's "Build all tables": what it offers, to which campaign, and what it says.
//
// Never needed for an answer: every table is built the first time a query, a panel or an export
// names it. Building them all only moves that cost to now, for a campaign about to be analysed at
// length -- so the entry is offered on a finished campaign only, whose runs will not change under
// the build, and its confirmation says plainly that nothing is missing without it.

/** Whether the menu offers "Build all tables" for a campaign in *phase*. */
export const offersBuildTables = (phase: string | undefined): boolean => phase === 'finished'

export const BUILD_TABLES_TITLE = 'Build all tables now?'

export const BUILD_TABLES_MESSAGE =
  'This is not needed: every table is built the first time something names it — a query, a ' +
  'panel or an export. Building them all now only moves that cost forward, for a campaign ' +
  'you are about to analyse at length. It runs in the background; progress appears in the ' +
  "campaign log's TABLES section."
