// The queue behind the app's transient notifications, kept free of React so it can be tested.
//
// A toast says that something short-lived happened -- a retrigger was accepted, a link was
// copied, a campaign ended.
//
// Failures are here too, and they get weight without getting permanence. A failure carries the
// service's own text, so it is given LONGER than a passing notice, counted apart so a burst of
// endings cannot evict one, and drawn nearest the corner. It still clears itself: a notice that
// has to be clicked away turns every failure into a chore, and the ones worth keeping have a
// home of their own -- a campaign's failure reason is on its card, one click from the toast that
// announced it.
//
// The gap this leaves, named because it is real: a REFUSED ACTION ("retrigger refused: ...")
// exists nowhere but its toast, so when that expires the reason is gone. A durable event log is
// what closes it; until then, the duration below is the whole of the answer.
//
// Nothing here knows what a campaign is. Callers pass a message; the campaign lifecycle watcher
// is an ordinary caller with no privileges.

/** How a toast reads. `error` is the emphasised one -- see `isFailure`. */
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
  /** Wall-clock ms after which this toast is gone. */
  deadline: number
}

/** Long enough to read a campaign id in, short enough not to become furniture. */
export const DEFAULT_DURATION_MS = 10_000

/**
 * Longer, for a failure.
 *
 * A passing notice is a few words and a glance; a failure is the service's own sentence, and
 * sometimes a path or a ref inside it. Ten seconds is enough to notice one and not enough to
 * read it, so the same clock would effectively hide the text it exists to deliver. Hovering
 * still holds it indefinitely.
 */
export const ERROR_DURATION_MS = 30_000

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
 * How many *failures* are on screen at once, counted separately from the rest.
 *
 * One shared cap lets a burst of campaigns ending together evict an unread failure, which is
 * the one eviction that loses something. Two caps, each dropping its own oldest, so the two
 * kinds cannot crowd each other out in either direction.
 */
export const MAX_VISIBLE_ERRORS = 3

/** Whether this toast reports something that went wrong: longer-lived, capped apart, drawn last. */
export function isFailure(toast: Pick<Toast, 'severity'>): boolean {
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
  const deadline = now + (isFailure(spec) ? ERROR_DURATION_MS : DEFAULT_DURATION_MS)
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

/** Drop the oldest beyond each group's cap, failures and passing notices counted apart. */
function trim(list: Toast[]): Toast[] {
  const dropped = new Set<number>()
  for (const [failure, cap] of [[true, MAX_VISIBLE_ERRORS], [false, MAX_VISIBLE]] as const) {
    const group = list.filter((t) => isFailure(t) === failure)
    for (const t of group.slice(0, Math.max(0, group.length - cap))) dropped.add(t.id)
  }
  return dropped.size === 0 ? list : list.filter((t) => !dropped.has(t.id))
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
