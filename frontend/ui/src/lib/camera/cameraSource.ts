// Where a camera panel gets its picture, and the one thing it needs besides the pixels: where the
// recording sits on the run's timeline.
//
// The video file alone cannot answer that. `rosbags_to_webm` re-times every frame onto a constant
// rate and drops the bag stamps, so a `.webm` in a run directory says nothing about *when* its first
// frame was -- and a camera that came up ten seconds into the trial would otherwise replay as though
// it had been running from the start. The `videos` table is what carries `t_start`, and the panel and
// the `get_camera_frame` MCP tool read the same row so they cannot disagree about it.

import type { DataProvider, PanelSpec } from '@robovast/panel-kit'

/** A resolved camera: everything the panel needs to put a picture on the clock.
 *
 *  One variant today. A **live** view widens this union -- WebRTC, negotiated through the service,
 *  is the intended transport, since a recording is what makes HTTP the right answer here and a live
 *  feed has no recording. It is deliberately not sketched: a peer carries a signaling endpoint and a
 *  session, not a URL, so a `{ url }` variant would be the wrong shape rather than a prepared one.
 *  What the seam guarantees is that the panel branches on `kind` and never sees a file path. */
export type CameraSource = {
  kind: 'recorded'
  /** Fetched by the browser itself, as a `<video src>`; ranged requests come for free. */
  url: string
  /** Run-timeline seconds of the first frame -- the offset between clock time and video time. */
  t0: number
  /** Run-timeline seconds of the last frame, when the producer recorded it. */
  t1?: number
  /** Frames per second, used for the seek tolerance. Absent for a producer that omitted it. */
  fps?: number
  /** For the empty state: which camera this is. */
  topic?: string
}

/** How a `.vast` names a camera panel's video. Both forms are optional -- a bare `- camera:` is a
 *  complete panel whenever the run registered exactly one video. */
export interface CameraBinding {
  /** Which camera, for a run that recorded several. */
  topic?: string
  /** Escape hatch: a video no producer registered in `videos`. Needs `t0` to be placeable. */
  path?: string
  /** Run-timeline seconds of `path`'s first frame. */
  t0?: number
  t1?: number
  fps?: number
}

/** The manifest table every video producer writes a row to. Not owned by any one of them:
 *  `rosbags_to_webm` is the first, a simulator that renders its own video may be the next. */
export const VIDEOS_TABLE = 'videos'

const num = (v: unknown): number | undefined => {
  const n = Number(v)
  return Number.isFinite(n) ? n : undefined
}

/** Resolve this panel's video, or `null` when the run has none.
 *
 *  `null` is a normal answer, not an error: a campaign whose postprocessing has not run yet, or one
 *  whose camera never published, has no row here. The panel says which, rather than rendering a
 *  black rectangle that looks like a working camera pointed at nothing.
 *
 *  Throws only when the binding itself is unusable -- a `path` with no `t0` cannot be placed on the
 *  clock, and silently pinning it to zero would show the wrong moment with full confidence. */
export async function resolveCameraSource(
  spec: PanelSpec,
  data: DataProvider,
): Promise<CameraSource | null> {
  const binding = (spec.config.source ?? {}) as CameraBinding

  if (binding.path) {
    if (typeof binding.t0 !== 'number')
      throw new Error(
        `camera panel: source.path '${binding.path}' needs a source.t0 (run-timeline seconds of ` +
          'its first frame). Without one the video cannot be placed on the playback clock. ' +
          'A video registered in the `videos` table carries this already.',
      )
    return {
      kind: 'recorded',
      url: data.runFileUrl(binding.path),
      t0: binding.t0,
      t1: binding.t1,
      fps: binding.fps,
      topic: binding.topic,
    }
  }

  if (!(await data.has(VIDEOS_TABLE, ['file', 't_start']))) return null

  const rows = await data.series(VIDEOS_TABLE, {
    timeCol: 't_start',
    ...(binding.topic ? { match: { topic: binding.topic } } : {}),
  })
  const row = rows[0]
  if (!row?.file) return null

  const t0 = num(row.t_start)
  if (t0 === undefined) return null

  return {
    kind: 'recorded',
    url: data.runFileUrl(String(row.file)),
    t0,
    t1: num(row.t_end),
    fps: num(row.fps),
    topic: row.topic == null ? undefined : String(row.topic),
  }
}
