// What the service worker does with a request: the whole of its policy, as pure functions, so the
// rules below are tested rather than trusted. `worker.ts` only wires them to the worker's events.
//
// The worker exists so the web UI can be installed as an app, and it caches the **app shell only**:
// the hashed script and style files the start page loads, the manifest and the icons, listed exactly
// at build time (`pwa.ts`). Three rules follow from what the UI is -- a thin client whose every
// screen is the service's data, behind a session:
//
// - **Nothing else is ever seen by the cache.** An API response, a run's files, a streamed log, the
//   login: none is passed through `respondWith` at all, so the browser handles them exactly as it
//   would with no worker. Only a GET for a URL on the list, same origin, with no query, is served
//   from the cache.
// - **A page load always goes to the network.** So the service's sign-in redirect still reaches a
//   session that has lapsed, and the page that loads is always the current build's. When the network
//   is not there, the answer is a page saying so -- never a cached start page that looks current and
//   cannot reach anything.
// - **A new build replaces the old shell on its next start.** The build id is in the worker's own
//   bytes, so a new build is a new worker; it takes over at once and drops every cache that is not
//   its own.

import { ACTIVE } from '../colors'

/** Every cache this worker owns starts with this; anything else under the origin is not its own. */
export const CACHE_PREFIX = 'robovast-shell-'

export function cacheName(buildId: string): string {
  return `${CACHE_PREFIX}${buildId}`
}

/** What to do with a request: answer it from the shell cache, load a page from the network (with the
 *  offline page as the answer when there is none), or leave it alone. */
export type Route = 'shell' | 'page' | 'pass'

export interface RequestLike {
  url: string
  method: string
  mode: string
}

export function route(request: RequestLike, origin: string, precache: ReadonlySet<string>): Route {
  if (request.method !== 'GET') return 'pass'
  const url = new URL(request.url)
  if (url.origin !== origin) return 'pass'
  if (request.mode === 'navigate') return 'page'
  // An exact path, and no query: a query is never part of a shell URL, and a request carrying one
  // is asking the service something.
  if (!url.search && precache.has(url.pathname)) return 'shell'
  return 'pass'
}

/** The caches a worker of `buildId` deletes when it takes over: every one of this worker's own that
 *  is not of this build. */
export function staleCaches(keys: readonly string[], buildId: string): string[] {
  const current = cacheName(buildId)
  return keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== current)
}

/** The answer to a page load when the network is not there: says so, offers a retry, and is plainly
 *  not the app. */
export const OFFLINE_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="${ACTIVE.surface.bg}">
<title>RoboVAST - offline</title>
<style>
  :root { color-scheme: dark; }
  body { font: 16px/1.5 system-ui, sans-serif; background: ${ACTIVE.surface.bg}; color: ${ACTIVE.text.ink}; margin: 0;
         min-height: 100vh; display: grid; place-items: center; padding: 1.5rem; box-sizing: border-box; }
  main { max-width: 26rem; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  button { font: inherit; padding: .75rem 1.25rem; min-height: 44px; margin-top: 1rem;
           border-radius: 8px; border: 0; background: ${ACTIVE.brand.accent}; color: ${ACTIVE.brand.onAccent};
           cursor: pointer; }
</style></head>
<body><main>
  <h1>RoboVAST cannot reach the service</h1>
  <p>This device is offline, or the service is not answering. Nothing shown here is from the
     service; reload once the connection is back.</p>
  <button type="button" onclick="location.reload()">Try again</button>
</main></body></html>
`
