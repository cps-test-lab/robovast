import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Collapse from '@mui/material/Collapse'
import Typography from '@mui/material/Typography'
import { RobovastError } from '@/lib/robovastClient'

// React unmounts the whole tree when a render throws and nothing catches it, so before this
// existed one bad value blanked the entire app: the panels render campaign-supplied data
// (a scene with an unloadable mesh, a plot spec with a bad encoding, a Vega layout the user
// authored), and `KeepAlive` keeps every visited view mounted, so a broken one stayed
// mounted and kept throwing. Contain the failure at the view and at the panel, and give the
// operator the two things they actually need: what went wrong, and a way back.
//
// It is also what makes code-splitting safe. A dynamic import that fails over a flaky
// port-forward throws exactly here, and `Try again` re-attempts it — a dropped chunk becomes
// a button rather than a white page.

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

/** True for the "a chunk did not arrive" family, which retrying usually fixes. */
function isLoadFailure(error: Error): boolean {
  return /dynamically imported module|Importing a module script failed|Loading chunk/i
    .test(error.message)
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

    const transient = isLoadFailure(error)
    // A RobovastError already carries the service's own words (its `detail`); anything else
    // is a client-side bug and only has a JS message to show.
    const detail = error instanceof RobovastError
      ? `${error.status} — ${error.message}`
      : error.message

    return (
      <Box sx={{ p: 2, maxWidth: 720 }}>
        <Alert
          severity={transient ? 'warning' : 'error'}
          action={<Button color="inherit" size="small" onClick={this.retry}>Try again</Button>}
        >
          <AlertTitle>
            {transient
              ? `Could not load ${this.props.label}`
              : `${this.props.label} stopped working`}
          </AlertTitle>
          {transient
            ? 'A part of the app did not download — usually a dropped connection to the service.'
            : detail}
          {!transient && (
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
          )}
        </Alert>
      </Box>
    )
  }
}
