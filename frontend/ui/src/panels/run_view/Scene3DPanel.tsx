// Scene3DPanel (type `scene3d`): the 3D world view -- the run view's full-bleed base layer.
//
// It reads two things, and nothing else:
//
//   * the **scene** descriptor (`scene.json`/`scene.bin`) -- the geometry: a body tree with rest
//     transforms and named joints. Static per *world*, so it is not a per-run artifact and is not
//     shipped by every campaign: the service compiles it on demand, in the campaign's own image, the
//     first time somebody opens a 3D view, and caches it by world identity. The panel asks
//     `GET /campaigns/{id}/scene`, POSTs once if nothing is cached, and loads the URL it is handed.
//   * the run's **motion**, from its tables: `sim_poses` (a pose per body per sample) and
//     `joint_states` (a value per joint per sample), decoded by the service from the simulator's own
//     recording -- for a finished run and, as it grows, for one still recording. The tables address
//     the geometry by *name*, so nothing has to be listed, and joint rows drive the loader's
//     `jointMap`.
//
// The panel talks to a `MotionSource` (see lib/scene3d/motionSource.ts), not to a table: the source
// (lib/scene3d/rowMotion.ts) loads a window of time around the clock and pages through it, and for a
// live run appends the rows the provider's subscription delivers. Every update arrives through
// `subscribe`, so a finished run and a live one are one code path here.
//
// Bindings (vast visualization.panels):
//   motion:
//     poses: <table>     the pose table (default sim_poses)
//     joints: <table>    the joint table (default joint_states)
//
// Geometry needs no binding at all: the service resolves the world the run used, so `- scene3d:` on
// its own is a complete panel.

