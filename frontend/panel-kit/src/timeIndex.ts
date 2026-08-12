// Nearest-sample lookup over a time-sorted array, shared by every panel that indexes a recording by
// the clock. Extracted because each caller had grown its own: `timeSeriesFromRows` had the binary
// search, while the costmap panel's pose lookup was a full linear scan re-run per layer per animation
// frame.
//
// Both functions require `times` to be ascending. Callers sort once at load; re-checking per lookup
// would cost more than the search.

/** Index of the last sample with `times[i] <= t` (rightmost), or -1 if `t` precedes them all. */
export function lastAtOrBefore(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length - 1
  let ans = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) {
      ans = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return ans
}

/** Index of the sample nearest `t` in absolute time, or -1 if `times` is empty.
 *
 *  Ties go to the earlier sample. Unlike `lastAtOrBefore` this may return the sample *after* `t`, so
 *  it is the right one for "what was the state at t" where a slightly-later reading is a better answer
 *  than a much-earlier one. */
export function nearestIndex(times: readonly number[], t: number): number {
  if (!times.length) return -1
  const i = lastAtOrBefore(times, t)
  if (i < 0) return 0
  if (i === times.length - 1) return i
  return t - times[i] <= times[i + 1] - t ? i : i + 1
}
