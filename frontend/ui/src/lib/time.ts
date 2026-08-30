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

/** How long ago, coarsely: "just now", "12 min ago", "3 h ago", "5 d ago", "8 wk ago".
 *
 *  For a campaign LIST, where the question is "which of these is recent" rather than "at what
 *  wall-clock time did this start". The absolute time answers a question nobody asked of a row —
 *  and a campaign id already carries a `-YYYY-MM-DD-HHMMSS` stamp, so printing it again beside the
 *  id is the same fact twice. (Only *nearly* the same: the id is stamped from the service host's
 *  naive local clock while this reads a UTC `created_at` in the viewer's zone, so on a cluster the
 *  two differ by the offset and this is the one that is right for the reader. That is exactly why
 *  the absolute time stays one hover away instead of being dropped.)
 *
 *  Coarse on purpose: past a day, minutes are noise, and a row is scanned rather than read.
 *  Returns '' for an absent or unparseable timestamp, like formatLocalTime. */
export function formatAge(iso?: string | null, now: number = Date.now()): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, (now - t) / 1000)
  if (s < 90) return 'just now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.round(h / 24)
  if (d < 14) return `${d} d ago`
  return `${Math.round(d / 7)} wk ago`
}
