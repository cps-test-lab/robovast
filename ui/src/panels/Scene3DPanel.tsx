// Scene3DPanel (type `scene3d`): the 3D world view -- the run view's full-bleed base layer. It
// renders the run's exported scene descriptor (rst's `scene.json`/`scene.bin` artifact, see
// ui/src/lib/scene3d/README.md) in a plain-three viewport and replays **everything that moved**: on
// every clock change each driven body is seated at the recorded pose nearest `t`.
//
// How a pose stream finds its body, and why nothing needs listing in the .vast: the substrate names a
// dynamic body's TF frame *after the body*. rst's `spawn_model` says so where it publishes ("frame is
// the MuJoCo body name (== the exported scene body name) so a viewer binds the transform to its node
// by name"), a walker broadcasts one transform per skeleton body under those body names, and the
// descriptor's skinned meshes are bound to those same body nodes. So the set of things to animate is
// the intersection of "frames this run recorded" and "bodies this scene has" -- discovered, not
// enumerated. Frames that are not bodies (`map`, `odom`) fall out for free.
//
// `bind` exists for the exceptions to that convention. There is one in practice: a ground-truth pose
// plugin publishes `<model>_base_link_gt` rather than the body name, deliberately, so that a rosbag
// carries the same frame name under any simulator.
//
// This file is the robovast adapter around the shared-candidate scene3d core: everything
// host-specific (the DataProvider, the PlaybackClock, the vast bindings) lives here, so the core
// stays extractable. A future live view swaps the provider/clock for live ones -- the panel and the
// core stay unchanged.
//
// Bindings (vast visualization.panels):
//   scene:
//     path: <path>                    descriptor path (default scene/scene.json)
//     scope: run | campaign           whose file space `path` is relative to (default run). `campaign`
//                                     is for a world every run compiled identically -- one copy for
//                                     the campaign instead of one per run.
//   poses:
//     source: { table, key, time_column, filter }
//                                     the frame-keyed pose table (default `poses` keyed by `frame`)
//     decimate_hz: <n>                cap samples per body (default 20 -- a viewer needs no more)
//     max_rows: <n>                   safety bound on the single query (default 100000)
//     bind: [{ body, frame }]         only where a frame's name differs from its body's
//   robot: { body, source }           deprecated single-body form, kept working: equivalent to one
//                                     `bind` entry plus that entry's own source table.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { useQuery } from '@tanstack/react-query'
import { Euler, Quaternion } from 'three'
import { registerPanel } from '@/lib/dashboard/registry'
import {
  useTimeSeriesGroups,
  type TimeSeriesBinding,
  type TimeSeriesGroupBinding,
  type TimeSeriesSource,
} from '@/lib/dashboard/timeSeries'
import type { PanelProps } from '@/lib/dashboard/types'
import { loadScene, type SceneModel } from '@/lib/scene3d/sceneLoader'
import { SceneViewport } from '@/lib/scene3d/viewport'

// The pose columns rosbags_tf_to_csv writes: map-frame position + RPY orientation.
const POSE_COLUMNS = [
  'position.x',
  'position.y',
  'position.z',
  'orientation.roll',
  'orientation.pitch',
  'orientation.yaw',
]

// A viewport redraws at display rate from whatever sample is nearest `t`, so pose data denser than
// this buys nothing visible while multiplying the query by the number of moving bodies.
const DEFAULT_DECIMATE_HZ = 20
const DEFAULT_MAX_ROWS = 100_000

interface PoseCfg {
  source?: TimeSeriesBinding & { key?: string }
  decimate_hz?: number
  max_rows?: number
  bind?: { body?: unknown; frame?: unknown }[]
}

