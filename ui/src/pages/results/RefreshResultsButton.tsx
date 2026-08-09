import Badge from '@mui/material/Badge'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import Tooltip from '@mui/material/Tooltip'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'

// What the Results container hands each sub-view so it can offer the reload. The campaign list is
// frozen while a view is open (see ResultsPage) — arriving at the Explorer catches it up on its own,
// so what is left for this button is a campaign that finishes while the view is *already* open, and
// `stale` is what says one is waiting.
export interface ResultsRefresh {
  /** Adopt the latest campaign list into all three views. */
  refresh: () => void
  /** Ready campaigns the views are not showing yet. */
  newCount: number
  /** The service's list differs from the one on screen (new campaigns, or deleted ones). */
  stale: boolean
  /** A refetch is in flight. */
  busy: boolean
}

// Always present so the reload is where the user last left it, and highlighted only when it would
// actually change something — a permanently loud button stops being a signal.
export function RefreshResultsButton({ state }: { state: ResultsRefresh }) {
  const tip = state.stale
    ? state.newCount
      ? `${state.newCount} new campaign${state.newCount > 1 ? 's' : ''} ready — click to show`
      : 'The campaign list changed — click to reload'
    : 'Reload the campaign list'

  return (
    <Tooltip title={tip}>
      {/* Kept enabled while busy so the tooltip stays reachable; the click is a no-op refetch. */}
      <IconButton
        size="small"
        aria-label="Reload campaign list"
        color={state.stale ? 'primary' : 'default'}
        onClick={state.refresh}
      >
        <Badge
          color="primary"
          variant={state.newCount ? 'standard' : 'dot'}
          badgeContent={state.newCount || null}
          invisible={!state.stale}
        >
          {state.busy ? <CircularProgress size={18} /> : <RefreshRoundedIcon fontSize="small" />}
        </Badge>
      </IconButton>
    </Tooltip>
  )
}
