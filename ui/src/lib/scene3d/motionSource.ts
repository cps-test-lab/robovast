// MotionSource: what a run's *motion* looks like to a viewer, independent of where it comes from.
//
// The Run view's 3D panel consumes this and nothing else, so the same panel replays a finished run
// from a `capture.json`/`capture.bin` pair (see runCapture.ts) and -- later, unchanged -- follows a
// live simulation over whatever transport is chosen. That is the point of the seam: the *data model*
// is a time base plus named tracks, and a file is only one serialization of it.
//
// Five properties make the model live-ready rather than file-shaped. Each is cheap here and expensive
// to retrofit once panels depend on the other behaviour:
//
//  1. `range().complete` is false when the upper bound can still move, and a consumer re-reads the
//     range instead of caching it. A file is complete by construction; a stream never is.
//  2. `tracks()` may grow between calls -- a live source gains a track when a robot spawns or a
//     pedestrian appears. A file's never does, but a consumer that assumed otherwise would have to
//     be rewritten rather than extended.
//  3. Samples are addressed by TIME, never by array index, and `indexAt` is nearest-sample with ties
//     to the earlier one -- never interpolated, because blending two states produces a pose the
//     simulation never had. For a live source "nearest" is simply "latest".
//  4. `subscribe` is the only way data arrival is announced, and the file source fires it once on
//     load. An interface method nothing calls is a guess; one the only shipping implementation uses
//     is a contract.
//  5. `fetch(t0, t1)` asks for a window rather than everything. The file source loads the whole
//     buffer today (a 30 s capture of a mobile manipulator is a few hundred KiB), but tracks are
//     time-contiguous by specification, so windowing is a change of implementation, not of API.
//
// Values are pushed into a MotionSink rather than returned, so a 60 Hz redraw of a few dozen tracks
// allocates nothing. The scene model is the sink: `joint` is its jointMap and `pose` its basePose.

/** Where a source's samples sit in time. `complete` is false while the upper bound can still move. */
export interface MotionRange {
  t0: number
  t1: number
  complete: boolean
}

/** One channel of motion. `joint` carries a scalar in the joint's own unit; `pose` a world-frame pose. */
export interface MotionTrack {
  kind: 'joint' | 'pose'
  /** The name this track drives -- a joint name for `joint`, a body name for `pose`, as the scene
   *  descriptor spells them. Names are the whole addressing scheme: no indices cross the interface. */
  name: string
  /** Physical unit of a `joint` value (`rad` / `m`), when the producer states one. */
  unit?: string
}

/** Provenance a viewer shows, and checks a capture against the scene it is animating. */
export interface MotionMeta {
  producer?: string
  producerVersion?: string
  /** The world this motion was recorded from. Naming it is what makes a mismatch reportable. */
  world?: string
  seed?: number | null
  /** The frame poses are expressed in. `world` matches the scene descriptor's geometry 1:1; anything
   *  else (a `map` frame, say) can be metres away and is indistinguishable from the numbers alone. */
  frame?: string
  /** `sim` = seconds of simulated time from the run's start; `wall` = wall-clock. Declared so a
   *  capture-driven panel and a rosbag-driven one can be told whether they share a clock. */
  timeBase?: string
}

/** Where a source writes the values of one sample. Implemented by the scene model. */
export interface MotionSink {
  joint(name: string, value: number): void
  pose(
    name: string,
    pos: ArrayLike<number>,
    quat: readonly [number, number, number, number],
  ): void
}

export interface MotionSource {
  /** Current extent in time. Re-read it; for a live source the upper bound moves. */
  range(): MotionRange
  /** Tracks known so far. May grow between calls. */
  tracks(): readonly MotionTrack[]
  meta(): MotionMeta
  /** Index of the sample nearest `t` (ties to the earlier), or -1 when nothing is loaded yet. */
  indexAt(t: number): number
  /** Push sample `index` into `sink`. Pose tracks arrive parents-first, as the format requires. */
  apply(index: number, sink: MotionSink): void
  /** Ensure `[t0, t1]` is available; resolves when it is. Fires subscribers if that added data. */
  fetch(t0: number, t1: number): Promise<void>
  /** Called whenever the range, the track set, or the loaded data changed. Returns an unsubscribe. */
  subscribe(listener: () => void): () => void
  /** Release buffers. A source is per run, so switching runs disposes one and opens another. */
  dispose(): void
}
