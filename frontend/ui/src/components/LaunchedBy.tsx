import Chip from '@mui/material/Chip'
import { CHIP_COLOURS, slotOf } from '@/lib/nameColor'

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

/** Stable across sessions and viewers: same name in, same colour out. Not collision-avoiding,
 * and it does not need to be — one campaign has one launcher, so two names are never compared
 * side by side the way a job list's nodes are. */
function colourFor(name: string): string {
  return CHIP_COLOURS[slotOf(name, CHIP_COLOURS.length)]
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
