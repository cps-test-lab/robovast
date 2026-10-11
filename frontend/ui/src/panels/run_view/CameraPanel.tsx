// CameraPanel (type `camera`): a picture of the run, played on the run's clock.
//
// This is what a backend with no 3D scene has instead of one. Gazebo writes no run capture and has no
// scene exporter, so a `scene3d` panel has nothing to replay there; a monitor camera spawned into the
// world gives that run view a picture of the trial. Placed like any other panel rather than as the
// `fill` base layer -- the other panels float over `fill`, and a camera image covered by them is
// worse than a smaller one that is whole.
//
// Three kinds of picture (lib/camera/cameraSource.ts), one component each below:
//   recorded  a `.webm` from the `videos` table, in a `<video>` that follows the clock
//   topic     the recorded frames of an image topic, one request per frame; live frames from the
//             run's stream while the clock follows a live run
//   render    a re-render of the recorded state at the clock's moment, for a run with no camera
//
// THE PANEL IS A READER OF THE CLOCK, NEVER A WRITER. The playback bar owns time (see
// panel-kit/src/clock.ts: one writer, every other panel a subscriber), so the `<video>` carries no
// native controls and never calls seek() -- it follows `t` and nothing else. Giving it controls would
// put two things in charge of when "now" is, and the run view would disagree with itself.
//
// Bindings (vast visualization.panels):
//   source: { topic }                 which video, for a run that recorded several
//   source: { path, t0, t1?, fps? }   escape hatch for a video no producer registered
//   kind: topic, topic: <image topic>
//   kind: render, camera?: <world camera>, view?: { azimuth, distance, ... }
//
// A bare `- camera:` is a complete panel when the run registered exactly one video -- the same
// promise `scene3d` makes.

import {
  useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode,
} from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { registerPanel } from '@/lib/panels/registry'
import { isLiveProvider } from '@/lib/panels/dataProvider'
import { useLiveStream } from '@/lib/liveStream'
import {
  cameraKind,
  cameraTopic,
  resolveCameraSource,
  VIDEOS_TABLE,
  type CameraConfig,
  type CameraSource,
} from '@/lib/camera/cameraSource'
import { cameraRoutes } from '@/lib/camera/frameRoutes'
import {
  insertFrameTime,
  jpegDataUrl,
  nearestFrameAtOrBefore,
  parseFrameEvent,
  parseFrameIndex,
} from '@/lib/camera/frameIndex'
import { LatestRequest } from '@/lib/camera/latestRequest'
import { FrameCache } from '@/lib/camera/frameCache'
import { fetchImage, NoImageError, type FetchedImage } from '@/lib/camera/fetchImage'
import type { PanelProps, PlaybackClock } from '@robovast/panel-kit'

/** Frame rate assumed when the producer recorded none — only ever used for the seek tolerance. */
const FALLBACK_FPS = 10

/** How far the element may drift from the clock before it is seeked, in frame periods.
 *
 *  A formula rather than a constant, because this panel has to hold at both ends of the range it
 *  serves: 60 ms at 25 fps, 1.5 s at a monitor camera's 1 Hz. In both cases that is "within one
 *  frame", which is the only precision a video actually has — a tighter bound would seek constantly
 *  to land on the frame it was already showing, and each seek re-buffers. */
const TOLERANCE_FRAMES = 1.5

/** Quiet time after a scrub before a frame is asked for: long enough to coalesce a drag, short
 *  enough that a scrub still feels like it moves the picture. */
const FRAME_SCRUB_DEBOUNCE_MS = 120
/** The most frames a playing `topic` panel asks for per second. The webm is the smooth derivative;
 *  this is the frames themselves, at a rate the route sustains. */
const FRAME_MAX_PER_SECOND = 10
/** Frames kept in the panel: scrubbing back over a moment shows what it showed before. */
const FRAME_CACHE = 64

/** A render runs the simulator and takes seconds, so a scrub waits longer before it asks. */
const RENDER_SCRUB_DEBOUNCE_MS = 300
/** The least time between two renders while playing: one in flight at a time bounds it too, this
 *  keeps a fast render from being asked for every tick. */
