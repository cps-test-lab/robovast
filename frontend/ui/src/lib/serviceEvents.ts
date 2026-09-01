// What the events panel decides, as functions.
//
// Markup stays in `pages/admin/`; this is the same split `lib/runMeter.ts` and `lib/eta.ts`
// make, and the reason those have tests while the panels do not.

import type { ServiceEvent } from './robovastClient'

/** Palette name for a severity, defaulting rather than throwing on one this build predates. */
export function eventTone(severity: string): 'error' | 'warning' | 'info' | 'success' {
  switch (severity) {
    case 'error': return 'error'
    case 'warning': return 'warning'
    case 'success': return 'success'
    default: return 'info'
  }
}

/**
 * Newest first, for reading.
 *
 * The route serves oldest-first from a cursor, which is what a caller *resuming* a position
 * wants: it holds a seq and asks for what came after. A person opening the panel wants the
 * opposite — what just happened — so the reversal belongs here rather than in the API.
 */
export function newestFirst(events: readonly ServiceEvent[]): ServiceEvent[] {
  return [...events].sort((a, b) => b.seq - a.seq)
}

/**
 * Whether there is more to ask for.
 *
 * A short page is the whole record, so offering "show more" there would promise something the
 * next request cannot deliver. Only a full page implies the record was cut off.
 */
export function hasMore(returned: number, limit: number): boolean {
  return returned >= limit
}
