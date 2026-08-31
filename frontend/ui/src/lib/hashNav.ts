// The URL hash is the navigation state: which topic, which of its views, which campaign the view is
// showing, and which node of that campaign. Held here rather than in App because several of the
// rules are load-bearing and only a test can hold them — see `configCampaignId` below, where a
// mistake does not misplace a view but *exposes* one that is meant to be reachable only by an
// explicit link, and `hashFor` below, which decides what each view is allowed to spell.
//
// Grammar:
//   #/<topic>                                     a leaf topic (Config)
//   #/<topic>/<view>[/<campaign>]                 a topic with views (Results), campaign-scoped
//   #/results/<view>/<campaign>/<config>[/<run>]  ...down to one node of that campaign
//   #/results/<view>/<campaign>?batch=<i>         ...a search campaign's round
//   #/results/explorer/<campaign>/…?tab=<name>    ...and which of its notebook tabs
//   #/config/campaign/<campaign>                  the deep link into one campaign's frozen config
//   #/execution?import=<search>                   the campaign view, share-import dialog on <search>
//   #/execution?campaign=<campaign>               the campaign view, that campaign's card opened
//
// The node's address is *positional*, and deliberately the same spelling RoboVAST uses for a run
// everywhere else -- `<campaign>/<config>/<run>` is the on-disk layout and the `/results` REST
// address space, where `file_address.py` already records the decision against invented segment
// markers. Its arity never varies, so no `cfg`/`run` keywords are needed to read it. Anything that
// is *not* part of the address (the notebook tab; a batch, which has no positional slot) goes in
// the query instead, so nothing in the path is derivable from anything else in it.

/** What a topic looks like to the hash: an id, and whether it has sub-views. */
export interface NavTopicShape {
  id: string
  views?: { id: string }[]
}

/** The Campaigns topic. Named here because the grammar mentions it: `?import=` is a request
 *  only that page can act on, and `navFromHash` refuses to read one addressed anywhere else
 *  (see `shareImport`). */
const EXECUTION_TOPIC = 'execution'

/** The Results sub-views. Declared here, with the grammar, because two of them appear in it by
 *  name: only the Explorer can show a node that is not a run, and only it has notebook tabs. */
export type ResultsViewId = 'explorer' | 'run' | 'data'
const EXPLORER: ResultsViewId = 'explorer'
const RUN_VIEW: ResultsViewId = 'run'

/** Which node of a campaign a Results view is showing — one of the four levels the results tree
 *  addresses, as a union rather than a bag of optional fields, so that an impossible combination
 *  (a config name *and* a batch index) cannot be built at all. The discriminant is the tree's own
 *  node kind, so `hashFor` and the views switch on it exhaustively.
 *
 *  `batch` appears only here, at its own level: it is a grouping recorded in the store rather than
 *  a directory, a run is identified by config + index alone, and the batch of a given config is a
 *  lookup away in the rows every Results view already loads. Carrying it beside a config would
 *  duplicate a derivable fact and need repairing whenever the two disagreed. */
export type ResultsSel =
  | { level: 'campaign' }
  | { level: 'batch'; batch: number }
  | { level: 'config'; configName: string }
  | { level: 'run'; configName: string; runId: number }

export const CAMPAIGN_SEL: ResultsSel = { level: 'campaign' }

export interface Nav {
  topicId: string
  viewId: string
  /** The campaign the view is showing, for topics whose views are campaign-scoped (Results). It is
   *  held here, in the URL, rather than inside the page: that is what lets a campaign card link
   *  straight into a view, a reload come back to the same campaign, and a link be pasted to someone
   *  else. Empty until a campaign is chosen — the page then fills it in (see App's setResults). */
  campaignId: string
  /** Which node *of* that campaign, for the same reasons — a link to a result, not just to the
   *  campaign that produced it. Shared between the Explorer and the Run view, so selecting a run in
   *  one and switching to the other replays that run. */
  sel: ResultsSel
  /** Which of the selected node's notebook tabs is open (a workload name, or `log` for the built-in
   *  Log tab). Kept separate from `sel` because it is the lens on a node, not part of its identity
   *  — which is also why it lives in the query rather than the path. */
  tab: string
  /** The campaign whose frozen `_config/` the Config topic is showing, read-only.
   *
   *  Deliberately *not* `campaignId`. The two want opposite memory: the results campaign is sticky,
   *  carried across every navigation by `nextNav` so stepping out to the campaign list and back
   *  returns to what was being read; this one must be forgotten the moment the user clicks Config
   *  in the sidebar, because that click means "my workspaces" and a campaign's config is a hidden
   *  view reachable only from its card. One field each, with one rule each. */
  configCampaignId: string
  /** A search string the campaign view's share-import dialog opens on — the deep link
   *  somebody is handed so that importing an archive off the share is one click.
   *
   *  Like `configCampaignId` this is carried by a link and nothing else: `nextNav` drops it,
   *  because a sidebar click on Campaigns means "the list", never somebody else's import
   *  request. Unlike `tab` it is also *parsed* for one topic only — every page stays mounted
   *  (App's KeepAlive), so a `#/config?import=x` that resolved would have the campaign view
   *  open a dialog over a page nobody is looking at. A tab a view ignores is inert; this is
   *  not. */
  shareImport: string
  /** A campaign whose card the campaign view should open, and scroll to, on arrival.
   *
   *  An *arrival instruction*, not live state, which is the whole of its contract: cards fold and
   *  unfold freely and several can be open at once, so a field naming one of them could not stay
   *  true and is not asked to. It says where a link points; what the reader does next is theirs.
   *
   *  Parsed for the campaign view alone, for `shareImport`'s reason -- every page stays mounted
   *  under KeepAlive, so a request addressed to one page must not be readable by another. Dropped
   *  by `nextNav`, because clicking Campaigns in the sidebar means "the list". */
  openCampaign: string
}

