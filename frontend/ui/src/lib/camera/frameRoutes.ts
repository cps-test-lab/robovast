// The URLs a camera panel fetches pictures from, built here rather than in the typed client because
// none of them returns JSON: a frame route answers with image bytes and a live stream with SSE
// frames, and the panel hands the URL to `fetch` or `EventSource` itself.
//
// Every builder takes the service base explicitly so it is a pure function of its inputs; the
// `cameraRoutes` object binds them to the base the rest of the UI uses.

/** Where a frame route addresses one run. */
export interface RunRef {
  campaignId: string
  configName: string
  runId: string | number
}

/** The viewpoint a rendered frame is taken from -- the screenshot route's own parameters. */
export interface RenderView {
  /** A camera the world defines, rendered from its pose. */
  camera?: string
  /** Free-camera settings (`azimuth`, `distance`, ...), each sent as one repeated `view=k=v`. */
  view?: Record<string, string | number>
}

const run = (r: RunRef) => `${r.configName}/${r.runId}`

const campaignData = (base: string, campaignId: string) =>
  `${base}/data/campaigns/${encodeURIComponent(campaignId)}`

/** A recorded camera frame at or before `t` (sim seconds), the newest without `t`. `image/jpeg`
 *  with the frame's own time in `X-Frame-Time`; 404 with a sentence when there is none. */
export function frameUrl(base: string, r: RunRef, topic: string, t?: number): string {
  const qs = new URLSearchParams({ run: run(r), topic })
  if (t !== undefined) qs.set('t', String(t))
  return `${campaignData(base, r.campaignId)}/frame?${qs.toString()}`
}

/** The sim seconds of every frame of `topic` this run recorded: `{"topic", "times": [...]}`. */
export function frameIndexUrl(base: string, r: RunRef, topic: string): string {
  const qs = new URLSearchParams({ run: run(r), topic })
  return `${campaignData(base, r.campaignId)}/frame-index?${qs.toString()}`
}

/** The run's live stream carrying `event: frame` for `topics` and no table rows: the camera panel
 *  opens a socket of its own rather than widening the tables feed, since frames are not rows. */
export function liveFramesUrl(base: string, r: RunRef, topics: string[]): string {
  const qs = new URLSearchParams({ run: run(r), tables: '', frames: topics.join(',') })
  return `${campaignData(base, r.campaignId)}/live?${qs.toString()}`
}

/** A render of the run's recorded state at `t` (sim seconds), or its newest state without `t`.
 *  `at` is the route's name for the moment. */
export function screenshotUrl(base: string, r: RunRef, opts: RenderView & { t?: number } = {}): string {
  const qs = new URLSearchParams({ config_name: r.configName, run_id: String(r.runId) })
  if (opts.t !== undefined) qs.set('at', String(opts.t))
  if (opts.camera) qs.set('camera', opts.camera)
  for (const [k, v] of Object.entries(opts.view ?? {})) qs.append('view', `${k}=${v}`)
  return `${base}/campaigns/${encodeURIComponent(r.campaignId)}/screenshot?${qs.toString()}`
}

/** "" by default: the service serves this SPA same-origin (see lib/robovastClient.ts). */
const BASE = (import.meta.env.VITE_ROBOVAST_URL ?? '').replace(/\/$/, '')

export const cameraRoutes = {
  frame: (r: RunRef, topic: string, t?: number) => frameUrl(BASE, r, topic, t),
  frameIndex: (r: RunRef, topic: string) => frameIndexUrl(BASE, r, topic),
  liveFrames: (r: RunRef, topics: string[]) => liveFramesUrl(BASE, r, topics),
  screenshot: (r: RunRef, opts?: RenderView & { t?: number }) => screenshotUrl(BASE, r, opts),
}
