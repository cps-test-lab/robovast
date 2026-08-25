import Chip from '@mui/material/Chip'

/**
 * Who says they started a campaign.
 *
 * Three decisions worth keeping:
 *
 * **The colour is derived, never stored.** A stable hash of the name picks a palette
 * entry, so the same person is the same colour for every viewer, in every session, with
 * nothing to coordinate or persist. Two people who choose the same name collide into one
 * colour — acceptable, because the names are self-declared anyway and among a handful of
 * colleagues it is self-correcting.
 *
 * **Never colour alone.** The name is always shown as text; the colour is a scanning aid
 * for "which of these are mine", not the information itself. That is also what keeps it
 * readable for colour-blind viewers and in both themes.
 *
 * **No name renders nothing at all.** A campaign launched without one shows no chip, rather
 * than a placeholder reading "unattributed": the row is scanned for the names that are
 * there, and a card per campaign saying nobody said is noise on every one of them. Absence
 * is still visible as absence, which is the part that mattered — what must never happen is
 * inventing a name.
 */

/**
 * Palette chosen to hold contrast in both the light and dark themes — the UI follows the
 * viewer's, so a colour tuned for one is unreadable in the other.
 */
const COLOURS = [
  '#1f6feb', // blue
  '#8250df', // purple
  '#bf3989', // magenta
  '#bc4c00', // orange
  '#1a7f37', // green
  '#0e7490', // teal
  '#9a6700', // ochre
  '#cf222e', // red
]

/** Stable across sessions and viewers: same name in, same colour out. */
function colourFor(name: string): string {
  let hash = 0
  for (let i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0
  }
  return COLOURS[Math.abs(hash) % COLOURS.length]
}

export function LaunchedBy({ name }: { name?: string | null }) {
  const trimmed = (name ?? '').trim()
  if (!trimmed) return null

  const colour = colourFor(trimmed)
  return (
    <Chip
      size="small"
      label={trimmed}
      variant="outlined"
      sx={{
        borderColor: colour,
        color: colour,
        // The tint carries the scanning cue; the text carries the information.
        bgcolor: `${colour}14`,
        height: 20,
        '& .MuiChip-label': { px: 0.75, fontSize: '0.75rem' },
      }}
    />
  )
}
