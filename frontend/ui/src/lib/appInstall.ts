// The page's side of the installable app: registering the service worker (src/sw/), and what this
// browser can say about it -- so Admin can tell a person why the app cannot be installed here rather
// than leaving the missing install prompt to be guessed at.
//
// The worker is registered only where it can work and is wanted: a secure context (HTTPS, or
// localhost), and a production build (under `npm run dev` there is no worker to register). Anywhere
// else the UI works exactly as it does without one.

import { useSyncExternalStore } from 'react'
import { PWA_FILES } from './pwaFiles'

// The app's base, wherever it is served: the worker's URL and its scope.
const PWA_SCOPE = import.meta.env.BASE_URL
const PWA_WORKER_URL = `${PWA_SCOPE}${PWA_FILES.worker}`

export type WorkerState =
  | { kind: 'registering' }
  | { kind: 'active' }
  /** The browser has no service workers at all. */
  | { kind: 'unsupported' }
  /** Plain HTTP on a name other than localhost: no worker, and no install. */
  | { kind: 'insecure' }
  /** A development server: nothing to register. */
  | { kind: 'development' }
  | { kind: 'failed'; error: string }

let state: WorkerState = { kind: 'registering' }
let registration: ServiceWorkerRegistration | null = null
const listeners = new Set<() => void>()

function set(next: WorkerState): void {
  state = next
  listeners.forEach((l) => l())
}

/** Why a worker would not be registered here, or null when it would be. */
export function workerBlocker(env: {
  hasServiceWorker: boolean
  secure: boolean
  production: boolean
}): WorkerState | null {
  if (!env.production) return { kind: 'development' }
  if (!env.secure) return { kind: 'insecure' }
  if (!env.hasServiceWorker) return { kind: 'unsupported' }
  return null
}

/** Register the app shell's worker, once, at start-up. */
export function registerShellWorker(): void {
  const blocked = workerBlocker({
    hasServiceWorker: 'serviceWorker' in navigator,
    secure: window.isSecureContext,
    production: import.meta.env.PROD,
  })
  if (blocked) {
    set(blocked)
    return
  }
  navigator.serviceWorker
    .register(PWA_WORKER_URL, { scope: PWA_SCOPE })
    .then(() => navigator.serviceWorker.ready)
    .then((ready) => {
      registration = ready
      set({ kind: 'active' })
    })
    .catch((err: unknown) => {
      // Said, not swallowed: the app works without a worker, but it cannot be installed, and a
      // person looking for the install prompt is told why on the Admin page.
      const error = err instanceof Error ? err.message : String(err)
      console.warn('the app shell\'s service worker could not be registered', err)
      set({ kind: 'failed', error })
    })
}

/** The worker's state, for a component. */
export function useWorkerState(): WorkerState {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    () => state,
  )
}

/** Whether this page is running as the installed app rather than in a browser tab. */
export function runningInstalled(): boolean {
  const standalone = typeof window.matchMedia === 'function'
    && window.matchMedia('(display-mode: standalone)').matches
  // iOS's own flag for a page started from the home screen.
  const iosStandalone = (navigator as Navigator & { standalone?: boolean }).standalone === true
  return standalone || iosStandalone
}

/** The worker's registration once it is active, or null while there is none. */
export function shellRegistration(): ServiceWorkerRegistration | null {
  return registration
}
