import { Suspense, lazy, useCallback, useMemo, useState } from 'react'
import type { ComponentType } from 'react'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { ErrorBoundary } from '@/components/ErrorBoundary'

// Code-splitting trades one big download for several small ones. That is the right trade
// here — the campaign list has no use for the SQL editor, the 3D viewport or the charting
// stacks, which together were most of a 10 MB entry chunk — but only if a chunk that does
// not arrive is recoverable. The service is routinely reached through a `kubectl
// port-forward`, where a dropped request is ordinary, and an unhandled failed import is a
// white page with "Failed to fetch dynamically imported module" in the console.
//
// So the two halves ship together: retry the import a couple of times on our own, and put a
// boundary behind that with a button, so the worst case is one click instead of a reload.

/** Attempts before giving up, including the first. Small: a real outage should surface. */
const ATTEMPTS = 3
/** Backoff between attempts. Short — a port-forward blip recovers in well under a second. */
const RETRY_DELAY_MS = 350

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/**
 * Import with retries.
 *
 * The cache-busting query on retries is load-bearing: a browser that has negatively cached
 * the failed request would otherwise serve the same failure back instantly, and every
 * attempt after the first would be free and useless.
 */
export async function retryImport<T>(
  load: (bust?: string) => Promise<T>,
): Promise<T> {
  let last: unknown
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    try {
      return await load(attempt === 0 ? undefined : `?retry=${attempt}`)
    } catch (e) {
      last = e
      if (attempt < ATTEMPTS - 1) await sleep(RETRY_DELAY_MS * (attempt + 1))
    }
  }
  throw last
}

/**
 * A lazily-loaded view: retried import, a spinner while it arrives, and a boundary that
 * offers to try again — which remounts, and so re-runs the import.
 *
 * *label* names the view in the failure message, so "Could not load Data browser" says
 * which part of the app is missing rather than that the app is broken.
 */
export function lazyView(
  label: string,
  load: () => Promise<{ default: ComponentType<any> }>,
) {
  return function LazyView(props: Record<string, unknown>) {
    const [attempt, setAttempt] = useState(0)
    const retry = useCallback(() => setAttempt((n) => n + 1), [])

    // A *new* `lazy` per attempt, not one hoisted to module scope. React caches a lazy
    // component's settled promise — including a rejection — and re-renders throw the same
    // error forever, so remounting alone would leave "Try again" doing nothing at all.
    // Keying the new lazy on `attempt` is what actually re-runs the import.
    const Loaded = useMemo(() => lazy(() => retryImport(load)), [attempt])

    return (
      <ErrorBoundary key={attempt} label={label} onRetry={retry}>
        <Suspense fallback={
          <Box sx={{ display: 'flex', justifyContent: 'center', p: 6 }}>
            <CircularProgress size={28} />
          </Box>
        }>
          <Loaded {...props} />
        </Suspense>
      </ErrorBoundary>
    )
  }
}