import { useCallback, useEffect, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { registerPanel } from '@/lib/panels/registry'
import { isLiveProvider } from '@/lib/panels/dataProvider'
import type { PanelProps } from '@robovast/panel-kit'
import { robovast } from '@/lib/robovastClient'
import type { MotionMeta, MotionSink, MotionSource } from '@/lib/scene3d/motionSource'
import { CANVAS } from '@/colors'
import {
  DEFAULT_JOINT_TABLE, DEFAULT_POSE_TABLE, openRowMotion, type RowReader,
} from '@/lib/scene3d/rowMotion'
import type { SceneModel } from '@/lib/scene3d/sceneLoader'
import { sceneModels, type SceneLease } from '@/lib/scene3d/sceneModelCache'
import { useSceneGeometry } from '@/lib/scene3d/useSceneGeometry'
import { SceneViewport } from '@/lib/scene3d/viewport'
import { registerSceneReset } from './sceneReset'

/** Half the window of motion kept loaded around the clock, in seconds. Wide enough that ordinary
 *  scrubbing stays inside it; the source pages the window at the query's row cap, so a wider one
 *  costs queries, not correctness. */
const HALF_WINDOW_S = 30
/** How close to the window's edge the clock may get before the next one is asked for. */
const REFILL_MARGIN_S = 10

/** The columns of a pose row the source reads, so a page carries no more than it needs. */
const POSE_COLUMNS = ['timestamp', 'frame', 'position.x', 'position.y', 'position.z',
  'orientation.x', 'orientation.y', 'orientation.z', 'orientation.w']
const JOINT_COLUMNS = ['timestamp', 'joint', 'position']

/** What the scene could not be driven by, so an empty-looking view can explain itself. */
interface Mismatch {
  unresolved: string[]
  resolved: number
  total: number
  world?: string
  producer?: string
}

function Scene3DPanel({ spec, clock, data }: PanelProps) {
  const motionCfg = (spec.config.motion ?? {}) as { poses?: unknown; joints?: unknown }
  const poseTable = String(motionCfg.poses ?? DEFAULT_POSE_TABLE)
  const jointTable = String(motionCfg.joints ?? DEFAULT_JOINT_TABLE)

  const containerRef = useRef<HTMLDivElement | null>(null)
  const viewportRef = useRef<SceneViewport | null>(null)
  const modelRef = useRef<SceneModel | null>(null)
  const leaseRef = useRef<SceneLease | null>(null)
  const sourceRef = useRef<MotionSource | null>(null)
  const sinkRef = useRef<MotionSink | null>(null)

  const [motionError, setMotionError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [mismatch, setMismatch] = useState<Mismatch | null>(null)
  /** Whether the source has any sample yet -- a live run's tables can be empty for a while. */
  const [empty, setEmpty] = useState(false)

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
   *  Called from *both* loaders, because either can win the race: checking only when the motion
   *  arrives would skip the report whenever the motion resolved first (its callback would find no
   *  model yet), and a run recorded against a different world would then render confidently and
   *  wrongly -- the one failure this report exists to catch. For a live run it runs per batch, so
   *  a body that appears mid-run is checked when it does.
   */
  const syncFromSource = useCallback(() => {
    const source = sourceRef.current
    const model = modelRef.current
    if (!source) return
    setEmpty(source.indexAt(0) < 0)
    if (!model) return
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
  // a fresh one and disposes this. The *model* outlives it: it is leased from `sceneModels` and handed
  // back on unmount, so the next run of the same world is seated in the parsed geometry this one
  // showed rather than fetching and building it again.
  //
  // The viewport's lifetime is also what the header's "Reset 3D view" entry follows: registering here
  // rather than once per panel *type* means the entry is offered while a camera exists to re-frame,
  // and withdrawn the moment this mount goes away.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const viewport = new SceneViewport(el)
    viewportRef.current = viewport
    const unregister = registerSceneReset(() => viewport.resetView())
    return () => {
      unregister()
      viewportRef.current = null
      viewport.dispose()
    }
  }, [])

  // Resolving a descriptor is the same protocol in both scene panels, so it is one hook.
  // Two distinct failures, kept apart: `resolveError` is the service refusing or failing to build
  // the descriptor, `loadError` is bytes that arrived and would not parse. Both mean "no geometry",
  // but only one of them is worth retrying.
  const [loadError, setLoadError] = useState<string | null>(null)
  const {
    status: scene,
    error: resolveError,
    url: sceneUrl,
    buildingText,
    buildingDetail,
  } = useSceneGeometry(
    () => robovast.sceneStatus(data.campaignId, data.configName, data.runId),
    () => robovast.runScene(data.campaignId, data.configName, data.runId),
    `${data.campaignId}/${data.configName}/${data.runId}`,
  )


  // Lease the geometry once the service says it is ready. Handing the previous lease back matters
  // even though the panel usually remounts: a URL that changes *within* a mounted viewport (a campaign
  // switch that keeps the panel alive) would otherwise keep the old world out of the cache and its
  // buffers on the GPU for the life of the tab.
  useEffect(() => {
    if (!sceneUrl) return
    let cancelled = false
    sceneModels
      .acquire(sceneUrl)
      .then((lease) => {
        if (cancelled) {
          lease.release()
          return
        }
        leaseRef.current?.release()
        leaseRef.current = lease
        const { model } = lease
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
        setLoadError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [sceneUrl, syncFromSource])

  // Open the motion source over the provider. The reader is the provider's page and, for a live
  // run, its subscription; the world the run used comes from `sim_recording` (one row per run) so
  // a mismatch can be named. Guarded the same way as the geometry, so a late resolve from the run
  // we just left can never be applied to the one now showing.
  useEffect(() => {
    let cancelled = false
    setMotionError(null)
    setMismatch(null)
    setLoading(true)
    const columns = { [poseTable]: POSE_COLUMNS, [jointTable]: JOINT_COLUMNS }
    const host = isLiveProvider(data) ? data : null
    const reader: RowReader = {
      page: (table, t0, t1, maxRows) =>
        data.seriesPage(table, { t0, t1, maxRows, columns: columns[table] })
          .then((page) => ({ rows: page.rows, truncated: page.truncated })),
      ...(host?.live ? { follow: (table, listener) => host.subscribeLive(table, listener) } : {}),
    }
    const source = openRowMotion(reader, { poseTable, jointTable })
    sourceRef.current?.dispose()
    sourceRef.current = source
    const unsubscribe = source.subscribe(syncFromSource)

    // The window the source has been asked for, so the clock is compared against an ask rather
    // than against what happens to be loaded -- a live run's tail grows past any ask.
    let window: [number, number] | null = null
    const askAround = (t: number) => {
      if (window && t >= window[0] + REFILL_MARGIN_S && t <= window[1] - REFILL_MARGIN_S) return
      window = [t - HALF_WINDOW_S, t + HALF_WINDOW_S]
      source.fetch(window[0], window[1]).then(
        () => {
          if (cancelled) return
          setLoading(false)
          setMotionError(null)
        },
        (err: unknown) => {
          if (cancelled) return
          setLoading(false)
          setMotionError(err instanceof Error ? err.message : String(err))
        },
      )
    }
    askAround(clock.t)
    const unclock = clock.subscribe(() => askAround(clock.t))

    // Provenance, for the mismatch report. Read once; a run's world does not change.
    const meta: MotionMeta = { frame: 'world', timeBase: 'sim' }
    host?.rows('sim_recording', { columns: ['world', 'seed'], maxRows: 1 })
      .then((rows) => {
        const row = rows[0]
        if (cancelled || !row) return
        meta.world = row.world == null ? undefined : String(row.world)
        meta.producer = 'simulator recording'
        // The source hands out one meta object for its life; filling it in is how a provenance
        // that arrives after the source opened reaches the next mismatch report.
        Object.assign(source.meta(), meta)
      })
      .catch(() => undefined) // a run with no `sim_recording` still has its motion

    return () => {
      cancelled = true
      unclock()
      unsubscribe()
    }
    // The clock is read for its position; its identity is per run, like the provider's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, poseTable, jointTable, syncFromSource])

  // Dispose whatever this mount still owns. Separate from the loaders so their guards stay simple.
  useEffect(
    () => () => {
      sourceRef.current?.dispose()
      sourceRef.current = null
      leaseRef.current?.release()
      leaseRef.current = null
      modelRef.current = null
      sinkRef.current = null
    },
    [],
  )

  useEffect(() => clock.subscribe(() => applyAt(clock.t)), [clock, applyAt])

  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: CANVAS }}>
      <Box ref={containerRef} sx={{ position: 'absolute', inset: 0 }} />
      {loading && !motionError && !buildingText ? (
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
          {/* The cluster's own words for the wait, when it has any. A pull that cannot succeed --
              a campaign imported from elsewhere whose image this cluster cannot reach -- says so
              here within seconds, where the stage alone would look like an ordinary cold start
              until the build's deadline ran out minutes later. */}
          {buildingDetail ? (
            <Box sx={{ mt: 0.5, fontSize: '0.85em', opacity: 0.85 }}>{buildingDetail}</Box>
          ) : null}
        </Alert>
      ) : null}
      {/* TOP CENTRE, unlike every other overlay here: top-left is where a run view's own panels
          anchor (the scenario tree is `anchor: top-left` in more than one campaign), so this alert
          sat *behind* one and a run with no geometry looked like a run with an empty world. The
          centre is the one edge of the viewport nothing else claims. Translated by half its own
          width rather than given a width, so a short message stays as narrow as it reads. */}
      {resolveError || loadError || scene?.error ? (
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
          No 3D geometry: {resolveError || loadError || scene?.error}
        </Alert>
      ) : scene && !scene.overrides_known ? (
        <Alert severity="warning" sx={{ position: 'absolute', top: 8, left: 8, maxWidth: 620 }}>
          {scene.note}
        </Alert>
      ) : null}
      {motionError ? (
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 620 }}>
          No motion to replay: <code>{poseTable}</code> and <code>{jointTable}</code> could not be
          read ({motionError}). Both are decoded from the simulator&apos;s own recording, which is
          the simulator backend&apos;s to enable — see its documentation.
        </Alert>
      ) : empty && !loading ? (
        <Alert severity="info" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 620 }}>
          {isLiveProvider(data) && data.live
            ? 'No motion recorded yet — the scene moves once the run’s first samples land.'
            : 'No motion recorded for this run.'}
        </Alert>
      ) : mismatch ? (
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 620 }}>
          {mismatch.resolved
            ? `${mismatch.resolved} of ${mismatch.total} tracks drive this scene; `
            : 'None of this run’s tracks name anything in this scene; '}
          unmatched: <code>{mismatch.unresolved.join(', ')}</code>
          {mismatch.world ? (
            <>
              . The recording names world <code>{mismatch.world}</code>
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