const RENDER_MIN_INTERVAL_MS = 500
/** How often the newest state is re-rendered while the clock follows a live run. */
const RENDER_LIVE_INTERVAL_MS = 3000
/** Renders are keyed on a rounded moment, so a scrub that settles a few milliseconds off a cached
 *  moment shows it rather than rendering a picture nobody could tell apart. */
const RENDER_QUANTUM_S = 0.1
const RENDER_CACHE = 32

/** How often a live run with no picture of this kind yet is asked again. */
const LIVE_SOURCE_RETRY_MS = 5000

/** The screenshot route runs the simulator, so the service declares it as a POST. */
const SCREENSHOT_METHOD = 'POST'

/** Object URLs are freed on eviction; a live frame's data URL owns nothing to free. */
const revokeIfBlob = (url: string) => {
  if (url.startsWith('blob:')) URL.revokeObjectURL(url)
}

const fmtT = (t: number) => `${t.toFixed(2)} s`

function CameraPanel({ spec, clock, data }: PanelProps) {
  const config = spec.config as CameraConfig
  const live = isLiveProvider(data) && data.live
  let kind: ReturnType<typeof cameraKind> | null = null
  let configError: Error | null = null
  try {
    kind = cameraKind(config)
    if (kind === 'topic') cameraTopic(config)
  } catch (e) {
    configError = e as Error
  }

  const query = useQuery({
    // The run scope leads, as everywhere: table names repeat across campaigns, and a key without it
    // serves the previous campaign's row after a switch.
    queryKey: [
      'camera-source', data.scope, kind, config.topic ?? null, config.camera ?? null,
      JSON.stringify(config.view ?? null), config.source?.topic ?? null, config.source?.path ?? null,
    ],
    queryFn: () => resolveCameraSource(spec, data),
    enabled: configError === null,
    retry: false,
    // A live run's camera may not have published yet: its index is asked for again until it has.
    refetchInterval: (q) => (live && q.state.data === null ? LIVE_SOURCE_RETRY_MS : false),
  })
  const source = query.data ?? null

  if (configError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {configError.message}
      </Alert>
    )
  if (query.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (query.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(query.error as Error).message}
      </Alert>
    )

  if (!source) return <NoSource kind={kind!} config={config} />

  switch (source.kind) {
    case 'recorded':
      return <RecordedCamera source={source} clock={clock} />
    case 'topic':
      return <TopicCamera source={source} clock={clock} data={data} />
    case 'render':
      return <RenderCamera source={source} clock={clock} data={data} />
  }
}

/** The run has nothing of this kind -- and which kind, since the fix differs. */
function NoSource({ kind, config }: { kind: CameraSource['kind']; config: CameraConfig }) {
  if (kind === 'topic')
    return (
      <Alert severity="info" sx={{ m: 1 }}>
        This run recorded no frames for <code>{config.topic ?? config.source?.topic}</code>. A{' '}
        <code>kind: topic</code> camera panel shows the frames of an image topic the scenario
        recorded — check the topic name against the run's bag.
      </Alert>
    )
  return (
    <Alert severity="info" sx={{ m: 1 }}>
      This run registered no video
      {config.source?.topic ? (
        <>
          {' '}
          for <code>{config.source.topic}</code>
        </>
      ) : null}
      . A camera panel plays a recording listed in the <code>{VIDEOS_TABLE}</code> table — add{' '}
      <code>rosbags_to_webm</code> to <code>results_processing.postprocessing</code> (naming the
      image topic the scenario recorded), then re-run postprocessing. A run with no camera at all
      can use <code>kind: render</code> instead.
    </Alert>
  )
}

/** The picture area shared by every kind: the image, a caption naming the kind and the moment
 *  shown, and the words for a missing frame or a failed request over it. */
