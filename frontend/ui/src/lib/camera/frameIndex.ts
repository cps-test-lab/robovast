// The frame index: the sim seconds of every frame a topic recorded, sorted ascending. It is what
// turns a clock time into the one frame the route would answer with, so the panel asks for a frame
// once per frame rather than once per clock tick, and knows that a frame is missing without a
// round trip.

/** Pick the frame the route answers with for `t`: the newest at or before it. `null` when `t` is
 *  before the first frame (or the index is empty) -- that is "no frame at this time", not frame 0. */
export function nearestFrameAtOrBefore(times: readonly number[], t: number): number | null {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (times[mid] <= t) lo = mid + 1
    else hi = mid
  }
  return lo === 0 ? null : times[lo - 1]
}

/** Add a frame time a live stream delivered, keeping the index sorted and free of duplicates.
 *  Returns the same array when nothing changed, so a reader can compare by identity. */
export function insertFrameTime(times: readonly number[], t: number): readonly number[] {
  const last = times[times.length - 1]
  if (last !== undefined && t > last) return [...times, t]
  const at = nearestFrameAtOrBefore(times, t)
  if (at === t) return times
  const out = [...times, t]
  out.sort((a, b) => a - b)
  return out
}

/** One `frame-index` answer, parsed. Refuses anything but `{topic, times: [numbers]}`, so a route
 *  that changed shape surfaces as an error rather than as a camera that never finds a frame. */
export function parseFrameIndex(data: unknown): { topic: string; times: number[] } {
  if (
    !data || typeof data !== 'object' ||
    typeof (data as { topic?: unknown }).topic !== 'string' ||
    !Array.isArray((data as { times?: unknown }).times)
  ) {
    throw new Error('frame-index: expected {"topic": <name>, "times": [...]}')
  }
  const { topic, times } = data as { topic: string; times: unknown[] }
  const nums = times.map(Number)
  if (nums.some((n) => !Number.isFinite(n))) {
    throw new Error(`frame-index for ${topic}: every time must be a finite number`)
  }
  return { topic, times: nums.sort((a, b) => a - b) }
}

/** What one `event: frame` of the live stream carries. */
export interface LiveFrame {
  topic: string
  /** Sim seconds of the frame. */
  t: number
  /** The JPEG bytes, as the stream sends them. */
  jpegBase64: string
}

/** One `frame` event's data, parsed. */
export function parseFrameEvent(data: string): LiveFrame {
  const parsed: unknown = JSON.parse(data)
  const p = parsed as { topic?: unknown; t?: unknown; jpeg_base64?: unknown } | null
  if (
    !p || typeof p !== 'object' ||
    typeof p.topic !== 'string' || typeof p.jpeg_base64 !== 'string' ||
    !Number.isFinite(Number(p.t))
  ) {
    throw new Error('live frame: expected {"topic": <name>, "t": <seconds>, "jpeg_base64": <data>}')
  }
  return { topic: p.topic, t: Number(p.t), jpegBase64: p.jpeg_base64 }
}

/** The `src` an `<img>` shows a live frame from. */
export const jpegDataUrl = (jpegBase64: string) => `data:image/jpeg;base64,${jpegBase64}`
