// MotionSource: what a run's *motion* looks like to a viewer, independent of where it comes from.
//
// The Run view's 3D panel consumes this and nothing else, so the same panel replays a finished run
// and follows one still recording from the same source (rowMotion.ts, over the run's pose and
// joint tables). That is the point of the seam: the *data model* is a time base plus named tracks,
// and where the samples come from is the source's business.
//
// Five properties make the model live-ready rather than file-shaped:
//
//  1. `range().complete` is false when the upper bound can still move, and a consumer re-reads the
//     range instead of caching it. A finished run is complete; one still recording is not.
//  2. `tracks()` may grow between calls -- a live source gains a track when a robot spawns or a
//     pedestrian appears.
//  3. Samples are addressed by TIME, never by array index, and `indexAt` is nearest-sample with ties
//     to the earlier one -- never interpolated, because blending two states produces a pose the
//     simulation never had. For a live source at its edge "nearest" is simply "latest".
//  4. `subscribe` is the only way data arrival is announced: once per loaded window, and per batch
//     of a run still recording.
//  5. `fetch(t0, t1)` asks for a window rather than everything: a run's poses are every body at
//     every tick, far past what one query returns, so the viewer asks around where it is looking.
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

/** Provenance a viewer shows, and checks a recording against the scene it is animating. */
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
   *  simulator-driven panel and a rosbag-driven one can be told whether they share a clock. */
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
