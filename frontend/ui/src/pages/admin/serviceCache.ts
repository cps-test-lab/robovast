import type { ServiceCache } from '@/lib/robovastClient'

/** Everything the caches hold now. */
export function heldBytes(report: ServiceCache): number {
  return report.caches.reduce((sum, cache) => sum + cache.size_bytes, 0)
}

/** What a clear would free: what they hold, less what a clear keeps.
 *
 *  The button offers this rather than the total, because a clear keeps what is in use -- and a
 *  button promising 12 GB that frees none of it, because all of it belongs to a running
 *  campaign, is the one failure a report exists to prevent. */
export function clearableBytes(report: ServiceCache): number {
  const kept = report.kept.reduce((sum, entry) => sum + entry.size_bytes, 0)
  return Math.max(0, heldBytes(report) - kept)
}