/** Resolve the .vast bindings into one grouped query + the explicit frame->body overrides. */
function readConfig(config: Record<string, unknown>) {
  const poses = (config.poses ?? {}) as PoseCfg
  const robot = (config.robot ?? {}) as { body?: unknown; source?: TimeSeriesBinding }
  // The deprecated single-body form: its `source` names the table (and usually filters it to the one
  // frame), its `body` names the target. Expressing it as an override keeps one code path.
  const legacyBody = robot.body != null ? String(robot.body) : null
  const legacyFrame = legacyBody
    ? ((robot.source?.filter?.frame as string | undefined) ?? legacyBody)
    : null

  const src: Partial<TimeSeriesBinding> & { key?: string } = poses.source ?? robot.source ?? {}
  const binding: TimeSeriesGroupBinding = {
    table: src.table ?? 'poses',
    key: src.key ?? 'frame',
    time_column: src.time_column,
    // A legacy `filter` pinned the table to one frame; as a grouped query that filter would hide
    // every other body, so it is dropped and re-expressed as the override below.
    filter: poses.source?.filter,
    decimate_hz: poses.decimate_hz ?? DEFAULT_DECIMATE_HZ,
  }

  const bodyByFrame = new Map<string, string>()
  if (legacyBody && legacyFrame) bodyByFrame.set(legacyFrame, legacyBody)
  for (const entry of poses.bind ?? []) {
    if (entry?.body == null || entry?.frame == null) continue
    bodyByFrame.set(String(entry.frame), String(entry.body))
  }
  return { binding, bodyByFrame, maxRows: poses.max_rows ?? DEFAULT_MAX_ROWS }
}

