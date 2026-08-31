// The queue behind the app's transient notifications, kept free of React so it can be tested.
//
// A toast says that something short-lived happened -- a retrigger was accepted, a link was
// copied, a campaign ended.
//
// Failures are here too, and they are the exception that proves the rule: an error carries
// backend text worth reading twice, so it must not erase itself. An error toast has NO deadline
// -- only the reader dismisses it. That is what let failures leave the campaign card, where they
// used to sit until the tab was reloaded because nothing ever reset the mutation that raised
// them. A notice that cannot be gotten rid of and one that vanishes mid-sentence are both wrong;
// this is the third option.
//
// Nothing here knows what a campaign is. Callers pass a message; the campaign lifecycle watcher
// is an ordinary caller with no privileges.

/** How a toast reads. `error` is the sticky one -- see the note on `deadline`. */
export type Severity = 'success' | 'info' | 'warning' | 'error'

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
  /**
   * Wall-clock ms after which this toast is gone, or `Infinity` for one that never expires.
   *
   * `Infinity` rather than a nullable field on purpose: every comparison in this module already
   * reads `deadline > now`, which is true forever, so sticky toasts need no branch in
   * `expireToasts` and none in the hover pause. The one place that does care is `trim`.
   */
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
 * How many *sticky* toasts are on screen at once, counted separately.
 *
 * One shared cap would be wrong in both directions: sticky toasts never leave on their own, so
 * a few of them would permanently crowd out arriving notices -- and a burst of successes would
 * evict an unread failure, which is the one thing that must not happen silently. Two caps, each
 * dropping its own oldest.
 */
export const MAX_VISIBLE_ERRORS = 3

/** Whether this toast waits for the reader rather than for the clock. */
export function isSticky(toast: Pick<Toast, 'severity'>): boolean {
  return toast.severity === 'error'
}

/**
 * Add *spec* to *list*, returning a new list.
 *
 * Keyed specs replace their predecessor **in place** rather than moving it to the end. Position
 * is age here, and a refreshed toast has not just arrived; re-ordering the stack under a reader
 * mid-sentence is the thing to avoid, even though its deadline does get extended.
 */
export function addToast(list: Toast[], spec: ToastSpec, now: number, id: number): Toast[] {
  const deadline = isSticky(spec) ? Infinity : now + DEFAULT_DURATION_MS
  if (spec.key !== undefined) {
    const at = list.findIndex((t) => t.key === spec.key)
    if (at !== -1) {
      const next = list.slice()
      next[at] = { ...spec, id: list[at].id, deadline }
      // Trimmed as well as replaced: a key can carry a *different* severity than last time --
      // retrying a failed action reuses the key its success used -- so a replacement can move a
      // toast between the two groups and put one of them over its cap.
      return trim(next)
    }
  }
  return trim([...list, { ...spec, id, deadline }])
}

/** Drop the oldest beyond each group's cap, sticky and transient counted apart. */
function trim(list: Toast[]): Toast[] {
  const dropped = new Set<number>()
  for (const [sticky, cap] of [[true, MAX_VISIBLE_ERRORS], [false, MAX_VISIBLE]] as const) {
    const group = list.filter((t) => isSticky(t) === sticky)
    for (const t of group.slice(0, Math.max(0, group.length - cap))) dropped.add(t.id)
  }
  return dropped.size === 0 ? list : list.filter((t) => !dropped.has(t.id))
}

/** Drop everything whose deadline has passed. Sticky toasts never have one. */
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
