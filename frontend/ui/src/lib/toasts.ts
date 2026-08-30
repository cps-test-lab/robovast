// The queue behind the app's transient notifications, kept free of React so it can be tested.
//
// A toast says that something short-lived happened -- a retrigger was accepted, a link was
// copied, a campaign ended. It is deliberately NOT the channel for failures: an error carries
// backend text worth reading twice, and one that erases itself after ten seconds is worse than
// one that never appeared. Those keep their inline Alert, which is why `Severity` has no
// `error` member -- the type is the rule.
//
// Nothing here knows what a campaign is. Callers pass a message; the campaign lifecycle watcher
// is an ordinary caller with no privileges.

/** How a toast reads. No `error`: failures stay inline, see above. */
export type Severity = 'success' | 'info' | 'warning'

/** One button on a toast, for the case where the notice has an obvious next step. */
export interface ToastAction {
  label: string
  onClick: () => void
}

/** What a caller passes to `notify`. */
export interface ToastSpec {
  severity: Severity
  /** One line. The subject of the sentence, not a report. */
  message: string
  /** An optional second line -- a count, a mode, a short reason. Never a traceback. */
  note?: string
  /**
   * Identity for a *repeatable* action. A second notify with the same key refreshes the toast
   * that is already up instead of stacking a duplicate, so leaning on a button does not build a
   * tower of identical notices. Omit it for anything that genuinely happened twice.
   */
  key?: string
  action?: ToastAction
}

export interface Toast extends ToastSpec {
  id: number
  /** Wall-clock ms after which this toast is gone. */
  deadline: number
}

/** Long enough to read a campaign id in, short enough not to become furniture. */
export const DEFAULT_DURATION_MS = 10_000

/**
 * How many are on screen at once.
 *
 * A campaign ending can arrive in a burst -- a batch of them finish together, or a service
 * restart moves several at once. Without a cap the stack grows until it covers the very list it
 * is reporting on, so the oldest are dropped: they are also the ones closest to expiring
 * anyway.
 */
export const MAX_VISIBLE = 4

/**
 * Add *spec* to *list*, returning a new list.
 *
 * Keyed specs replace their predecessor **in place** rather than moving it to the end. Position
 * is age here, and a refreshed toast has not just arrived; re-ordering the stack under a reader
 * mid-sentence is the thing to avoid, even though its deadline does get extended.
 */
export function addToast(list: Toast[], spec: ToastSpec, now: number, id: number): Toast[] {
  const deadline = now + DEFAULT_DURATION_MS
  if (spec.key !== undefined) {
    const at = list.findIndex((t) => t.key === spec.key)
    if (at !== -1) {
      const next = list.slice()
      next[at] = { ...spec, id: list[at].id, deadline }
      return next
    }
  }
  return trim([...list, { ...spec, id, deadline }])
}

/** Drop the oldest beyond MAX_VISIBLE. */
function trim(list: Toast[]): Toast[] {
  return list.length <= MAX_VISIBLE ? list : list.slice(list.length - MAX_VISIBLE)
}

/** Drop everything whose deadline has passed. */
export function expireToasts(list: Toast[], now: number): Toast[] {
  const kept = list.filter((t) => t.deadline > now)
  // Same array when nothing expired, so a caller can skip a re-render on the common tick.
  return kept.length === list.length ? list : kept
}

/** Drop one by id. */
export function dismissToast(list: Toast[], id: number): Toast[] {
  const kept = list.filter((t) => t.id !== id)
  return kept.length === list.length ? list : kept
}

/**
 * Push every deadline out by *byMs*.
 *
 * Hovering the stack pauses it: rather than tracking a paused-at per toast, each frame of the
 * hover moves the deadlines forward by the frame's own length, which keeps a single clock and
 * survives a toast being added mid-hover.
 */
export function extendDeadlines(list: Toast[], byMs: number): Toast[] {
  if (byMs <= 0 || list.length === 0) return list
  return list.map((t) => ({ ...t, deadline: t.deadline + byMs }))
}
