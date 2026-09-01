import { Component, useEffect, useState } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Collapse from '@mui/material/Collapse'
import Typography from '@mui/material/Typography'
import { RobovastError } from '@/lib/robovastClient'
import { markReloading, reloadAllowed, servedBuildDiffers } from '@/lib/servedBuild'

// React unmounts the whole tree when a render throws and nothing catches it, so before this
// existed one bad value blanked the entire app: the panels render campaign-supplied data
// (a scene with an unloadable mesh, a plot spec with a bad encoding, a Vega layout the user
// authored), and `KeepAlive` keeps every visited view mounted, so a broken one stayed
// mounted and kept throwing. Contain the failure at the view and at the panel, and give the
// operator the two things they actually need: what went wrong, and a way back.
//
// It is also what makes code-splitting safe. A dynamic import that fails over a flaky
// port-forward throws exactly here — a dropped chunk becomes a button rather than a white
// page. A chunk goes missing in two ways that want opposite remedies: a dropped request
// wants the import retried, a service restarted onto new asset hashes wants the document
// reloaded. So the panel asks the service which one happened (see lib/servedBuild) instead
// of offering the operator both buttons and the guess, and reloads itself when the answer
// is that it was updated — the case where nothing else this tab does will work either.

interface Props {
  /** Named in the message, so a nested boundary says which part failed. */
  label: string
  /** Re-attempt hook — for a lazy view this remounts, retrying the import. */
  onRetry?: () => void
  children: ReactNode
}

interface State {
  error: Error | null
  showDetail: boolean
}

/** True for the "a chunk did not arrive" family — a dropped request, or a build the service
 *  no longer has. Exported for its test: it is what decides whether the panel offers Reload,
 *  and each engine words the same failure differently, so a tightened pattern would silently
 *  drop the button from the case it exists for. */
export function isLoadFailure(error: Error): boolean {
  return /dynamically imported module|Importing a module script failed|Loading chunk/i
    .test(error.message)
}

/** Seconds shown before the tab reloads itself onto the new build.
 *
 *  Short, and shorter than the Admin page's countdown after a roll it started, because the
 *  two moments differ: there the page still works and the reload is prophylactic, here the
 *  view the user asked for is already missing. It is not zero only so that `Not now` is
 *  reachable — another view may be holding an unsaved edit, and losing it silently would be
 *  a worse surprise than the error panel this replaces. */
const RELOAD_COUNTDOWN_S = 3

/** What the build probe concluded. `unknown` is its own answer and not a synonym for
 *  `same`: a probe that could not run leaves both causes open, and saying "the connection
 *  dropped" on it would be a guess dressed as a finding. */
type Build = 'checking' | 'updated' | 'same' | 'unknown'

/**
 * The amber half of the panel: a chunk did not arrive, and this asks why.
 *
 * Its own component, and a function one, because it owns a probe and a countdown that the
 * boundary itself has no use for — the boundary must stay a class only because
 * `getDerivedStateFromError` has no hook. It is mounted fresh per failure (the boundary is
 * keyed on the retry count in lazyView), so the probe runs once per failure, not once per
 * render.
 */
