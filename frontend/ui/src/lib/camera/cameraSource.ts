// Where a camera panel gets its picture, and the one thing it needs besides the pixels: where the
// picture sits on the run's timeline.
//
// Three kinds, chosen by the panel's `kind`:
//
//  - `recorded`: a `.webm` a producer registered in the `videos` table (or named by `source.path`).
//    The video file alone cannot place itself: `rosbags_to_webm` re-times every frame onto a constant
//    rate and drops the bag stamps, so the `videos` row is what carries `t_start`, and the panel and
//    the `get_camera_frame` MCP tool read the same row so they cannot disagree about it.
//  - `topic`: the frames of an image topic the run recorded, one request per frame through the
//    frame route -- the frames themselves, at their own times, live or finished. The `.webm` above
//    is the smooth-playback derivative of the same recording.
//  - `render`: a run with no camera topic at all. The screenshot route re-renders the run's
//    recorded state at the clock's moment, from a camera the world defines or a free viewpoint.

import type { DataProvider, PanelSpec } from '@robovast/panel-kit'
import { cameraRoutes, type RenderView, type RunRef } from './frameRoutes'
import { parseFrameIndex } from './frameIndex'

/** A resolved camera: everything the panel needs to put a picture on the clock. The panel branches
 *  on `kind` and never sees a file path. */
export type CameraSource =
  | {
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
  | {
      kind: 'topic'
      topic: string
      run: RunRef
      /** Sim seconds of every frame recorded so far, ascending. Grows while the run is live. */
      times: number[]
    }
  | {
      kind: 'render'
      run: RunRef
      view: RenderView
    }

export type CameraKind = CameraSource['kind']

/** How a `.vast` names a camera panel's picture.
 *
 *     - camera:                                  the run's one registered video
 *     - camera: { source: { topic } }            which video, for a run that recorded several
 *     - camera: { source: { path, t0 } }         a video no producer registered
 *     - camera: { kind: topic, topic: /cam }     the recorded frames of an image topic
 *     - camera: { kind: render, camera: top }    a render of the recorded state
 *     - camera: { kind: render, view: { azimuth: 90, distance: 12 } }
 */
export interface CameraConfig {
  kind?: CameraKind
  /** `kind: topic`: the image topic. Falls back to `source.topic`, so one name serves both. */
  topic?: string
  /** `kind: render`: a camera the world defines. */
  camera?: string
  /** `kind: render`: free-camera settings. */
  view?: Record<string, string | number>
  source?: CameraBinding
}

/** The `recorded` kind's binding. Both forms are optional -- a bare `- camera:` is a complete panel
 *  whenever the run registered exactly one video. */
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

const KINDS: ReadonlySet<string> = new Set<CameraKind>(['recorded', 'topic', 'render'])

/** Which kind a panel config asks for. `recorded` unless it says, so a config written before the
 *  other kinds existed reads the same. */
export function cameraKind(config: CameraConfig): CameraKind {
  const kind = config.kind ?? 'recorded'
  if (!KINDS.has(kind)) {
    throw new Error(
      `camera panel: kind '${String(kind)}' is not one of ${[...KINDS].join(', ')}`,
    )
  }
  return kind
}

/** The topic a `topic` panel reads, or throws: there is no one-topic default the way `recorded` has
 *  the one registered video, because the frame route is asked by name. */
export function cameraTopic(config: CameraConfig): string {
  const topic = config.topic ?? config.source?.topic
  if (!topic) {
    throw new Error(
      "camera panel: kind 'topic' needs a `topic` (the image topic the scenario recorded).",
    )
  }
  return topic
}

export const runRef = (data: DataProvider): RunRef => ({
  campaignId: data.campaignId,
  configName: data.configName,
  runId: data.runId,
})

const num = (v: unknown): number | undefined => {
  const n = Number(v)
  return Number.isFinite(n) ? n : undefined
}

/** Resolve this panel's picture source, or `null` when the run has none.
 *
 *  `null` is a normal answer, not an error: a campaign whose postprocessing has not run yet, or one
 *  whose camera never published, has no `videos` row and no frames. The panel says which, rather
 *  than rendering a black rectangle that looks like a working camera pointed at nothing.
 *
 *  Throws only when the binding itself is unusable -- a `path` with no `t0` cannot be placed on the
 *  clock, and silently pinning it to zero would show the wrong moment with full confidence.
 *
 *  `fetchJson` is how the frame index is read; the default is the browser's `fetch`. */
export async function resolveCameraSource(
  spec: PanelSpec,
  data: DataProvider,
  fetchJson: (url: string) => Promise<unknown> = defaultFetchJson,
): Promise<CameraSource | null> {
  const config = spec.config as CameraConfig
  switch (cameraKind(config)) {
    case 'topic':
      return resolveTopic(config, data, fetchJson)
    case 'render':
      return { kind: 'render', run: runRef(data), view: { camera: config.camera, view: config.view } }
    case 'recorded':
      return resolveRecorded(config.source ?? {}, data)
  }
}

async function defaultFetchJson(url: string): Promise<unknown> {
  const res = await fetch(url)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} reading ${url}`)
  return res.json()
}

async function resolveTopic(
  config: CameraConfig, data: DataProvider, fetchJson: (url: string) => Promise<unknown>,
): Promise<CameraSource | null> {
  const topic = cameraTopic(config)
  const run = runRef(data)
  const body = await fetchJson(cameraRoutes.frameIndex(run, topic))
  if (body === null) return null
  const index = parseFrameIndex(body)
  return { kind: 'topic', topic, run, times: index.times }
}

async function resolveRecorded(
  binding: CameraBinding, data: DataProvider,
): Promise<CameraSource | null> {
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
