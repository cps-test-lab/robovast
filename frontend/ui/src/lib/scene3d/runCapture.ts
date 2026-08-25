// runCapture: the file-backed MotionSource -- reads a run capture (`capture.json` + `capture.bin`).
//
// The format is specified in robovast/docs/run_capture.rst and this is its normative reader. roqsim's
// `roqsim export capture` is the first producer; a second simulator implements the same two files.
//
// Layout, and why it is shaped this way:
//   * `capture.json` indexes tracks by byte offset into `capture.bin`; each track is **sample-major**
//     (all of sample 0's values, then sample 1's...), so a time window is a contiguous byte range.
//     That is what makes windowed reads possible later without touching the format.
//   * The time track is float64 because a float32 second degrades with magnitude -- a run's timestamps
//     are exactly where that matters. Value tracks are float32, which is what the producer sampled.
//   * Offsets are aligned per dtype, because `new Float64Array(buffer, off, n)` *throws* unless `off`
//     is a multiple of 8. A reader that ignored this would work on most captures and fail on some.
//
// This file imports nothing -- not `three`, not `@/…` -- so the scene3d directory stays extractable.

import type {
  MotionMeta,
  MotionRange,
  MotionSink,
  MotionSource,
  MotionTrack,
} from './motionSource'

/** The format string this reader implements. */
export const CAPTURE_FORMAT = 'robovast.run_capture'

/**
 * The highest format version this reader implements. What each version changed:
 *
 *   1  the original.
 *   2  the `overrides` half of world identity addresses components by PATH (`robot.lidar`) rather
 *      than by plugin name. The document's *shape* is unchanged, which is exactly why the version
 *      had to move: a consumer keying a cache on it would otherwise take a stale hit for a document
 *      that now resolves to a different world.
 *
 * v2 therefore changes nothing here -- nothing in this file reads `overrides`. The consumer that
 * does is the service (`scene_cache.world_identity`), which keys geometry on it and so keys the
 * version too. That is recorded rather than left unsaid, because a reader that later starts reading
 * `overrides` has to key the version as well. The format is specified in docs/run_capture.rst.
 */
export const CAPTURE_VERSION = 2

interface RawTrack {
  kind?: unknown
  name?: unknown
  unit?: unknown
  width?: unknown
  samples?: unknown
  dtype?: unknown
  off?: unknown
}

interface RawManifest {
  format?: unknown
  version?: unknown
  complete?: unknown
  frame?: unknown
  producer?: unknown
  producer_version?: unknown
  world?: unknown
  seed?: unknown
  time?: RawTrack & { base?: unknown; t0?: unknown; t1?: unknown }
  tracks?: RawTrack[]
}

export class CaptureFormatError extends Error {}

interface Loaded {
  kind: 'joint' | 'pose'
  name: string
  unit?: string
  width: number
  values: Float32Array // sample-major, length = samples * width
}

function num(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new CaptureFormatError(`${what}: expected a number, got ${JSON.stringify(value)}`)
  }
  return value
}

/** Read a typed view of `track` out of `bin`, refusing anything the buffer cannot actually hold. */
function view(bin: ArrayBuffer, track: RawTrack, what: string): Float64Array | Float32Array {
  const off = num(track.off, `${what}.off`)
  const samples = num(track.samples, `${what}.samples`)
  const width = track.width == null ? 1 : num(track.width, `${what}.width`)
  const dtype = track.dtype == null ? 'f4' : String(track.dtype)
  const count = samples * width
  const size = dtype === 'f8' ? 8 : 4
  // Bounds and alignment are checked here rather than left to the typed-array constructor, whose own
  // message ("start offset of Float64Array should be a multiple of 8") says nothing about which track.
  if (off % size !== 0) {
    throw new CaptureFormatError(
      `${what}: offset ${off} is not ${size}-byte aligned, so it cannot be read as ${dtype}`,
    )
  }
  if (off + count * size > bin.byteLength) {
    throw new CaptureFormatError(
      `${what}: needs ${count * size} bytes at ${off} but capture.bin is ${bin.byteLength} bytes ` +
        `— the manifest and the binary disagree`,
    )
  }
  return dtype === 'f8'
    ? new Float64Array(bin, off, count)
    : new Float32Array(bin, off, count)
}

/**
 * Open a run capture. `url` addresses `capture.json`; `capture.bin` is fetched as its **sibling**,
 * the same relative-path convention the scene descriptor uses for `scene.bin`.
 */
