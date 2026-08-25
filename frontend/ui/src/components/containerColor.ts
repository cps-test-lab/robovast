// A stable, legible colour per container name, shared by every surface that shows one — the live
// log panel's `[container]` prefixes and the merged run log's container column — so a container is
// the same colour wherever it is read.
//
// The palette avoids very light and very dark hues so lines stay readable on the log panel's
// `background.paper` in both themes.

const CONTAINER_COLORS = [
  '#2e9599', // teal
  '#c9611e', // orange
  '#8250df', // purple
  '#2f7d31', // green
  '#1f6feb', // blue
  '#c2185b', // pink
  '#8a6d1a', // olive
  '#5a6b7a', // slate
]

function slotOf(name: string): number {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0
  return Math.abs(h) % CONTAINER_COLORS.length
}

/** Colours for a known set of container names, distinct wherever the palette allows.
 *
 *  The hash alone is not enough: it is a hash, so two names in one log can land on the same entry
 *  -- `sut` and `robovast` did -- and a column that paints two containers identically says they
 *  are the same container. So each name takes its hashed slot when free and the next free slot
 *  otherwise; a log with at most `CONTAINER_COLORS.length` containers then never repeats a colour.
 *
 *  Assignment walks the names in sorted order rather than order of appearance, so a panel whose
 *  containers arrive over time does not recolour what is already on screen when a later one shows
 *  up: only a name that actually collides moves. */
export function containerColorer(names: Iterable<string>): (name: string) => string {
  const taken = new Set<number>()
  const assigned = new Map<string, number>()
  for (const name of [...new Set(names)].sort()) {
    const home = slotOf(name)
    let slot = home
    for (let i = 0; i < CONTAINER_COLORS.length && taken.has(slot); i++)
      slot = (home + i + 1) % CONTAINER_COLORS.length
    // More containers than colours: the palette is exhausted, so fall back to the hashed slot and
    // accept the repeat rather than leaving the name uncoloured.
    if (taken.has(slot)) slot = home
    taken.add(slot)
    assigned.set(name, slot)
  }
  return (name) => CONTAINER_COLORS[assigned.get(name) ?? slotOf(name)]
}
