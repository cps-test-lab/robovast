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

export type BoxTone = 'neutral' | 'error'

export function CollapsibleBox({
  title,
  meta,
  leading,
  note,
  tone = 'neutral',
  variant = 'card',
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
  // Always-visible text under the header row, independent of `open`. For the reason a thing
  // is stuck — that has to be readable without unfolding anything.
  note?: ReactNode
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
        onClick={onToggle}
        sx={{
          cursor: 'pointer',
          userSelect: 'none',
          px: 1,
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
            sx={{ fontWeight: 600, minWidth: 0, overflowWrap: 'anywhere' }}
          >
            {title}
          </Typography>
          <Box flexGrow={1} />
          {meta ? (
            <Typography
              variant="caption"
              component="span"
              sx={{ color: error ? 'inherit' : 'text.secondary', whiteSpace: 'nowrap' }}
            >
              {meta}
            </Typography>
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
