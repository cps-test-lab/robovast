// The log's controls: text (substring or regex), one severity button, and one dropdown over every
// source the loaded log actually has. Shared by both hosts, so a filter learnt in one place works
// in the other.
//
// Nothing here carries a text label except the search box's placeholder. The bar sits above a
// log that is itself dense monospace text, and words in the chrome compete with the words being
// read; every control says what it is on hover instead.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import IconButton from '@mui/material/IconButton'
import InputAdornment from '@mui/material/InputAdornment'
import ListItemText from '@mui/material/ListItemText'
import ListSubheader from '@mui/material/ListSubheader'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import ClearRoundedIcon from '@mui/icons-material/ClearRounded'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import FilterListOffRoundedIcon from '@mui/icons-material/FilterListOffRounded'
import FilterListRoundedIcon from '@mui/icons-material/FilterListRounded'
import KeyboardArrowDownRoundedIcon from '@mui/icons-material/KeyboardArrowDownRounded'
import KeyboardArrowUpRoundedIcon from '@mui/icons-material/KeyboardArrowUpRounded'
import WarningAmberRoundedIcon from '@mui/icons-material/WarningAmberRounded'
import WrapTextRoundedIcon from '@mui/icons-material/WrapTextRounded'
import type { Facets, HighlightMode, LogFilter } from './logFilter'

/** off -> on -> only -> off. One control for two questions ("colour these" / "show only these")
 *  because they are the same question asked with different force. */
const NEXT: Record<HighlightMode, HighlightMode> = { off: 'on', on: 'only', only: 'off' }

/** Hover text per state. The button carries no label, so this is the only place that says what
 *  it does — hence naming both the current state and what a click will do. */
const HIGHLIGHT_TITLE: Record<HighlightMode, string> = {
  off: 'Warnings and errors not highlighted — click to highlight them',
  on: 'Warnings and errors highlighted — click to show only those lines',
  only: 'Showing only warnings and errors — click to turn highlighting off',
}

type FacetKind = 'containers' | 'nodes' | 'sources'

const FACET_TITLE: Record<FacetKind, string> = {
  containers: 'Container',
  nodes: 'ROS node',
  sources: 'Source',
}

