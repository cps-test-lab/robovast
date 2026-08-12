// Scene3DPanel (type `scene3d`): the 3D world view -- the run view's full-bleed base layer.
//
// It reads the two artifacts that define a replay, and nothing else:
//
//   * the **scene** descriptor (`scene.json`/`scene.bin`) -- the geometry: a body tree with rest
//     transforms and named joints. Static per *world*, so it is not a per-run artifact and is no longer
//     shipped by every campaign: the service compiles it on demand, in the campaign's own image, the
//     first time somebody opens a 3D view, and caches it by world identity. The panel asks
//     `GET /campaigns/{id}/scene`, POSTs once if nothing is cached, and loads the URL it is handed.
//   * the **run capture** (`capture.json`/`capture.bin`) -- the motion: named joint-value and
//     body-pose tracks over a time base. Per run, and only the simulator that ran can produce it.
//
// Both formats are specified in robovast/docs/run_capture.rst; rst is the first producer of each.
//
// This replaces reading a `poses` table out of the postprocessed `data.db`. That path needed a rosbag,
// a `rosbags_tf_to_csv` step and a postprocessing run before anything moved, imposed a naming contract
// on the simulator ("emit a TF frame per moving body, named after the body") plus a `bind` list for its
// exceptions, and could only ever animate world-parented bodies -- so an articulated robot replayed
// rigid. None of that survives: tracks address the geometry by *name*, so nothing has to be listed,
// and joint tracks drive the loader's `jointMap`, which no panel used before.
//
// The panel talks to a `MotionSource` (see lib/scene3d/motionSource.ts), not to a file. A live source
// implements the same interface, so following a running simulation is a new source rather than a new
// panel -- and because every update arrives through `subscribe`, that path is exercised here from the
// first frame rather than merely declared.
//
// Bindings (vast visualization.panels):
//   capture:
//     path: <path>                 run capture manifest (default capture/capture.json)
//
// Geometry needs no binding at all: the run's capture names the world it used, so `- scene3d:` on its
// own is a complete panel. `scene.scope`/`capture.scope` are gone -- with the descriptor resolved by
// content key there is nothing to declare, and nothing to declare *wrongly* (a campaign-scope
// descriptor pointed at a world that varies per config rendered confidently wrong geometry).

