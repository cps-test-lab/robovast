// One stable colour per name, and the machinery for keeping a set of them apart.
//
// Three surfaces colour a name so it can be scanned rather than read: the `[container]`
// prefixes in the log panels, the user who launched a campaign, and the node a job landed on.
// Two of them had grown their own copy of the same hash; a third copy would have left no
// answer to which one is "the" hash, and a name that is one colour here and another there is
// worse than an uncoloured one.
//
// Palettes stay with the surfaces that own them — a log line and a chip have different
// legibility constraints — so what is shared is the assignment, not the colours.

/** Stable across sessions and viewers: same name in, same slot out. */
export function slotOf(name: string, slots: number): number {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0
  return Math.abs(h) % slots
}

/** A colourer for a KNOWN set of names, distinct wherever the palette allows.
 *
 * The hash alone is not enough: it is a hash, so two names on one screen can land on the same
 * entry, and two things painted identically read as one thing — a column that gave two
 * containers the same colour said they were the same container. So each name takes its hashed
 * slot when free and the next free slot otherwise; with no more names than colours, none
 * repeats.
 *
 * Assignment walks the names in sorted order rather than order of appearance, so a view whose
 * names arrive over time does not recolour what is already on screen when a later one shows
 * up: only a name that actually collides moves.
 *
 * A name the colourer was not built with still gets its hashed colour rather than none, so a
 * caller need not rebuild it to render one more row. */
export function distinctColorer(
  names: Iterable<string>,
  palette: readonly string[],
): (name: string) => string {
  const taken = new Set<number>()
  const assigned = new Map<string, number>()
  for (const name of [...new Set(names)].sort()) {
    const home = slotOf(name, palette.length)
    let slot = home
    for (let i = 0; i < palette.length && taken.has(slot); i++)
      slot = (home + i + 1) % palette.length
    // More names than colours: the palette is exhausted, so fall back to the hashed slot and
    // accept the repeat rather than leaving the name uncoloured.
    if (taken.has(slot)) slot = home
    taken.add(slot)
    assigned.set(name, slot)
  }
  return (name) => palette[assigned.get(name) ?? slotOf(name, palette.length)]
}

/** The chip palette: tuned to hold contrast in BOTH themes, since the UI follows the viewer's
 * and a colour picked for one is unreadable in the other. Shared by the surfaces that render a
 * name as a tinted chip — who launched a campaign, and which node a job is on. */
export const CHIP_COLOURS = [
  '#1f6feb', // blue
  '#8250df', // purple
  '#bf3989', // magenta
  '#bc4c00', // orange
  '#1a7f37', // green
  '#0e7490', // teal
  '#9a6700', // ochre
  '#cf222e', // red
] as const
