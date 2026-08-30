import React from 'react'
import { createRoot } from 'react-dom/client'
import CssBaseline from '@mui/material/CssBaseline'
import { ThemeProvider } from '@mui/material/styles'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './App'
import { CampaignStreamProvider } from './components/CampaignStreamProvider'
import { DialogProvider } from './components/DialogProvider'
import { ToastProvider } from './components/ToastProvider'
import { ErrorBoundary } from './components/ErrorBoundary'
import { appTheme } from './theme'

// Refetch-on-focus is off by default because most of what this app fetches does not change:
// a finished campaign's results, a workspace's files, the config schema. Re-reading them
// every time the window is touched costs a round trip — over a port-forward, a slow one —
// and re-running a Results SQL query would be worse than pointless.
//
// The live readings opt back in individually: a running campaign's phase and job list
// (Monitor), the sidebar's resource meters, the Results tab's campaign listing. They poll on
// a timer, that timer is suspended while the tab is hidden, and so returning to the tab is
// precisely when they must read once. See `Staying up to date` in docs/web_ui.rst.
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
})

// Vite fires this when a chunk cannot be fetched. Its default action is to **throw** — the
// reload in Vite's docs is a suggested handler, not the default — and that throw is the whole
// error path: it rejects the `import()`, which `lazyView` retries and then shows a boundary
// for. So this listener only logs.
//
// It used to call `event.preventDefault()`, on the belief that the default was a reload.
// Vite's helper is `baseModule().catch(handlePreloadError)` and `handlePreloadError` rethrows
// only if the event was *not* default-prevented, so preventing it made the import resolve
// with `undefined` instead. Every `import(...).then((m) => ({ default: m.Page }))` in the app
// then threw `m is undefined` — a TypeError, which reads as a code bug rather than a missing
// chunk, so the retry never ran and the boundary showed "stopped working" with a JS message.
// A service restarted onto a new build (new asset hashes) hit exactly that.
window.addEventListener('vite:preloadError', (event) => {
  console.warn('chunk preload failed; lazyView will retry and then offer a reload', event)
})

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider theme={appTheme}>
        {/* `enableColorScheme` puts `color-scheme: dark` on <html>, which is what tells the
            browser to paint its own widgets dark. Without it the theme is dark but the native
            scrollbars are not: Safari on macOS draws a light-grey track down the side of every
            scrolling panel in the run view. It also covers the other UA-painted surfaces --
            form controls, the text caret, and overscroll. */}
        <CssBaseline enableColorScheme />
        {/* Last resort: the shell itself (sidebar, providers). A view that throws is caught
            by its own boundary; this catches everything outside them. */}
        <ErrorBoundary label="RoboVAST">
          <DialogProvider>
            {/* ToastProvider wraps CampaignStreamProvider, not the other way round: the stream
                provider announces campaigns starting and ending, so it calls useToasts(). */}
            <ToastProvider>
              {/* Above App rather than inside the Campaigns page: the page is KeepAlive-mounted,
                  so it only exists once it has been visited, and a deep link elsewhere would
                  leave the campaign list unstreamed for anything app-wide that reads it. One
                  EventSource either way -- the provider opens it once. */}
              <CampaignStreamProvider>
                <App />
              </CampaignStreamProvider>
            </ToastProvider>
          </DialogProvider>
        </ErrorBoundary>
      </ThemeProvider>
    </QueryClientProvider>
  </React.StrictMode>,
)
