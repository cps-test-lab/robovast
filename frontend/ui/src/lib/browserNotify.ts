// OS-level notifications, for the case the toast cannot cover: nobody is looking at the tab.
//
// Deliberately separate from the toast layer. A toast is a rectangle in this document; this
// leaves the document, needs a permission grant, and must not fire while the user is already
// watching the thing it would announce. Keeping them apart means the toast provider never
// touches the Notification API, and the one caller that wants both simply calls both.
//
// This is NOT the durable channel. A browser that is closed gets nothing, and that is fine:
// campaign lifecycle already reaches a phone from the server via ntfy
// (robovast.execution.notify), once per campaign rather than once per open tab.

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
