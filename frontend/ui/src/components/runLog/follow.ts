// Whether a tailing log view follows its newest row, decided from the scroll container's geometry.
//
// A tail follows while the reader is at the bottom and pauses once they scroll up; scrolling back
// down resumes it. Every scroll event is judged the same way -- the view's own jump to the bottom
// lands at the bottom, so it keeps following without a flag to tell it apart from the reader's.

/** How far from the bottom still counts as at the bottom, in px. Not zero: fractional line metrics
 *  and a zoomed browser leave sub-pixel slack that must not read as the reader scrolling away. */
export const TAIL_SLACK_PX = 24

export interface ScrollGeometry {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

/** True when the view should follow: its bottom edge is within `slack` of the content's end
 *  (always so for content shorter than the viewport). */
export function tailFollows(g: ScrollGeometry, slack = TAIL_SLACK_PX): boolean {
  return g.scrollHeight - g.scrollTop - g.clientHeight <= slack
}
