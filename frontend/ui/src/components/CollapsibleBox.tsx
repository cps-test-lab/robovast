import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Collapse from '@mui/material/Collapse'
import IconButton from '@mui/material/IconButton'
import KeyboardArrowDownRoundedIcon from '@mui/icons-material/KeyboardArrowDownRounded'
import KeyboardArrowUpRoundedIcon from '@mui/icons-material/KeyboardArrowUpRounded'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

// The one collapsible-block shape the campaign view uses: a header that stays visible and a
// body that folds away under it. Everything foldable inside a campaign card goes through
// here — the controller failure, the jobs list, each job's log, the campaign log — so they
// read as one family instead of a bordered box next to a bare text button.
//
// Clicking anywhere on the header toggles it: the chevron is there to say the block folds,
// not to be the only target. The body is deliberately not a click target — it holds log text
// and tracebacks people drag across to copy, and a collapse mid-selection would take the text
// away as they read it.
//
// The header's title and meta are selectable all the same. A folded block is often the only
// place its subject is named on screen, so the name has to be copyable without unfolding
// anything first; a click that ends a drag-selection is ignored instead of toggling. The
// header's blank space stays unselectable — dragging across a click target should not paint.

export type BoxTone = 'neutral' | 'error'

export function CollapsibleBox({
  title,
  meta,
  leading,
  actions,
  note,
  tone = 'neutral',
  variant = 'card',
  subheader,
  flush = false,
  open,
  onToggle,
  children,
}: {
  title: ReactNode
  // Right-aligned secondary text in the header (a count, a state) — visible while collapsed,
  // which is what makes a folded block worth leaving folded.
  meta?: ReactNode
  // Rendered before the title, for a status chip or icon.
  leading?: ReactNode
  // Controls in the header, before the chevron — a job's Stop button. Its own slot rather
  // than part of `leading`, because a control inside the header's click target would toggle
  // the block as well as fire: the wrapper's own handler is stopped for this region.
  actions?: ReactNode
  // Always-visible text under the header row, independent of `open`. For the reason a thing
  // is stuck — that has to be readable without unfolding anything. NOT a click target: it
  // holds tracebacks and log lines people drag across to copy, and folding the block out from
  // under a selection takes the text away as they read it.
  note?: ReactNode
  // Always-visible content under the header row that IS part of the header's click target.
  // The sibling of `note`, and the distinction is the whole reason both exist: this is for
  // something a reader would naturally click *at* — the batches meter, whose bar is the
  // obvious thing to press to see the batches in detail — where `note` is for something they
  // would select. Loosening `note` instead would have collapsed open tracebacks mid-drag.
  subheader?: ReactNode
  // Drop the header's horizontal padding, so a full-width `subheader` (a meter) lines up with
  // whatever sits above and below the block rather than being inset by it.
  flush?: boolean
  tone?: BoxTone
  // `card` is a standalone bordered block; `row` is a flat entry inside another block's body
  // (the job rows), which supplies the separation itself instead of nesting a second border.
  variant?: 'card' | 'row'
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const card = variant === 'card'
  const error = tone === 'error'
  // See the header note: a click that ends a drag-selection is a copy, not a toggle. A plain
  // click cannot trip it, because its own mousedown collapses any earlier selection first.
  const toggleUnlessSelecting = () => {
    if (window.getSelection()?.toString()) return
    onToggle()
  }
  return (
    <Box
      sx={{
        ...(card
          ? {
              border: 1,
              borderColor: error ? 'error.main' : 'divider',
              borderRadius: 1,
              // Keeps the header's tint and the body's log background inside the rounded
              // corners instead of squaring them off.
              overflow: 'hidden',
            }
          : {}),
      }}
    >
      <Box
        onClick={toggleUnlessSelecting}
        sx={{
          cursor: 'pointer',
          userSelect: 'none',
          px: flush ? 0 : 1,
          py: 0.25,
          bgcolor: error ? 'error.main' : card ? 'action.hover' : 'transparent',
          color: error ? 'error.contrastText' : 'inherit',
          '&:hover': { bgcolor: error ? 'error.main' : 'action.selected' },
        }}
      >
        <Stack direction="row" alignItems="center" spacing={1}>
          {leading}
          <Typography
            variant="caption"
            component="span"
            sx={{ fontWeight: 600, minWidth: 0, overflowWrap: 'anywhere', userSelect: 'text' }}
          >
            {title}
          </Typography>
          <Box flexGrow={1} />
          {meta ? (
            <Typography
              variant="caption"
              component="span"
              sx={{
                color: error ? 'inherit' : 'text.secondary',
                whiteSpace: 'nowrap',
                userSelect: 'text',
              }}
            >
              {meta}
            </Typography>
          ) : null}
          {actions ? (
            // Clicks here are the action's, not the header's: without this a Stop button would
            // also fold the block it sits on, so the log you wanted to read closes as you act.
            <Box
              sx={{ display: 'flex', alignItems: 'center', cursor: 'auto' }}
              onClick={(e) => e.stopPropagation()}
            >
              {actions}
            </Box>
          ) : null}
          <IconButton
            size="small"
            // Named after the block, so a screen reader gets "Show Jobs" rather than five
            // identical "Expand" buttons in a row.
            aria-label={`${open ? 'Hide' : 'Show'} ${typeof title === 'string' ? title : 'details'}`}
            aria-expanded={open}
            // The wrapper toggles too, so both handlers would fire for one click on the button.
            onClick={(e) => {
              e.stopPropagation()
              onToggle()
            }}
            sx={{ color: 'inherit', p: 0.25 }}
          >
            {open ? (
              <KeyboardArrowUpRoundedIcon fontSize="small" />
            ) : (
              <KeyboardArrowDownRoundedIcon fontSize="small" />
            )}
          </IconButton>
        </Stack>
        {/* Inside the header's click target, unlike `note` below it: clicking the meter folds
            the block, which is the affordance a bar with no other purpose invites. */}
        {subheader ? <Box sx={{ pb: 0.25 }}>{subheader}</Box> : null}
        {note ? (
          <Box sx={{ pb: 0.5, cursor: 'auto', userSelect: 'text' }} onClick={(e) => e.stopPropagation()}>
            {note}
          </Box>
        ) : null}
      </Box>
      <Collapse in={open} unmountOnExit>
        {/* The separator lives here rather than on the header, so a collapsed block ends on
            its own tinted bar instead of a dangling line. */}
        <Box sx={{ borderTop: 1, borderColor: 'divider' }}>{children}</Box>
      </Collapse>
    </Box>
  )
}
