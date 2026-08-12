/** Index of the last sample with `times[i] <= t` (rightmost), or -1 if `t` precedes them all. */
export declare function lastAtOrBefore(times: readonly number[], t: number): number;
/** Index of the sample nearest `t` in absolute time, or -1 if `times` is empty.
 *
 *  Ties go to the earlier sample. Unlike `lastAtOrBefore` this may return the sample *after* `t`, so
 *  it is the right one for "what was the state at t" where a slightly-later reading is a better answer
 *  than a much-earlier one. */
export declare function nearestIndex(times: readonly number[], t: number): number;
