// OS-level notifications, for the case the toast cannot cover: nobody is looking at the tab.
//
// Deliberately separate from the toast layer. A toast is a rectangle in this document; this
// leaves the document, needs a permission grant, and must not fire while the user is already
// watching the thing it would announce. Keeping them apart means the toast provider never
// touches the Notification API, and a caller that wants both simply calls both.
//
// This is NOT the durable channel. A browser that is closed gets nothing, and that is fine:
// campaign lifecycle already reaches a phone from the server via ntfy
// (robovast.execution.notify), once per campaign rather than once per open tab.

// Three states, one key: absent means nobody has been asked yet, '1' means on, '0' means off.
// The absent state is what `shouldAsk` reads, and it is why declining is stored rather than
// simply not-stored -- a decline that left no trace would be re-asked on every visit.
const OPT_IN_KEY = 'robovast.browserNotifications'

/** Whether this browser can do it at all (absent over plain http, and in some embeddings). */
export function supported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window
}

export function permission(): NotificationPermission | 'unsupported' {
  return supported() ? Notification.permission : 'unsupported'
}

/** The user's own switch, remembered per browser. Granting permission is not the same as
 *  wanting them: a grant is forever, and this is what they can turn back off. */
export function optedIn(): boolean {
  try {
    return window.localStorage.getItem(OPT_IN_KEY) === '1'
  } catch {
    // Private modes and blocked site data throw on access rather than returning null.
    return false
  }
}

export function setOptedIn(on: boolean): void {
  try {
    window.localStorage.setItem(OPT_IN_KEY, on ? '1' : '0')
  } catch {
    // Not being able to remember the choice is not a reason to refuse it for this session.
  }
}

/** Whether this browser has answered at all -- either button counts, not just the yes. */
export function decided(): boolean {
  try {
    return window.localStorage.getItem(OPT_IN_KEY) !== null
  } catch {
    // A storage that throws can never remember an answer, so treating this as "already
    // decided" is the kinder reading: the alternative is asking again on every single load.
    return true
  }
}

/**
 * Whether to put the one-time ask in front of the user.
 *
 * Not `permission() === 'default'`: a browser that already granted permission but has lost our
 * record of the choice (cleared site data, a second profile) must still be asked whether it
 * *wants* them -- the grant is the browser's answer, not the user's. `denied` is excluded
 * because it is sticky and cannot be re-prompted from the page, so an ask there offers nothing.
 */
export function shouldAsk(): boolean {
  return supported() && !decided() && permission() !== 'denied'
}

/** Ask the browser. Must be called from a user gesture, or it resolves `default` unprompted. */
export async function requestPermission(): Promise<NotificationPermission | 'unsupported'> {
  if (!supported()) return 'unsupported'
  try {
    return await Notification.requestPermission()
  } catch {
    return Notification.permission
  }
}

export interface BrowserNote {
  title: string
  body?: string
  /** Replaces an earlier note with the same tag instead of stacking one beside it. */
  tag?: string
}

/**
 * Post one notification, or do nothing.
 *
 * Every precondition is checked **here** rather than at the call site -- support, opt-in,
 * permission, and above all whether the tab is actually hidden. A visible tab already shows the
 * toast, and an OS popup on top of it says the same thing twice. Putting that test inside `post`
 * is what stops the next caller forgetting it.
 */
export function post(note: BrowserNote): void {
  if (!supported() || !optedIn()) return
  if (Notification.permission !== 'granted') return
  if (typeof document !== 'undefined' && document.visibilityState !== 'hidden') return
  try {
    new Notification(note.title, { body: note.body, tag: note.tag })
  } catch {
    // Some platforms require a service worker and throw on the constructor. Best-effort: a
    // notification that cannot be shown must never break the page that asked for it.
  }
}
