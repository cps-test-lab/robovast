// The URL hash is the navigation state: which topic, which of its views, and which campaign the
// view is showing. Held here rather than in App because two of the rules are load-bearing and only
// a test can hold them — see `configCampaignId` below, where a mistake does not misplace a view but
// *exposes* one that is meant to be reachable only by an explicit link.
//
// Grammar:
//   #/<topic>                              a leaf topic (Config)
//   #/<topic>/<view>[/<campaign>]          a topic with views (Results), optionally campaign-scoped
//   #/config/campaign/<campaign>           the deep link into one campaign's frozen config

/** What a topic looks like to the hash: an id, and whether it has sub-views. */
export interface NavTopicShape {
  id: string
  views?: { id: string }[]
}

export interface Nav {
  topicId: string
  viewId: string
  /** The campaign the view is showing, for topics whose views are campaign-scoped (Results). It is
   *  held here, in the URL, rather than inside the page: that is what lets a campaign card link
   *  straight into a view, a reload come back to the same campaign, and a link be pasted to someone
   *  else. Empty until a campaign is chosen — the page then fills it in (see App's setCampaign). */
  campaignId: string
  /** The campaign whose frozen `_config/` the Config topic is showing, read-only.
   *
   *  Deliberately *not* `campaignId`. The two want opposite memory: the results campaign is sticky,
   *  carried across every navigation by `nextNav` so stepping out to the campaign list and back
   *  returns to what was being read; this one must be forgotten the moment the user clicks Config
   *  in the sidebar, because that click means "my workspaces" and a campaign's config is a hidden
   *  view reachable only from its card. One field each, with one rule each. */
  configCampaignId: string
}

/** Marks the third segment as a campaign id rather than a view, for a leaf topic.
 *
 *  A literal rather than `#/config/<id>` so a stale `#/config/files` bookmark — the sub-view Config
 *  had before the Editor/Files split became an in-page tab bar — still resolves to plain Config
 *  instead of asking for a campaign named `files`. */
export const CAMPAIGN_SEGMENT = 'campaign'

/** Parse a location hash into a valid Nav, falling back when it is empty or names no topic.
 *
 *  Takes the hash rather than reading `window`: everything here is a rule about strings, and the
 *  rules are the part that has to be testable. */
export function navFromHash(hash: string, topics: NavTopicShape[], fallback: Nav): Nav {
  const [rawTopic, rawView, rawCampaign] = hash.replace(/^#\/?/, '').split('/')
  const topic = topics.find((t) => t.id === rawTopic)
  if (!topic) return fallback
  if (!topic.views) {
    // A leaf topic has no view to name, so its second segment is a scope marker if it is one at
    // all — anything else (a stale bookmark) is noise and leaves plain topic.
    return {
      topicId: topic.id,
      viewId: '',
      campaignId: '',
      configCampaignId: rawView === CAMPAIGN_SEGMENT ? (rawCampaign ?? '') : '',
    }
  }
  const view = topic.views.find((v) => v.id === rawView)?.id ?? topic.views[0]?.id ?? ''
  // The campaign is taken verbatim — the page validates it against the campaigns it has and repairs
  // the hash if it is stale, which is the only place that knows whether an id still names anything.
  return { topicId: topic.id, viewId: view, campaignId: rawCampaign ?? '', configCampaignId: '' }
}

export function hashFor({ topicId, viewId, campaignId, configCampaignId }: Nav): string {
  if (!viewId) {
    return configCampaignId ? `/${topicId}/${CAMPAIGN_SEGMENT}/${configCampaignId}` : `/${topicId}`
  }
  return campaignId ? `/${topicId}/${viewId}/${campaignId}` : `/${topicId}/${viewId}`
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
    // ...and the config campaign is dropped, always. This is the whole of "reachable only through
    // the explicit link": carrying it would reopen a campaign's config on a plain sidebar click.
    configCampaignId: '',
  }
}