export async function openRunCapture(url: string): Promise<MotionSource> {
  const manifestRes = await fetch(url)
  if (!manifestRes.ok) {
    throw new CaptureFormatError(`${url}: HTTP ${manifestRes.status} ${manifestRes.statusText}`)
  }
  const manifest = (await manifestRes.json()) as RawManifest

  if (manifest.format !== CAPTURE_FORMAT) {
    throw new CaptureFormatError(
      `${url}: not a run capture (format is ${JSON.stringify(manifest.format)}, ` +
        `expected ${CAPTURE_FORMAT})`,
    )
  }
  // Refuse a newer version by name rather than misreading it: a viewer that guessed would render
  // something plausible and wrong, which is the failure this whole design is trying to avoid. The
  // message says what to DO, because the fix is always the same one and never the reader's: teach
  // this file the new version. "Re-export with an older producer" was advice nobody could take.
  // An absent `version` reads as 1 -- the spec requires the field, and the oldest format is the only
  // thing a reader can assume about one that lacks it without guessing forward.
  const version = manifest.version == null ? 1 : num(manifest.version, `${url}: version`)
  if (version > CAPTURE_VERSION) {
    throw new CaptureFormatError(
      `${url}: capture format version ${version} is newer than this viewer supports ` +
        `(${CAPTURE_VERSION}). This reader has to learn v${version} — see the format versions ` +
        `table in RoboVAST's docs/run_capture.rst.`,
    )
  }

  const binUrl = url.replace(/[^/]*$/, 'capture.bin')
  const binRes = await fetch(binUrl)
  if (!binRes.ok) {
    throw new CaptureFormatError(
      `${binUrl}: HTTP ${binRes.status} ${binRes.statusText} — capture.json is present but its ` +
        `binary sibling is not`,
    )
  }
  const bin = await binRes.arrayBuffer()

  const time = manifest.time
  if (!time) throw new CaptureFormatError(`${url}: no "time" block`)
  const times = view(bin, time, `${url}: time`) as Float64Array
  if (!times.length) throw new CaptureFormatError(`${url}: the time track is empty`)

  const loaded: Loaded[] = []
  for (const [i, raw] of (manifest.tracks ?? []).entries()) {
    const what = `${url}: tracks[${i}]`
    const kind = String(raw.kind)
    if (kind !== 'joint' && kind !== 'pose') {
      // Reserved kinds (array tracks, for a lidar scan) are additive by design, so an unknown one is
      // skipped rather than fatal -- an older viewer keeps replaying the motion it does understand.
      continue
    }
    const name = String(raw.name ?? '')
    if (!name) throw new CaptureFormatError(`${what}: a track must be named`)
    const width = raw.width == null ? 1 : num(raw.width, `${what}.width`)
    const expected = kind === 'pose' ? 7 : 1
    if (width !== expected) {
      throw new CaptureFormatError(
        `${what}: a ${kind} track has width ${expected}, not ${width}`,
      )
    }
    if (num(raw.samples, `${what}.samples`) !== times.length) {
      throw new CaptureFormatError(
        `${what}: ${raw.samples} samples but the time track has ${times.length}. A track sharing the ` +
          `manifest's time base must have one value per time sample.`,
      )
    }
    loaded.push({
      kind,
      name,
      unit: raw.unit == null ? undefined : String(raw.unit),
      width,
      values: view(bin, raw, what) as Float32Array,
    })
  }

  const meta: MotionMeta = {
    producer: manifest.producer == null ? undefined : String(manifest.producer),
    producerVersion:
      manifest.producer_version == null ? undefined : String(manifest.producer_version),
    world: manifest.world == null ? undefined : String(manifest.world),
    seed: typeof manifest.seed === 'number' ? manifest.seed : null,
    frame: manifest.frame == null ? undefined : String(manifest.frame),
    timeBase: time.base == null ? undefined : String(time.base),
  }
  const range: MotionRange = {
    t0: times[0],
    t1: times[times.length - 1],
    // A file is finished by construction; the flag exists so the same shape can describe a stream.
    complete: manifest.complete !== false,
  }
  const trackList: MotionTrack[] = loaded.map((t) => ({ kind: t.kind, name: t.name, unit: t.unit }))
  const listeners = new Set<() => void>()
  let disposed = false

  const source: MotionSource = {
    range: () => range,
    tracks: () => trackList,
    meta: () => meta,

    indexAt(t: number): number {
      if (disposed || !times.length) return -1
      // Nearest sample, ties to the earlier one, never interpolated. Binary search: a capture at
      // 25 Hz over a long run is tens of thousands of samples and this runs every animation frame.
      let lo = 0
      let hi = times.length - 1
      if (t <= times[lo]) return lo
      if (t >= times[hi]) return hi
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1
        if (times[mid] <= t) lo = mid
        else hi = mid
      }
      // `<=` keeps the earlier sample when `t` is exactly between two.
      return t - times[lo] <= times[hi] - t ? lo : hi
    },

    apply(index: number, sink: MotionSink): void {
      if (disposed || index < 0 || index >= times.length) return
      for (const track of loaded) {
        if (track.kind === 'joint') {
          sink.joint(track.name, track.values[index])
        } else {
          const at = index * 7
          // Reusing one scratch pair would be faster still, but a sink may retain what it is given;
          // subarray views are cheap and cannot alias across tracks.
          sink.pose(track.name, track.values.subarray(at, at + 3), [
            track.values[at + 3],
            track.values[at + 4],
            track.values[at + 5],
            track.values[at + 6],
          ])
        }
      }
    },

    async fetch(): Promise<void> {
      // The whole buffer arrived with the manifest, so any window is already satisfied. The signature
      // is the windowed one because tracks are time-contiguous by specification: serving a window
      // from range requests is a change here and nowhere else.
    },

    subscribe(listener: () => void): () => void {
      listeners.add(listener)
      // Fire immediately for data that is already here. Without this the file source's one and only
      // notification would be lost: `openRunCapture` resolves after the buffer is parsed, so every
      // subscriber is by definition late. The same rule is what a live source owes a viewer that
      // joins mid-stream -- tell it the current state now rather than at the next batch -- so the
      // consumer needs no special case for "already loaded".
      if (!disposed) queueMicrotask(listener)
      return () => listeners.delete(listener)
    },

    dispose(): void {
      disposed = true
      listeners.clear()
    },
  }

  return source
}
