// The service worker: shell.ts's policy, wired to the worker's events. Built into `/sw.js` by the
// `pwa` Vite plugin, which defines the two constants below for the build it is part of. Type-checked
// on its own (`tsconfig.sw.json`), because a worker's globals are not a page's.
/// <reference lib="webworker" />

import { OFFLINE_PAGE, cacheName, route, staleCaches } from './shell'

declare const self: ServiceWorkerGlobalScope
/** The URLs of the app shell, as the page requests them. */
declare const SHELL_PRECACHE: string[]
/** Which build this worker is; a new build is a new worker. */
declare const SHELL_BUILD_ID: string

const PRECACHE = new Set(SHELL_PRECACHE)
const CACHE = cacheName(SHELL_BUILD_ID)

self.addEventListener('install', (event) => {
  // Precached whole or not at all: `addAll` rejects if any file fails, and a worker that failed to
  // install is not used. The fetches carry the session cookie, as the page's own would.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll([...PRECACHE]))
      // A new build takes over at once rather than when every tab of the old one has closed: its
      // pages come from the network anyway, so nothing in an open tab depends on the old shell.
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(staleCaches(keys, SHELL_BUILD_ID).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const which = route(event.request, self.location.origin, PRECACHE)
  if (which === 'pass') return
  if (which === 'page') {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(OFFLINE_PAGE, {
          status: 503,
          headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' },
        })),
    )
    return
  }
  // A shell file: its URL names its bytes, so the cached copy is the file. Missing from the cache (a
  // cache the browser evicted), it is fetched as it would be with no worker.
  event.respondWith(
    caches.open(CACHE)
      .then((cache) => cache.match(event.request))
      .then((hit) => hit ?? fetch(event.request)),
  )
})

// A notification the page posted through this worker (browserNotify.ts): a tap opens what it is
// about, in a window of this app if one is open.
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data as { url?: string } | null
  const url = data?.url ?? '/'
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin)
      if (open) return open.focus().then((w) => w.navigate(url))
      return self.clients.openWindow(url)
    }),
  )
})
