import React from 'react'
import { createRoot } from 'react-dom/client'
import CssBaseline from '@mui/material/CssBaseline'
import { ThemeProvider } from '@mui/material/styles'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './App'
import { DialogProvider } from './components/DialogProvider'
import { ErrorBoundary } from './components/ErrorBoundary'
import { appTheme } from './theme'

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
})

// Vite fires this when a preloaded chunk cannot be fetched, and its default action is to
// reload the page. Over a port-forward that turns one dropped request into a full reload
// of an app the operator was in the middle of using — and, if the drop persists, a loop.
// `lazyView` already retries the import and shows a boundary with a button, so suppress the
// default and let the failure reach that instead.
window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault()
  console.warn('chunk preload failed; the view will retry on demand', event)
})

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider theme={appTheme}>
        <CssBaseline />
        {/* Last resort: the shell itself (sidebar, providers). A view that throws is caught
            by its own boundary; this catches everything outside them. */}
        <ErrorBoundary label="RoboVAST">
          <DialogProvider>
            <App />
          </DialogProvider>
        </ErrorBoundary>
      </ThemeProvider>
    </QueryClientProvider>
  </React.StrictMode>,
)