export function LogFilterBar({
  filter,
  onChange,
  facets,
  invalidRegex,
  onStepSeverity,
  wrap,
  onWrapChange,
  wrapDisabledReason,
  notes,
}: {
  filter: LogFilter
  onChange: (filter: LogFilter) => void
  facets: Facets
  invalidRegex?: boolean
  /** Provided only where there is a clock to seek; absent in the Explorer. */
  onStepSeverity?: (dir: 1 | -1) => void
  wrap?: boolean
  onWrapChange?: (wrap: boolean) => void
  /** Non-empty when wrapping is refused, and why — shown instead of the normal tooltip. */
  wrapDisabledReason?: string
  /** Things that change how the log should be read (no clock map, hidden lines, a capped load).
   *  Surfaced here as one icon so the list itself gets the whole panel; absent when there is
   *  nothing to say, which is the common case. */
  notes?: string[]
}) {
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)

  const toggle = (kind: FacetKind, value: string) => {
    const current = filter[kind]
    onChange({
      ...filter,
      [kind]: current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value],
    })
  }

  const selectedCount =
    filter.containers.length + filter.nodes.length + filter.sources.length

  const groups = useMemo(
    () =>
      (['containers', 'nodes', 'sources'] as FacetKind[])
        // A single value is not a choice; showing it as one implies there are others.
        .filter((kind) => facets[kind].length > 1)
        .map((kind) => ({ kind, values: facets[kind] })),
    [facets],
  )

  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 0.5,
        px: 0.5,
        py: 0.25,
        flexShrink: 0,
        borderBottom: 1,
        borderColor: 'divider',
      }}
    >
      <TextField
        value={filter.text}
        onChange={(e) => onChange({ ...filter, text: e.target.value })}
        placeholder={filter.regex ? 'regex…' : 'filter…'}
        size="small"
        error={!!invalidRegex}
        helperText={invalidRegex ? 'invalid regex' : undefined}
        variant="standard"
        sx={{ flexGrow: 1, minWidth: 90, '& .MuiInputBase-input': { fontSize: 12, py: 0.25 } }}
        InputProps={{
          endAdornment: (
            <InputAdornment position="end">
              {filter.text ? (
                <IconButton size="small" onClick={() => onChange({ ...filter, text: '' })}>
                  <ClearRoundedIcon sx={{ fontSize: 14 }} />
                </IconButton>
              ) : null}
              <Tooltip title={filter.regex ? 'Regular expression' : 'Plain substring'}>
                <Chip
                  label=".*"
                  size="small"
                  color={filter.regex ? 'primary' : 'default'}
                  variant={filter.regex ? 'filled' : 'outlined'}
                  onClick={() => onChange({ ...filter, regex: !filter.regex })}
                  sx={{ height: 16, fontSize: 10, fontFamily: 'monospace' }}
                />
              </Tooltip>
            </InputAdornment>
          ),
        }}
      />

      {/* One icon, no label: the rows' own yellow and red already say which severity is which,
          so a word for each would only repeat them. The icon takes the colour of the state it is
          in, and `only` adds a filled ring so the stronger state does not look like the milder
          one at a glance. */}
      <Tooltip title={HIGHLIGHT_TITLE[filter.highlight]}>
        <IconButton
          size="small"
          aria-label={HIGHLIGHT_TITLE[filter.highlight]}
          onClick={() => onChange({ ...filter, highlight: NEXT[filter.highlight] })}
          sx={{
            flexShrink: 0,
            p: 0.25,
            color: filter.highlight === 'off' ? 'text.disabled' : '#b58900',
            bgcolor: filter.highlight === 'only' ? '#b5890022' : undefined,
            border: filter.highlight === 'only' ? '1px solid' : '1px solid transparent',
            borderColor: filter.highlight === 'only' ? '#b58900' : 'transparent',
          }}
        >
          <WarningAmberRoundedIcon sx={{ fontSize: 16 }} />
        </IconButton>
      </Tooltip>

      {onStepSeverity ? (
        <>
          <Tooltip title="Previous warning or error (seeks playback)">
            <IconButton size="small" onClick={() => onStepSeverity(-1)}>
              <KeyboardArrowUpRoundedIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
          <Tooltip title="Next warning or error (seeks playback)">
            <IconButton size="small" onClick={() => onStepSeverity(1)}>
              <KeyboardArrowDownRoundedIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
        </>
      ) : null}

      {onWrapChange ? (
        <Tooltip
          title={
            wrapDisabledReason ||
            (wrap ? 'Word wrap on — click for one line per entry'
                  : 'Word wrap off — click to wrap long lines')
          }
        >
          <span>
            <IconButton
              size="small"
              aria-label="toggle word wrap"
              disabled={!!wrapDisabledReason}
              onClick={() => onWrapChange(!wrap)}
              sx={{ flexShrink: 0, p: 0.25, color: wrap ? 'primary.main' : 'text.disabled' }}
            >
              <WrapTextRoundedIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </span>
        </Tooltip>
      ) : null}

      {notes?.length ? (
        <Tooltip
          title={
            <Box component="span" sx={{ display: 'block', whiteSpace: 'pre-line' }}>
              {notes.join('\n')}
            </Box>
          }
        >
          <InfoOutlinedIcon sx={{ fontSize: 15, color: 'warning.main', flexShrink: 0 }} />
        </Tooltip>
      ) : null}

      {groups.length ? (
        <>
          <Tooltip title="Filter by container, ROS node or source">
            <Chip
              icon={<FilterListRoundedIcon sx={{ fontSize: 14 }} />}
              label={selectedCount || 'all'}
              size="small"
              variant={selectedCount ? 'filled' : 'outlined'}
              color={selectedCount ? 'primary' : 'default'}
              onClick={(e) => setMenuAnchor(e.currentTarget)}
              sx={{ height: 18, fontSize: 10, flexShrink: 0 }}
            />
          </Tooltip>
          <Menu
            anchorEl={menuAnchor}
            open={!!menuAnchor}
            onClose={() => setMenuAnchor(null)}
            slotProps={{ paper: { sx: { maxHeight: 420 } } }}
          >
            {/* Clearing every selection at once, at the top where it is found without scrolling
                past the list it undoes. Icon only, like the rest of the bar. Disabled rather than
                hidden when nothing is selected: a control that comes and goes is one you have to
                look for. */}
            <Box sx={{ display: 'flex', justifyContent: 'flex-end', px: 0.5, pb: 0.25 }}>
              <Tooltip title={selectedCount ? 'Clear all source filters' : 'No source filters set'}>
                <span>
                  <IconButton
                    size="small"
                    aria-label="Clear all source filters"
                    disabled={!selectedCount}
                    onClick={() =>
                      onChange({ ...filter, containers: [], nodes: [], sources: [] })
                    }
                  >
                    <FilterListOffRoundedIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </span>
              </Tooltip>
            </Box>
            <Divider />
            {groups.map(({ kind, values }, gi) => [
              gi ? <Divider key={`d-${kind}`} /> : null,
              <ListSubheader key={`h-${kind}`} sx={{ lineHeight: '24px', fontSize: 11 }}>
                {FACET_TITLE[kind]}
              </ListSubheader>,
              ...values.map((facet) => (
                <MenuItem
                  key={`${kind}-${facet.value}`}
                  dense
                  onClick={() => toggle(kind, facet.value)}
                >
                  <Checkbox
                    size="small"
                    checked={filter[kind].includes(facet.value)}
                    sx={{ p: 0.25, mr: 0.5 }}
                  />
                  <ListItemText
                    primary={facet.value || '(unknown)'}
                    primaryTypographyProps={{ fontSize: 12 }}
                  />
                  {/* The count is why this is a summary of the run and not just a list. */}
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    {facet.count}
                  </Typography>
                </MenuItem>
              )),
            ])}
          </Menu>
        </>
      ) : null}
    </Box>
  )
}