/** Marks the third segment as a campaign id rather than a view, for a leaf topic.
 *
 *  A literal rather than `#/config/<id>` so a stale `#/config/files` bookmark — the sub-view Config
 *  had before the Editor/Files split became an in-page tab bar — still resolves to plain Config
 *  instead of asking for a campaign named `files`. */
export const CAMPAIGN_SEGMENT = 'campaign'

/** The URL spelling of the Explorer's built-in Log tab.
 *
 *  Unambiguous because a workload may not be named `log`: two tabs reading the same would be a bug
 *  in the tab bar with or without a URL, so it is refused where notebooks are declared
 *  (`ExplorerConfig` in `robovast.common.config`) rather than guessed at here. */
export const LOG_TAB_SLUG = 'log'

/** `decodeURIComponent` that cannot throw. A hash is user-editable text, and one malformed escape
 *  (`%zz`) must not take the page down with it — it is far more useful to read it as written. */
function decodeSegment(raw: string): string {
  try {
    return decodeURIComponent(raw)
  } catch {
    return raw
  }
}

/** A run index or batch index as written in a URL, or null for anything that is not one. Strict
 *  rather than `parseInt`, which would read `3abc` as 3 and thereby invent a node. */
function index(raw: string | null | undefined): number | null {
  return raw && /^\d+$/.test(raw) ? Number(raw) : null
}

/** The node named by the segments after the campaign, plus the query. */
function selFromHash(configSeg: string, runSeg: string, query: URLSearchParams): ResultsSel {
  const configName = decodeSegment(configSeg ?? '')
  if (configName) {
    const runId = index(runSeg)
    return runId == null ? { level: 'config', configName } : { level: 'run', configName, runId }
  }
  // Only reached with no config segment, which is what makes `?batch=` alongside a config
  // impossible to express rather than merely forbidden.
  const batch = index(query.get('batch'))
  return batch == null ? CAMPAIGN_SEL : { level: 'batch', batch }
}

/** Parse a location hash into a valid Nav, falling back when it is empty or names no topic.
 *
 *  Takes the hash rather than reading `window`: everything here is a rule about strings, and the
 *  rules are the part that has to be testable. */
