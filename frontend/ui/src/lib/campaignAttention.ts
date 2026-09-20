// What a campaign is telling a person while it runs, and how the view says it.
//
// A campaign that is losing runs to something an author could fix -- memory it measured too
// low, so far -- keeps going: a sweep that reaches its end having lost some runs is worth more
// than one held halfway, and which of those somebody wants is their call, not the runner's. So
// the campaign states the fact and carries on, and the monitor's job is to make that fact
// impossible to miss for whoever is watching, without interrupting whoever is not.
//
// A pure function of the summary, tested on its own: what deserves a mark on the card is one
// question, and answering it in one place is what keeps the card from growing rules.

/** A campaign summary, narrowed to what a mark is decided from. */
export interface AttentionSource {
  attention?: string | null
}

export interface Attention {
  /** One line for the icon's tooltip and the dialog's title. */
  title: string
  /** The campaign's own sentence: what is happening, with the numbers it was measured from. */
  detail: string
}

/** What this campaign wants a person to know, or `null` while it wants nothing. */
export function attentionFor(summary: AttentionSource | null | undefined): Attention | null {
  const detail = (summary?.attention || '').trim()
  if (!detail) return null
  return { title: 'This campaign needs attention', detail }
}
