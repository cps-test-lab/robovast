// CameraPanel (type `camera`): a camera that was recorded during the run, played on the run's clock.
//
// This is what a backend with no 3D scene has instead of one. Gazebo writes no run capture and has no
// scene exporter, so a `scene3d` panel has nothing to replay there; a monitor camera spawned into the
// world and converted to a `.webm` by `rosbags_to_webm` gives that run view a picture of the trial.
// Placed like any other panel rather than as the `fill` base layer -- the other panels float over
// `fill`, and a camera image covered by them is worse than a smaller one that is whole.
//
// THE PANEL IS A READER OF THE CLOCK, NEVER A WRITER. The playback bar owns time (see
// panel-kit/src/clock.ts: one writer, every other panel a subscriber), so the `<video>` carries no
// native controls and never calls seek() -- it follows `t` and nothing else. Giving it controls would
// put two things in charge of when "now" is, and the run view would disagree with itself.
//
// Bindings (vast visualization.panels):
//   source: { topic }                 which camera, for a run that recorded several
//   source: { path, t0, t1?, fps? }   escape hatch for a video no producer registered
//
// A bare `- camera:` is a complete panel when the run registered exactly one video -- the same
// promise `scene3d` makes.

import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { registerPanel } from '@/lib/panels/registry'
import { resolveCameraSource, VIDEOS_TABLE, type CameraBinding } from '@/lib/camera/cameraSource'
import type { PanelProps } from '@robovast/panel-kit'

/** Frame rate assumed when the producer recorded none — only ever used for the seek tolerance. */
const FALLBACK_FPS = 10

/** How far the element may drift from the clock before it is seeked, in frame periods.
 *
 *  A formula rather than a constant, because this panel has to hold at both ends of the range it
 *  serves: 60 ms at 25 fps, 1.5 s at a monitor camera's 1 Hz. In both cases that is "within one
 *  frame", which is the only precision a video actually has — a tighter bound would seek constantly
 *  to land on the frame it was already showing, and each seek re-buffers. */
const TOLERANCE_FRAMES = 1.5

function CameraPanel({ spec, clock, data }: PanelProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [outOfRange, setOutOfRange] = useState(false)
  const binding = (spec.config.source ?? {}) as CameraBinding

  const query = useQuery({
    // The run scope leads, as everywhere: table names repeat across campaigns, and a key without it
    // serves the previous campaign's row after a switch.
    queryKey: ['camera-source', data.scope, binding.topic ?? null, binding.path ?? null],
    queryFn: () => resolveCameraSource(spec, data),
    retry: false,
  })
  const source = query.data ?? null

  /** Put the element where the clock says, doing as little as possible. */
  const sync = useCallback(() => {
    const el = videoRef.current
    if (!el || !source) return
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

  if (query.isPending) return <CircularProgress size={20} sx={{ m: 2 }} />
  if (query.isError)
    return (
      <Alert severity="error" sx={{ m: 1 }}>
        {(query.error as Error).message}
      </Alert>
    )

  if (!source)
    return (
      <Alert severity="info" sx={{ m: 1 }}>
        This run registered no video
        {binding.topic ? (
          <>
            {' '}
            for <code>{binding.topic}</code>
          </>
        ) : null}
        . A camera panel plays a recording listed in the <code>{VIDEOS_TABLE}</code> table — add{' '}
        <code>rosbags_to_webm</code> to <code>results_processing.postprocessing</code> (naming the
        image topic the scenario recorded), then re-run postprocessing.
      </Alert>
    )

  return (
    <Box sx={{ position: 'relative', height: '100%', bgcolor: 'common.black' }}>
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
      {outOfRange ? (
        <Box
          sx={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            bgcolor: 'rgba(0,0,0,0.65)',
            color: 'common.white',
            fontSize: 13,
            textAlign: 'center',
            px: 2,
          }}
        >
          No frames at this time
        </Box>
      ) : null}
    </Box>
  )
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