function LoadFailureAlert({ label, onRetry }: { label: string; onRetry?: () => void }) {
  const [build, setBuild] = useState<Build>('checking')
  const [reloadIn, setReloadIn] = useState<number | null>(null)

  useEffect(() => {
    let live = true
    servedBuildDiffers().then(
      (differs) => {
        if (!live) return
        setBuild(differs ? 'updated' : 'same')
        // Confirmed stale, so reload without being asked: every hashed URL this tab holds
        // is a 404 now, and the next view the user opens fails the same way. The cooldown
        // is what keeps that from becoming a loop while a roll has two builds in the air.
        if (differs && reloadAllowed()) {
          markReloading()
          setReloadIn(RELOAD_COUNTDOWN_S)
        }
      },
      () => { if (live) setBuild('unknown') },
    )
    return () => { live = false }
  }, [])

  useEffect(() => {
    if (reloadIn === null) return
    if (reloadIn <= 0) {
      window.location.reload()
      return
    }
    const t = setTimeout(() => setReloadIn((n) => (n === null ? null : n - 1)), 1_000)
    return () => clearTimeout(t)
  }, [reloadIn])

  const updated = build === 'updated'
  const reload = () => window.location.reload()
  // The remedy that fits the answer goes first. `same` is the only branch where trying the
  // import again is worth anything -- the chunk demonstrably still exists -- so it leads
  // there and Reload leads everywhere else. A running countdown replaces both with itself:
  // stopping it must leave the panel standing rather than claim the tab is fine.
  const buttons = reloadIn !== null
    ? [
        <Button key="reload" color="inherit" size="small" onClick={reload}>Reload now</Button>,
        <Button key="later" color="inherit" size="small"
                onClick={() => setReloadIn(null)}>Not now</Button>,
      ]
    : [
        <Button key="reload" color="inherit" size="small" onClick={reload}>Reload</Button>,
        <Button key="retry" color="inherit" size="small" onClick={onRetry}>Try again</Button>,
      ]
  if (build === 'same' && reloadIn === null) buttons.reverse()

  return (
    <Alert severity="warning" action={buttons}>
      <AlertTitle>{updated ? 'RoboVAST was updated' : `Could not load ${label}`}</AlertTitle>
      {/* One sentence per answer, and each says what was actually established. */}
      {build === 'checking' &&
        'A part of the app did not download. Checking whether RoboVAST was updated…'}
      {updated && (reloadIn === null
        ? 'This tab is running the previous build, whose files the service no longer has. '
          + 'Reload to finish.'
        : `This tab is running the previous build. Reloading in ${reloadIn}s…`)}
      {build === 'same' &&
        'A part of the app did not download, but the service is still serving this build — '
        + 'the connection to it dropped.'}
      {build === 'unknown' &&
        'A part of the app did not download, and the service could not be reached to ask why. '
        + 'Either the connection dropped, or RoboVAST was updated and this tab is running the '
        + 'previous build.'}
    </Alert>
  )
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, showDetail: false }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept: the boundary shows a summary, and the console keeps the component stack that
    // says which child threw.
    console.error(`[${this.props.label}] render failed`, error, info.componentStack)
  }

  private retry = () => {
    this.setState({ error: null, showDetail: false })
    this.props.onRetry?.()
  }

  render() {
    const { error, showDetail } = this.state
    if (!error) return this.props.children

    // A missing chunk has its own panel: it asks the service what happened and can act on
    // the answer, where a render bug only ever has a stack to show.
    if (isLoadFailure(error)) {
      return (
        <Box sx={{ p: 2, maxWidth: 720 }}>
          <LoadFailureAlert label={this.props.label} onRetry={this.retry} />
        </Box>
      )
    }

    // A RobovastError already carries the service's own words (its `detail`); anything else
    // is a client-side bug and only has a JS message to show.
    const detail = error instanceof RobovastError
      ? `${error.status} — ${error.message}`
      : error.message

    return (
      <Box sx={{ p: 2, maxWidth: 720 }}>
        <Alert
          severity="error"
          action={
            // No Reload: a render bug would simply come back, and reloading only costs the
            // rest of the tab's state.
            <Button color="inherit" size="small" onClick={this.retry}>Try again</Button>
          }
        >
          <AlertTitle>{this.props.label} stopped working</AlertTitle>
          {detail}
          <Box sx={{ mt: 1 }}>
            <Button size="small" color="inherit"
                    onClick={() => this.setState({ showDetail: !showDetail })}>
              {showDetail ? 'Hide details' : 'Details'}
            </Button>
            <Collapse in={showDetail}>
              <Typography component="pre" variant="caption"
                          sx={{ mt: 1, whiteSpace: 'pre-wrap', overflowX: 'auto' }}>
                {error.stack ?? detail}
              </Typography>
            </Collapse>
          </Box>
        </Alert>
      </Box>
    )
  }
}
