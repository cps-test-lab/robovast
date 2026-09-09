// A stable, legible colour per container name, shared by every surface that shows one — the live
// log panel's `[container]` prefixes and the merged run log's container column — so a container is
// the same colour wherever it is read.
//
// The palette avoids very light and very dark hues so lines stay readable on the log panel's
// `background.paper` in both themes. That constraint is why these colours are not the chip
// palette: a chip carries its own tinted ground, a log line sits on the panel's.

import { distinctColorer } from '@/lib/nameColor'

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

/** Colours for a known set of container names, distinct wherever the palette allows.
 *
 *  The distinctness is the point and not a nicety: `sut` and `robovast` hash to the same slot,
 *  and a column that paints two containers identically says they are the same container. See
 *  `lib/nameColor` for how collisions are resolved and why assignment is order-independent. */
export function containerColorer(names: Iterable<string>): (name: string) => string {
  return distinctColorer(names, CONTAINER_COLORS)
}
