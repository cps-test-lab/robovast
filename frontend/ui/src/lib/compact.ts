// Which layout a screen gets, decided in one place.
//
// Two separate questions, deliberately not one:
//
// - **Is the screen small?** (`compact`) A phone held upright is narrow, and held sideways is short;
//   either way there is no room for the permanent sidebar beside a page, nor for a run view's
//   panels around a scene. Decided by size: below 600 CSS px wide (MUI's `sm`) or below 500 high.
// - **Is it driven by a finger?** (`coarse`) Targets must be big enough to hit and nothing may need a
//   hover. Decided by the input, not the size: a narrow desktop window still has a mouse, and a
//   tablet that is not compact still has fingers.
//
// The media queries are exported so the theme and CSS can use the same ones; `useCompact` is the
// hook a component reads them through.

import useMediaQuery from '@mui/material/useMediaQuery'

/** Width below which the screen is compact: MUI's `sm` breakpoint. */
export const COMPACT_MAX_WIDTH_PX = 600
/** Height below which the screen is compact: a phone on its side. */
export const COMPACT_MAX_HEIGHT_PX = 500

export const COMPACT_QUERY =
  `(max-width: ${COMPACT_MAX_WIDTH_PX - 0.05}px), (max-height: ${COMPACT_MAX_HEIGHT_PX - 0.05}px)`
export const COARSE_QUERY = '(pointer: coarse)'
/** The coarse-pointer media query as an `sx`/styled key: `{ minHeight: 36, [ON_COARSE]: { minHeight:
 *  TOUCH_TARGET_PX } }` for a component whose own `sx` would otherwise override the theme's touch
 *  sizes. */
export const ON_COARSE = `@media ${COARSE_QUERY}`

/** The smallest a target may be under a finger, in CSS px (WCAG 2.5.5, and the platforms' own). */
export const TOUCH_TARGET_PX = 44

/** A dense height for a mouse that still gives a finger TOUCH_TARGET_PX: for a component that sets
 *  its own `minHeight` in `sx`, which would otherwise override the theme's touch sizes. */
export function denseHeight(px: number) {
  return { minHeight: px, [ON_COARSE]: { minHeight: TOUCH_TARGET_PX } }
}

export interface Compactness {
  /** A small screen: the overlay navigation and the phone layouts. */
  compact: boolean
  /** A finger as the primary pointer: large targets, tap instead of hover. */
  coarse: boolean
}

export function useCompact(): Compactness {
  // `noSsr` so the first render already has the answer: this app never renders on a server, and a
  // first frame laid out for the desktop would flash the permanent sidebar on every phone load.
  const compact = useMediaQuery(COMPACT_QUERY, { noSsr: true })
  const coarse = useMediaQuery(COARSE_QUERY, { noSsr: true })
  return { compact, coarse }
}
