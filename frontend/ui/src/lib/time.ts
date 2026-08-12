// Locale-aware time formatting for the UI. The backend hands out ISO-8601 UTC
// timestamps (a campaign's start time, etc.); render them in the viewer's own
// locale and timezone so times read naturally wherever the UI is opened.

/** Format an ISO-8601 timestamp as a local date+time, or '' when absent/invalid. */
export function formatLocalTime(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

/** Format a clock time `secondsFromNow` in the future, in the viewer's locale. */
export function formatLocalClock(secondsFromNow: number): string {
  return new Date(Date.now() + secondsFromNow * 1000).toLocaleTimeString()
}
