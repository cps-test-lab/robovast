/**
 * Whether the service is now serving a different frontend build than this tab booted from.
 *
 * A code-split chunk goes missing in two ways that want opposite remedies — a dropped
 * request wants the import retried, a service updated onto new asset hashes wants the
 * document reloaded — and the browser throws the same error for both, so a boundary that
 * only sees the error can offer both buttons and decide neither. index.html is what breaks
 * the tie: it names the entry chunk by content hash, and Rollup propagates a hash change up
 * the import graph, so *any* change to the app changes the name written there. Re-fetch it,
 * find a different name, and this tab is provably holding URLs the service no longer has.
 */

/** The `.js` URLs an index.html references: the entry module and its modulepreloads. */
const SCRIPT_URL_RE = /<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+\.js)"/gi

/**
 * The build fingerprint of a served index.html, as the sorted list of script URLs in it.
 *
 * A regex rather than DOMParser because this is also the unit under test, and the tests run
 * without a DOM. It is safe here for the reason a regex over HTML usually is not: the input
 * is Vite's own generated index.html, whose shape is fixed by the bundler, not authored.
 */
export function parseServedEntries(html: string): string[] {
  return [...html.matchAll(SCRIPT_URL_RE)].map((m) => m[1]).sort()
}

/** The same fingerprint, read from a live document. */
function documentEntries(doc: Document): string[] {
  return [...doc.querySelectorAll('script[src], link[href]')]
    .map((el) => el.getAttribute('src') ?? el.getAttribute('href') ?? '')
    .filter((url) => url.endsWith('.js'))
    .sort()
}

// Read once, at module init, and never again. This module is reached from the entry chunk,
// so it runs before React renders and therefore before Vite's preload helper starts adding
// `<link rel="modulepreload">` elements of its own for dynamic imports. Re-reading the
// document later would compare a grown list against a static file and call every lazy
// import an update.
const BOOT_ENTRIES = typeof document === 'undefined' ? [] : documentEntries(document)

/**
 * Do two fingerprints describe different builds?
 *
 * An empty list on either side means "cannot tell", never "different": under `npm run dev`
 * index.html points at `/src/main.tsx` and there are no hashed `.js` names to compare at
 * all, and a tab must not reload itself over a question nobody could answer.
 */
export function entriesDiffer(boot: string[], served: string[]): boolean {
  if (boot.length === 0 || served.length === 0) return false
  return boot.join('\n') !== served.join('\n')
}

/**
 * Fetch the document this tab was served from and compare builds.
 *
 * Navigation is hash-based, so `location.pathname` is the app root wherever the user has
 * navigated to, and asking for it can never hit an API route. Uncached twice over — the
 * `no-store` and the unique query — because a cached index.html would answer with the build
 * this tab already has and hide exactly the change being looked for.
 */
export async function servedBuildDiffers(): Promise<boolean> {
  const res = await fetch(`${location.pathname}?_build=${Date.now()}`, {
    cache: 'no-store',
    headers: { Accept: 'text/html' },
  })
  if (!res.ok) throw new Error(`build probe: ${res.status}`)
  return entriesDiffer(BOOT_ENTRIES, parseServedEntries(await res.text()))
}

/** Where the last automatic reload is recorded. Per-tab: the cooldown is about this tab. */
const RELOAD_MARK = 'robovast.buildReloadAt'
/** How long after an automatic reload the next one is refused. */
const RELOAD_COOLDOWN_MS = 60_000

/**
 * May this tab reload itself now?
 *
 * The cooldown is the loop guard, and the loop it guards against is real rather than
 * theoretical: mid-roll, two replicas serve two builds, so a tab can fetch index.html from
 * the new one and its chunks from the old one and satisfy the "updated" test again on every
 * pass. Refusing the second reload leaves the panel and its buttons, which is a worse minute
 * for one user than a tab that reloads forever.
 */
export function reloadAllowed(now = Date.now()): boolean {
  try {
    const at = Number(sessionStorage.getItem(RELOAD_MARK) ?? 0)
    return !at || now - at >= RELOAD_COOLDOWN_MS
  } catch {
    // Storage denied (private mode, blocked cookies). Losing the guard is better than
    // losing the reload: without it the user is back to reloading by hand.
    return true
  }
}

/** Record a reload about to happen, so the one after it falls inside the cooldown. */
export function markReloading(now = Date.now()): void {
  try {
    sessionStorage.setItem(RELOAD_MARK, String(now))
  } catch {
    // See reloadAllowed: an unavailable store degrades to no guard, not to no reload.
  }
}