export function navFromHash(hash: string, topics: NavTopicShape[], fallback: Nav): Nav {
  const [rawPath, rawQuery = ''] = hash.replace(/^#\/?/, '').split('?')
  const [rawTopic, rawView, rawCampaign, rawConfig, rawRun] = rawPath.split('/')
  const topic = topics.find((t) => t.id === rawTopic)
  if (!topic) return fallback
  if (!topic.views) {
    // A leaf topic has no view to name, so its second segment is a scope marker if it is one at
    // all — anything else (a stale bookmark) is noise and leaves plain topic.
    return {
      topicId: topic.id,
      viewId: '',
      campaignId: '',
      sel: CAMPAIGN_SEL,
      tab: '',
      configCampaignId: rawView === CAMPAIGN_SEGMENT ? decodeSegment(rawCampaign ?? '') : '',
      // Read for the campaign view alone. Every other leaf topic gets '' no matter what its
      // query says — see `shareImport` for why this one is not parsed everywhere `tab` is.
      shareImport:
        topic.id === EXECUTION_TOPIC
          ? (new URLSearchParams(rawQuery).get('import') ?? '')
          : '',
      openCampaign:
        topic.id === EXECUTION_TOPIC
          ? (new URLSearchParams(rawQuery).get('campaign') ?? '')
          : '',
    }
  }
  const view = topic.views.find((v) => v.id === rawView)?.id ?? topic.views[0]?.id ?? ''
  const query = new URLSearchParams(rawQuery)
  // The campaign is taken verbatim — the page validates it against the campaigns it has and repairs
  // the hash if it is stale, which is the only place that knows whether an id still names anything.
  // The same is true one level down: whether a campaign *has* this config or run is a question only
  // the loaded rows can answer, so the node is parsed here and checked there.
  return {
    topicId: topic.id,
    viewId: view,
    campaignId: decodeSegment(rawCampaign ?? ''),
    sel: selFromHash(rawConfig, rawRun, query),
    tab: query.get('tab') ?? '',
    configCampaignId: '',
    // A topic with views is never the campaign view, which is a leaf.
    shareImport: '',
    openCampaign: '',
  }
}

/** The path segments a selection adds after the campaign. */
function selPath(sel: ResultsSel): string {
  switch (sel.level) {
    case 'config':
      return `/${encodeURIComponent(sel.configName)}`
    case 'run':
      return `/${encodeURIComponent(sel.configName)}/${sel.runId}`
    default:
      return ''
  }
}

/** How much of the selection a given view's address carries.
 *
 *  A hash is a view's address, not a place to stash state the view ignores: the Run view replays a
 *  run and cannot show a campaign, batch or config node, and the Data browser is campaign-scoped
 *  and has no node at all. So each spells what it can act on, and no more. The cost is stated
 *  rather than hidden — stepping out to the Data browser and back returns to the campaign node,
 *  and back from the Run view returns to the run but the default tab. */
function selFor(viewId: string, sel: ResultsSel): ResultsSel {
  if (viewId === EXPLORER) return sel
  if (viewId === RUN_VIEW) return sel.level === 'run' ? sel : CAMPAIGN_SEL
  return CAMPAIGN_SEL
}

export function hashFor(nav: Nav): string {
  const { topicId, viewId, campaignId, configCampaignId } = nav
  if (!viewId) {
    if (configCampaignId) {
      return `/${topicId}/${CAMPAIGN_SEGMENT}/${encodeURIComponent(configCampaignId)}`
    }
    // Spelled for the campaign view only, the same shape as `tab` being spelled by the
    // Explorer alone: a view's address may not carry a request another view would act on.
    if (topicId === EXECUTION_TOPIC) {
      // One at a time, and `import` first: both are requests to the same page, and a link
      // carrying two of them would be asking it to do two unrelated things at once.
      if (nav.shareImport) return `/${topicId}?import=${encodeURIComponent(nav.shareImport)}`
      if (nav.openCampaign) return `/${topicId}?campaign=${encodeURIComponent(nav.openCampaign)}`
    }
    return `/${topicId}`
  }
  if (!campaignId) return `/${topicId}/${viewId}`
  const sel = selFor(viewId, nav.sel)
  const query = new URLSearchParams()
  if (sel.level === 'batch') query.set('batch', String(sel.batch))
  if (viewId === EXPLORER && nav.tab) query.set('tab', nav.tab)
  const path = `/${topicId}/${viewId}/${encodeURIComponent(campaignId)}${selPath(sel)}`
  const q = query.toString()
  return q ? `${path}?${q}` : path
}

/** The Nav a topic/view selection leads to — the state transition behind App's `select`. */
export function nextNav(
  nav: Nav,
  topics: NavTopicShape[],
  topicId: string,
  viewId?: string,
): Nav {
  const topic = topics.find((t) => t.id === topicId) ?? topics[0]
  const view = viewId ?? topic.views?.[0]?.id ?? ''
  return {
    topicId: topic.id,
    viewId: view,
    // The results campaign is carried through every navigation: going from Explorer to the Data
    // browser is a change of lens on one campaign, not a request for a different one, and stepping
    // out to the campaign list and back should return to what was being read. It only reaches the
    // hash for a topic that has views (hashFor), so `#/config` stays `#/config`.
    campaignId: nav.campaignId,
    // ...and so is the node within it, for the same reason one level down: that is what makes the
    // Explorer's and the Run view's cross-links land on the same run. Which of it a given view
    // actually spells is `hashFor`'s decision, not this one.
    sel: nav.sel,
    tab: nav.tab,
    // The config campaign, in contrast, is dropped, always. This is the whole of "reachable only
    // through the explicit link": carrying it would reopen a campaign's config on a plain sidebar
    // click.
    configCampaignId: '',
    // Dropped for the same reason: an import request belongs to the link it arrived on, and
    // carrying it would re-open the share dialog every time somebody clicked Campaigns.
    shareImport: '',
    openCampaign: '',
  }
}