function Scene3DPanel({ spec, clock, data }: PanelProps) {
  const sceneCfg = (spec.config.scene ?? {}) as { path?: unknown; scope?: unknown }
  const scenePath = String(sceneCfg.path ?? 'scene/scene.json')
  const campaignScope = String(sceneCfg.scope ?? 'run') === 'campaign'
  const sceneUrl = campaignScope ? data.campaignFileUrl(scenePath) : data.runFileUrl(scenePath)

  const { binding, bodyByFrame, maxRows } = useMemo(
    () => readConfig(spec.config),
    [spec.config],
  )

  const containerRef = useRef<HTMLDivElement | null>(null)
  const viewportRef = useRef<SceneViewport | null>(null)
  const modelRef = useRef<SceneModel | null>(null)
  // frame -> its series, already narrowed to the bodies this scene actually has.
  const drivenRef = useRef<[string, TimeSeriesSource][]>([])
  const [sceneError, setSceneError] = useState<string | null>(null)
  const [sceneLoading, setSceneLoading] = useState(true)
  const [bodies, setBodies] = useState<string[] | null>(null)

  const poseQuery = useTimeSeriesGroups(binding, data, POSE_COLUMNS, maxRows)

  // What the run recorded, independent of the scene: used only to explain an empty view, so a run
  // whose frames match no body says which frames it *did* have rather than showing a still scene.
  const framesQuery = useQuery({
    queryKey: ['scene3d-frames', binding.table, binding.key],
    queryFn: () => data.distinct(binding.table, binding.key),
    retry: false,
  })

  // Seat every driven body at its recorded pose nearest `t`. The viewport renders continuously, so
  // mutating body matrices (basePose) is all an update takes -- no React state per frame.
  const applyPoses = useCallback((t: number) => {
    const model = modelRef.current
    if (!model) return
    const q = new Quaternion()
    const euler = new Euler()
    for (const [frame, series] of drivenRef.current) {
      const row = series.at(t)
      if (!row) continue
      const px = Number(row['position.x'])
      const py = Number(row['position.y'])
      const pz = Number(row['position.z'])
      const roll = Number(row['orientation.roll'])
      const pitch = Number(row['orientation.pitch'])
      const yaw = Number(row['orientation.yaw'])
      if (![px, py, pz, roll, pitch, yaw].every(Number.isFinite)) continue
      // ROS RPY (extrinsic x-y-z) == three intrinsic 'ZYX'; basePose wants a wxyz quaternion.
      q.setFromEuler(euler.set(roll, pitch, yaw, 'ZYX'))
      model.basePose(bodyByFrame.get(frame) ?? frame, [px, py, pz], [q.w, q.x, q.y, q.z])
    }
  }, [bodyByFrame])

  // Mount the viewport (PanelHost remounts the panel per run, so one viewport per run).
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

  // Load the scene descriptor into the viewport.
  useEffect(() => {
    let disposed = false
    setSceneLoading(true)
    setSceneError(null)
    setBodies(null)
    loadScene(sceneUrl)
      .then((model) => {
        if (disposed) return
        modelRef.current = model
        viewportRef.current?.setSceneRoot(model.root)
        if (model.view) viewportRef.current?.setView(model.view)
        setSceneLoading(false)
        setBodies(model.bodies)
      })
      .catch((err: unknown) => {
        if (disposed) return
        setSceneLoading(false)
        setSceneError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      disposed = true
      modelRef.current = null
    }
  }, [sceneUrl])

  // Match the recorded series onto the scene's bodies, then follow the clock. This is the whole
  // convention: a series drives a body when its frame names one (or `bind` says which).
  useEffect(() => {
    if (!poseQuery.data || !bodies) return
    const present = new Set(bodies)
    // A body an explicit `bind` claims is driven by *that* series only. Without this the two
    // collide whenever a bound frame targets a body some other frame is also named after -- which is
    // the normal case, not a corner one: a run records both `base_link` (the localization estimate)
    // and `<model>_base_link_gt` bound onto the `base_link` body, so both would write the same body
    // every tick and the last one applied would win, non-deterministically, with the robot drawn
    // wherever the loser was not. An override that merely competes is not an override.
    const claimed = new Set(bodyByFrame.values())
    drivenRef.current = Array.from(poseQuery.data).filter(([frame]) => {
      const bound = bodyByFrame.get(frame)
      const body = bound ?? frame
      if (!present.has(body)) return false
      return bound != null || !claimed.has(body)
    })
    applyPoses(clock.t)
  }, [poseQuery.data, bodies, bodyByFrame, clock, applyPoses])
  useEffect(() => clock.subscribe(() => applyPoses(clock.t)), [clock, applyPoses])

  const driven = poseQuery.data && bodies ? drivenRef.current.length : null
  const recorded = framesQuery.data?.length ?? 0

  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: '#12171f' }}>
      <Box ref={containerRef} sx={{ position: 'absolute', inset: 0 }} />
      {sceneLoading ? (
        <CircularProgress size={24} sx={{ position: 'absolute', top: 16, left: 16 }} />
      ) : null}
      {sceneError ? (
        <Alert severity="warning" sx={{ position: 'absolute', top: 8, left: 8, maxWidth: 560 }}>
          No 3D scene at <code>{scenePath}</code> ({sceneError}). A campaign ships one either as a
          per-run artifact (the simulation sets <code>SIM_SUITE_SCENE_EXPORT_DIR</code>) or as a
          campaign-level file addressed with <code>scene.scope: campaign</code>.
        </Alert>
      ) : null}
      {poseQuery.isError ? (
        <Alert severity="error" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 560 }}>
          {(poseQuery.error as Error).message}
        </Alert>
      ) : driven === 0 ? (
        // Nothing moves. Name the mismatch rather than showing a silently static world: the usual
        // cause is a frame whose name is not its body's, which is what `bind` is for.
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 560 }}>
          Nothing to replay: none of the {recorded} frame(s) in <code>{binding.table}</code> names a
          body in this scene. A dynamic body's frame must be named after the body — otherwise map it
          with <code>poses.bind: [{'{'} body, frame {'}'}]</code>.
        </Alert>
      ) : null}
    </Box>
  )
}

registerPanel({
  manifest: {
    type: 'scene3d',
    label: '3D scene',
    // The full-view base layer: PanelHost renders `fill` frameless behind the overlay panels.
    defaultPosition: { anchor: 'fill' },
    resizable: false,
    minimizable: false,
    frameless: true,
  },
  component: Scene3DPanel,
})

export default Scene3DPanel
