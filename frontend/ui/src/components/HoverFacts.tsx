import Box from '@mui/material/Box'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import type { TooltipProps } from '@mui/material/Tooltip'

/**
 * A hover panel of plain label→value facts.
 *
 * The shape that recurs whenever something on screen is a summary and the facts behind it
 * are worth one hover but not one column: an origin, a revision, an image digest. It is
 * deliberately only that — a caption, then rows — because the moment a hover panel starts
 * explaining itself in prose it stops being readable at a glance.
 *
 * Three decisions worth keeping:
 *
 * **A fact with no value is omitted, never rendered as a dash.** A row reading `—` claims
 * the fact exists and is empty; leaving it out says the panel never had it. Those are
 * different, and only one of them is true.
 *
 * **The trigger carries a dotted underline and a help cursor**, matching the meta hovers in
 * `DetailsBox`, so there is something to aim at. A hover nobody discovers is not a feature.
 *
 * **No colours of its own.** It inherits the tooltip's surface (the theme's `MuiPaper`
 * override already styles it) and uses semantic roles for text, so it holds up in both
 * themes without a single literal hex.
 */

export type Fact = {
  label: string
  /** Rendered as-is. Falsy values drop the row entirely — see the note above. */
  value?: string | null
}

export function HoverFacts({
  title,
  facts,
  placement = 'top',
  children,
}: {
  /** Names what the facts are about, e.g. "Launched from". Omit for a bare list. */
  title?: string
  facts: Fact[]
  placement?: TooltipProps['placement']
  /** The trigger. Wrapped as-is; give it the affordance with {@link hoverTriggerSx}. */
  children: React.ReactElement
}) {
  const shown = facts.filter((f) => f.value)
  if (!shown.length) return children

  const cell = { padding: '1px 8px 1px 0', whiteSpace: 'nowrap' as const, fontSize: 11 }
  return (
    <Tooltip
      placement={placement}
      title={
        <Box>
          {title ? (
            <Typography sx={{ fontSize: 11, fontWeight: 600, mb: 0.5 }}>{title}</Typography>
          ) : null}
          <Box component="table" sx={{ borderCollapse: 'collapse' }}>
            <tbody>
              {shown.map((f) => (
                <tr key={f.label}>
                  <td style={{ ...cell, textAlign: 'left', opacity: 0.7 }}>{f.label}</td>
                  <td style={{ ...cell, textAlign: 'left' }}>{f.value}</td>
                </tr>
              ))}
            </tbody>
          </Box>
        </Box>
      }
    >
      {children}
    </Tooltip>
  )
}

/** The trigger affordance: dotted underline + help cursor, as used by `DetailsBox`'s meta. */
export const hoverTriggerSx = {
  cursor: 'help',
  textDecoration: 'underline dotted',
  textUnderlineOffset: 3,
} as const