function Picture({
  src, caption, missing, error, children,
}: {
  src: string | null
  caption: string
  missing: string | null
  error: string | null
  children?: ReactNode
}) {
  return (
    <Box sx={{ position: 'relative', height: '100%', bgcolor: 'common.black' }}>
      {children ?? (src ? (
        <Box
          component="img"
          src={src}
          alt=""
          sx={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
        />
      ) : null)}
      {missing || error ? (
        <Box
          sx={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            bgcolor: 'rgba(0,0,0,0.65)',
            color: error ? 'error.light' : 'common.white',
            fontSize: 13,
            textAlign: 'center',
            px: 2,
          }}
        >
          {error ?? missing}
        </Box>
      ) : null}
      <Box
        sx={{
          position: 'absolute',
          left: 0,
          right: 0,
          bottom: 0,
          px: 1,
          py: 0.25,
          bgcolor: 'rgba(0,0,0,0.55)',
          color: 'common.white',
          fontSize: 11,
          fontFamily: 'monospace',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {caption}
      </Box>
    </Box>
  )
}

/** Whether the clock is tracking the edge of a live run -- the one clock field these components
 *  need as React state, since it decides whether a stream is open. */
const useFollowing = (clock: PlaybackClock) =>
  useSyncExternalStore(clock.subscribe, () => clock.getSnapshot().following)

// -- recorded ---------------------------------------------------------------------------------------

function RecordedCamera({
  source, clock,
}: {
  source: Extract<CameraSource, { kind: 'recorded' }>
  clock: PlaybackClock
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [outOfRange, setOutOfRange] = useState(false)
  const [t, setT] = useState(clock.t)

  /** Put the element where the clock says, doing as little as possible. */
  const sync = useCallback(() => {
    const el = videoRef.current
    if (!el) return
    const { t, playing, speed } = clock.getSnapshot()
    const target = t - source.t0
    // `duration` is NaN until metadata arrives; treat that as "no upper bound known yet" rather
    // than as out of range, or the first paint is an error message that fixes itself.
    const end = Number.isFinite(el.duration) ? el.duration : Infinity

    if (target < 0 || target > end) {
      // A run whose camera came up late, or a trial that ran past the last frame. Showing frame 0
      // here would be a picture of a different moment presented as this one.
      setOutOfRange(true)
      if (!el.paused) el.pause()
      return
    }
    setOutOfRange(false)
    // The caption at a rate a reader can follow, not at every tick.
    setT(Math.round(t * 10) / 10)

    const tolerance = TOLERANCE_FRAMES / (source.fps || FALLBACK_FPS)
    if (Math.abs(el.currentTime - target) > tolerance) el.currentTime = target

    if (playing) {
      if (el.playbackRate !== speed) el.playbackRate = speed
      // Rejected when the browser declines to play (no gesture yet); the element is muted so this
      // is rare, and a rejection means the frame stays where the seek above put it.
      if (el.paused) void el.play().catch(() => undefined)
    } else if (!el.paused) {
      el.pause()
    }
  }, [clock, source])

  // Imperative, like Scene3DPanel: the clock ticks at display rate while playing, and re-rendering
  // this component for each tick would buy nothing a direct property write does not.
  useEffect(() => {
    sync()
    return clock.subscribe(sync)
  }, [clock, sync])

  return (
    <Picture
      src={null}
      caption={`video${source.topic ? ` ${source.topic}` : ''} · ${fmtT(t)}`}
      missing={outOfRange ? 'No frames at this time' : null}
      error={null}
    >
      <Box
        component="video"
        ref={videoRef}
        src={source.url}
        muted
        playsInline
        preload="auto"
        // No controls of its own, and no browser affordances that would act as some.
        // Chrome floats a Picture-in-Picture button over a video this size even with
        // `controls` unset, and popping the panel out into a always-on-top window
        // detaches it from the clock driving it -- the frame would keep changing with
        // nothing on screen explaining what time it is showing.
        disablePictureInPicture
        controlsList="nodownload noplaybackrate noremoteplayback"
        onLoadedMetadata={sync}
        sx={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
      />
    </Picture>
  )
}

// -- topic ------------------------------------------------------------------------------------------

/** What the panel shows: the frame's own time and its picture. */
interface Shown {
  t: number
  src: string
}

function TopicCamera({
  source, clock, data,
}: {
  source: Extract<CameraSource, { kind: 'topic' }>
  clock: PlaybackClock
  data: PanelProps['data']
}) {
  const live = isLiveProvider(data) && data.live
  const following = useFollowing(clock)
  const streaming = live && following

  const [shown, setShown] = useState<Shown | null>(null)
  const [missing, setMissing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // The index grows as live frames arrive, and is re-read when a reader leaves the edge of a live
  // run (below); a ref, because every clock tick reads it and none should re-render for it.
  const times = useRef<readonly number[]>(source.times)
  useEffect(() => {
    times.current = source.times
  }, [source])

  const cache = useMemo(() => new FrameCache(FRAME_CACHE, revokeIfBlob), [])
  useEffect(() => () => cache.clear(), [cache])

  const show = useCallback((t: number, src: string) => {
    setShown({ t, src })
    setMissing(null)
    setError(null)
  }, [])

  const syncRef = useRef<() => void>(() => undefined)
  const requests = useMemo(
    () =>
      new LatestRequest<number, FetchedImage>({
        run: (t, signal) => fetchImage(cameraRoutes.frame(source.run, source.topic, t), signal),
        minIntervalMs: 1000 / FRAME_MAX_PER_SECOND,
        onResult: (t, result) => {
          if (result instanceof NoImageError) {
            setMissing(result.message)
          } else if (result instanceof Error) {
            setError(result.message)
          } else {
            cache.set(result.frameTime ?? t, result.url)
            // Through `sync` rather than shown directly: the clock may have moved on while the
            // frame was in flight, and sync knows which frame is wanted now.
            syncRef.current()
          }
        },
      }),
    [source, cache],
  )
  useEffect(() => () => requests.dispose(), [requests])

  /** Put the picture where the clock says: the newest frame at or before `t`, from the cache when
   *  it is there and through one debounced, rate-bound request when not. */
  const sync = useCallback(() => {
    if (live && clock.getSnapshot().following) return // the stream is showing the newest frame
    const { t, playing } = clock.getSnapshot()
    const at = nearestFrameAtOrBefore(times.current, t)
    if (at === null) {
      setMissing(`No frame at or before ${fmtT(t)}`)
      return
    }
    const cached = cache.get(at)
    if (cached !== undefined) {
      setShown((prev) => (prev?.t === at && prev.src === cached ? prev : { t: at, src: cached }))
      setMissing(null)
      setError(null)
      return
    }
    requests.request(at, { debounceMs: playing ? 0 : FRAME_SCRUB_DEBOUNCE_MS })
  }, [clock, live, cache, requests])
  syncRef.current = sync

  useEffect(() => {
    sync()
    return clock.subscribe(sync)
  }, [clock, sync])

  // While the clock follows a live run, the newest frame comes down the run's stream rather than
  // through one request per frame. Each one extends the index too, so scrubbing back over the live
  // segment finds the frames that were shown.
  const streamUrl = streaming ? cameraRoutes.liveFrames(source.run, [source.topic]) : null
  useLiveStream(streamUrl, {
    events: {
      frame: (e) => {
        let frame
        try {
          frame = parseFrameEvent(e.data as string)
        } catch (err) {
          setError((err as Error).message)
          return
        }
        if (frame.topic !== source.topic) return
        times.current = insertFrameTime(times.current, frame.t)
        const src = jpegDataUrl(frame.jpegBase64)
        cache.set(frame.t, src)
        show(frame.t, src)
      },
    },
  })

  // Leaving the edge of a live run: the open segment has grown since the index was read, and the
  // stream only ever carried the frames this panel saw. Re-read the index so the scrub covers what
  // landed meanwhile; the stream's own times stay in it. Only on the way out of following -- the
  // index a panel mounts with is fresh.
  const wasStreaming = useRef(false)
  useEffect(() => {
    const leaving = wasStreaming.current && !streaming
    wasStreaming.current = streaming
    if (!leaving) return
    const controller = new AbortController()
    void (async () => {
      try {
        const res = await fetch(cameraRoutes.frameIndex(source.run, source.topic), {
          signal: controller.signal,
        })
        if (!res.ok) return
        const index = parseFrameIndex(await res.json())
        let merged = index.times as readonly number[]
        for (const t of times.current) merged = insertFrameTime(merged, t)
        times.current = merged
        sync()
      } catch {
        // An aborted or failed re-read leaves the index as it was: the frame route still answers
        // "at or before" for any `t`, so a stale index costs a coarser scrub, not a wrong frame.
      }
    })()
    return () => controller.abort()
  }, [streaming, source, sync])

  const caption = streaming
    ? `topic ${source.topic} · live${shown ? ` · ${fmtT(shown.t)}` : ''}`
    : `topic ${source.topic} · frame ${shown ? fmtT(shown.t) : '—'}`

  return <Picture src={shown?.src ?? null} caption={caption} missing={missing} error={error} />
}

// -- render -----------------------------------------------------------------------------------------

/** `null` asks for the newest state; a number is a moment, rounded to the quantum. */
type RenderKey = number | null

function RenderCamera({
  source, clock, data,
}: {
  source: Extract<CameraSource, { kind: 'render' }>
  clock: PlaybackClock
  data: PanelProps['data']
}) {
  const live = isLiveProvider(data) && data.live
  const following = useFollowing(clock)
  const newest = live && following

  const [shown, setShown] = useState<Shown | null>(null)
  const [missing, setMissing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const cache = useMemo(() => new FrameCache(RENDER_CACHE, revokeIfBlob), [])
  useEffect(() => () => cache.clear(), [cache])
  const newestUrl = useRef<string | null>(null)
  useEffect(
    () => () => {
      if (newestUrl.current) revokeIfBlob(newestUrl.current)
    },
    [],
  )

  const syncRef = useRef<() => void>(() => undefined)
  const requests = useMemo(
    () =>
      new LatestRequest<RenderKey, FetchedImage>({
        run: (key, signal) => {
          setBusy(true)
          return fetchImage(
            cameraRoutes.screenshot(source.run, { ...source.view, t: key ?? undefined }),
            signal,
            { method: SCREENSHOT_METHOD },
          )
        },
        minIntervalMs: RENDER_MIN_INTERVAL_MS,
        onResult: (key, result) => {
          setBusy(false)
          if (result instanceof NoImageError) {
            setMissing(result.message)
          } else if (result instanceof Error) {
            setError(result.message)
          } else if (key === null) {
            // The newest state is never cached: the next one is newer, and the one before it is
            // freed here -- a cached moment's URL is the cache's to free, never this one's.
            if (newestUrl.current) revokeIfBlob(newestUrl.current)
            newestUrl.current = result.url
            setShown({ t: result.frameTime ?? clock.t, src: result.url })
            setMissing(null)
            setError(null)
          } else {
            cache.set(key, result.url)
            syncRef.current()
          }
        },
      }),
    [source, cache, clock],
  )
  useEffect(() => () => requests.dispose(), [requests])

  const sync = useCallback(() => {
    if (live && clock.getSnapshot().following) return // the interval below asks for the newest
    const { t, playing } = clock.getSnapshot()
    const key = Math.round(t / RENDER_QUANTUM_S) * RENDER_QUANTUM_S
    const cached = cache.get(key)
    if (cached !== undefined) {
      setShown((prev) => (prev?.t === key && prev.src === cached ? prev : { t: key, src: cached }))
      setMissing(null)
      setError(null)
      return
    }
    requests.request(key, { debounceMs: playing ? 0 : RENDER_SCRUB_DEBOUNCE_MS })
  }, [clock, live, cache, requests])
  syncRef.current = sync

  useEffect(() => {
    sync()
    return clock.subscribe(sync)
  }, [clock, sync])

  // Following a live run: the newest state, re-rendered at a modest interval -- each render runs
  // the simulator, and one in flight at a time is the other bound.
  useEffect(() => {
    if (!newest) return
    const ask = () => requests.request(null, { force: true, debounceMs: 0 })
    ask()
    const timer = window.setInterval(ask, RENDER_LIVE_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [newest, requests])

  const viewpoint = source.view.camera
    ? `camera ${source.view.camera}`
    : Object.keys(source.view.view ?? {}).length
      ? 'free view'
      : 'default view'
  const caption =
    `render · ${viewpoint}` +
    (newest ? ' · live' : '') +
    (shown ? ` · ${fmtT(shown.t)}` : '') +
    (busy ? ' · rendering…' : '')

  return <Picture src={shown?.src ?? null} caption={caption} missing={missing} error={error} />
}

registerPanel({
  manifest: {
    type: 'camera',
    label: 'Camera',
    defaultPosition: { anchor: 'center', width: 480, height: 500 },
    resizable: true,
    minimizable: true,
  },
  component: CameraPanel,
})

export default CameraPanel