import { useCallback, useEffect, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { registerPanel } from '@/lib/dashboard/registry'
import type { PanelProps } from '@robovast/panel-kit'
import { robovast, type SceneStatus } from '@/lib/robovastClient'
import type { MotionSink, MotionSource } from '@/lib/scene3d/motionSource'
import { openRunCapture } from '@/lib/scene3d/runCapture'
import { loadScene, type SceneModel } from '@/lib/scene3d/sceneLoader'
import { SceneViewport } from '@/lib/scene3d/viewport'

/** Where a run's capture manifest lives unless the panel says otherwise. Exported because RunView
 *  needs the same answer to find the run's time base -- a `scene3d` panel implies a capture whether or
 *  not it spells one out, and two copies of this string would drift. */
export const DEFAULT_CAPTURE_PATH = 'capture/capture.json'

/** How often to re-ask while geometry is being built. A warm cluster build is ~8 s and a cold one up
 *  to a couple of minutes (a 2 GB image pull), so a second is responsive without making the wait
 *  itself expensive. */
const SCENE_POLL_MS = 1000

/** What each stage is called, naming the *cost* rather than the mechanism -- the point of showing a
 *  stage at all is that a two-minute image pull must not look like a hang. */
const STAGE_TEXT: Record<string, string> = {
  queued: 'Waiting for cluster capacity \u2014 the campaign queue is busy',
  pulling: 'Fetching the simulation image onto the node \u2014 first time only',
  compiling: 'Compiling the world geometry',
  transferring: 'Copying the scene back from the container',
}

/** What the scene could not be driven by, so an empty-looking view can explain itself. */
interface Mismatch {
  unresolved: string[]
  resolved: number
  total: number
  world?: string
  producer?: string
}

function Scene3DPanel({ spec, clock, data }: PanelProps) {
  const captureCfg = (spec.config.capture ?? {}) as { path?: unknown }
  const capturePath = String(captureCfg.path ?? DEFAULT_CAPTURE_PATH)
  const captureUrl = data.runFileUrl(capturePath)

  const containerRef = useRef<HTMLDivElement | null>(null)
  const viewportRef = useRef<SceneViewport | null>(null)
  const modelRef = useRef<SceneModel | null>(null)
  const sourceRef = useRef<MotionSource | null>(null)
  const sinkRef = useRef<MotionSink | null>(null)

  const [scene, setScene] = useState<SceneStatus | null>(null)
  const [sceneError, setSceneError] = useState<string | null>(null)
  const [captureError, setCaptureError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [mismatch, setMismatch] = useState<Mismatch | null>(null)

  /** Seat the scene at the sample nearest `t`. Nothing here allocates: the source pushes into the
   *  sink, which is the loader's own imperative API, and the viewport redraws continuously. */
  const applyAt = useCallback((t: number) => {
    const source = sourceRef.current
    const sink = sinkRef.current
    if (!source || !sink) return
    const index = source.indexAt(t)
    if (index >= 0) source.apply(index, sink)
  }, [])

  /** Match the source's tracks against the scene and seat the current sample.
   *
   *  Called from *both* loaders, because either can win the race: checking only when the capture
   *  arrives would skip the report whenever the capture resolved first (its callback would find no
   *  model yet), and a capture recorded against a different world would then render confidently and
   *  wrongly -- the one failure this report exists to catch.
   */
  const syncFromSource = useCallback(() => {
    const source = sourceRef.current
    const model = modelRef.current
    if (!source || !model) return
    const known = new Set([...model.bodies, ...model.joints])
    const names = source.tracks().map((t) => t.name)
    const unresolved = names.filter((n) => !known.has(n))
    const meta = source.meta()
    setMismatch(
      unresolved.length
        ? {
            unresolved: unresolved.slice(0, 8),
            resolved: names.length - unresolved.length,
            total: names.length,
            world: meta.world,
            producer: meta.producer,
          }
        : null,
    )
    applyAt(clock.t)
    // clock is read for its current position only; subscribing to it happens separately.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applyAt])

  // One viewport per mount. PanelHost remounts the panel per run, so switching run or campaign builds
  // a fresh one and disposes this.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const viewport = new SceneViewport(el)
    viewportRef.current = viewport
    return () => {
      viewportRef.current = null
      viewport.dispose()
    }
  }, [])

  // Resolve the geometry through the service: ask, POST once if nothing is cached, then poll until it
  // is. Asking never builds -- that is what makes it safe to re-render, prefetch, or reload mid-build.
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let asked = false
    setScene(null)
    setSceneError(null)

    const poll = async () => {
      try {
        const status = await robovast.sceneStatus(data.campaignId, data.configName, data.runId)
        if (cancelled) return
        setScene(status)
        if (status.error) return
        if (status.cached) return
        // One POST per mount. Re-posting each tick would be harmless (the service joins an in-flight
        // build) but it would also hide a build that silently never starts.
        if (!asked && !status.in_progress) {
          asked = true
          const started = await robovast.runScene(data.campaignId, data.configName, data.runId)
          if (cancelled) return
          if (!started.ok) {
            setSceneError(started.message)
            return
          }
        }
        timer = setTimeout(poll, SCENE_POLL_MS)
      } catch (err: unknown) {
        if (cancelled) return
        setSceneError(err instanceof Error ? err.message : String(err))
      }
    }
    void poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [data.campaignId, data.configName, data.runId])

  const sceneUrl = scene?.cached && scene.url ? robovast.sceneAssetUrl(scene.url) : ''

  // Load the geometry once the service says it is ready. Disposing the previous model matters even
  // though the panel usually remounts: a URL that changes *within* a mounted viewport (a campaign
  // switch that keeps the panel alive) would otherwise leave the old world's buffers on the GPU for
  // the life of the tab.
  useEffect(() => {
    if (!sceneUrl) return
    let cancelled = false
    loadScene(sceneUrl)
      .then((model) => {
        if (cancelled) {
          model.dispose()
          return
        }
        modelRef.current?.dispose()
        modelRef.current = model
        // The sink is the scene model: a joint track drives jointMap, a pose track basePose.
        sinkRef.current = {
          joint: (name, value) => model.jointMap[name]?.(value),
          pose: (name, pos, quat) => model.basePose(name, pos, quat),
        }
        viewportRef.current?.setSceneRoot(model.root)
        if (model.view) viewportRef.current?.setView(model.view)
        syncFromSource()
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setSceneError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [sceneUrl, syncFromSource])

  // Open the motion source. Guarded the same way, so a late resolve from the campaign we just left
  // can never be applied to the one now showing.
  useEffect(() => {
    let cancelled = false
    setCaptureError(null)
    setMismatch(null)
    setLoading(true)
    let unsubscribe: (() => void) | null = null

    openRunCapture(captureUrl)
      .then((source) => {
        if (cancelled) {
          source.dispose()
          return
        }
        sourceRef.current?.dispose()
        sourceRef.current = source
        setLoading(false)
        // Every update arrives here -- for a file that is once, when the buffer is parsed; for a live
        // source it is per batch. The panel has one way to hear about data either way.
        unsubscribe = source.subscribe(syncFromSource)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoading(false)
        setCaptureError(err instanceof Error ? err.message : String(err))
      })

    return () => {
      cancelled = true
      unsubscribe?.()
    }
  }, [captureUrl, syncFromSource])

  // Dispose whatever this mount still owns. Separate from the loaders so their guards stay simple.
  useEffect(
    () => () => {
      sourceRef.current?.dispose()
      sourceRef.current = null
      modelRef.current?.dispose()
      modelRef.current = null
      sinkRef.current = null
    },
    [],
  )

  useEffect(() => clock.subscribe(() => applyAt(clock.t)), [clock, applyAt])

  // Non-empty exactly while geometry is being built, and never once it is cached or has failed --
  // polling a dead task while showing "nearly there" is worse than showing the error.
  const buildingText =
    scene && !scene.cached && !scene.error && !sceneError
      ? STAGE_TEXT[scene.stage] ?? 'Building the world geometry'
      : ''

  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: '#12171f' }}>
      <Box ref={containerRef} sx={{ position: 'absolute', inset: 0 }} />
      {loading && !captureError && !buildingText ? (
        <CircularProgress size={24} sx={{ position: 'absolute', top: 16, left: 16 }} />
      ) : null}
      {/* Building geometry is a *named* wait, not a spinner: a cold cluster miss is up to two minutes,
          almost all of it a 2 GB image pull, and a blank viewport that long is indistinguishable from a
          broken one. Same reason the Data browser announces its first-query fetch. */}
      {buildingText ? (
        <Alert
          severity="info"
          icon={<CircularProgress size={16} />}
          sx={{ position: 'absolute', top: 8, left: 8, maxWidth: 560 }}
        >
          {buildingText}
          {scene?.world ? (
            <>
              {' '}
              (<code>{scene.world}</code>)
            </>
          ) : null}
          . Built once per world, then cached — every other run of this world is instant.
        </Alert>
      ) : null}
      {/* TOP CENTRE, unlike every other overlay here: top-left is where a run view's own panels
          anchor (the scenario tree is `anchor: top-left` in more than one campaign), so this alert
          sat *behind* one and a run with no geometry looked like a run with an empty world. The
          centre is the one edge of the viewport nothing else claims. Translated by half its own
          width rather than given a width, so a short message stays as narrow as it reads. */}
      {sceneError || scene?.error ? (
        <Alert
          severity="warning"
          sx={{
            position: 'absolute',
            top: 8,
            left: '50%',
            transform: 'translateX(-50%)',
            maxWidth: 'min(620px, calc(100% - 16px))',
          }}
        >
          No 3D geometry: {sceneError || scene?.error}
        </Alert>
      ) : scene && !scene.overrides_known ? (
        <Alert severity="warning" sx={{ position: 'absolute', top: 8, left: 8, maxWidth: 620 }}>
          {scene.note}
        </Alert>
      ) : null}
      {captureError ? (
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 620 }}>
          No motion to replay: <code>{capturePath}</code> could not be read ({captureError}).
          Recording a capture is the simulator backend&apos;s to enable — see its documentation.
          Both the recording and the capture are written when the run stops cleanly, so a run
          killed by a per-run timeout has neither.
        </Alert>
      ) : mismatch ? (
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 620 }}>
          {mismatch.resolved
            ? `${mismatch.resolved} of ${mismatch.total} tracks drive this scene; `
            : 'None of this capture’s tracks name anything in this scene; '}
          unmatched: <code>{mismatch.unresolved.join(', ')}</code>
          {mismatch.world ? (
            <>
              . The capture names world <code>{mismatch.world}</code>
              {mismatch.producer ? ` (producer ${mismatch.producer})` : ''} — check it is the world
              this scene was exported from.
            </>
          ) : null}
        </Alert>
      ) : null}
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'scene3d',
    label: '3D scene',
    // The base layer: it takes whatever the docked panels leave over, and the overlay panels
    // float on top of it. Its chrome is declared here (`frameless` below), not inferred from
    // the position -- `fill` says where the panel goes, the manifest says what it looks like.
    defaultPosition: { fill: true },
    resizable: false,
    minimizable: false,
    frameless: true,
  },
  component: Scene3DPanel,
})

export default Scene3DPanel
