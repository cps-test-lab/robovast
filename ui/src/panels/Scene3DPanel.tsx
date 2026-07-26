// Scene3DPanel (type `scene3d`): the 3D world view -- the run view's full-bleed base layer. It
// renders the run's exported scene descriptor (rst's `scene.json`/`scene.bin` run artifact,
// see ui/src/lib/scene3d/README.md) in a plain-three viewport and replays the robot: on every clock
// change the recorded map-frame pose nearest `t` drives the robot's base body.
//
// This file is the robovast adapter around the shared-candidate scene3d core: everything
// host-specific (the DataProvider, the PlaybackClock, the vast bindings) lives here, so the core
// stays extractable. A future live view swaps the provider/clock for live ones -- the panel and the
// core stay unchanged.
//
// Bindings (vast visualization.panels):
//   scene: { path }                   run-relative descriptor path (default scene/scene.json)
//   robot:
//     body: <scene body name>         the body basePose drives (default base_link)
//     source: { table, time_column, filter }
//                                     the pose time series; defaults to the rosbags_tf_to_csv
//                                     `poses` table filtered to the body's frame

import { useCallback, useEffect, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { Euler, Quaternion } from 'three'
import { registerPanel } from '@/lib/dashboard/registry'
import { useTimeSeries, type TimeSeriesBinding, type TimeSeriesSource } from '@/lib/dashboard/timeSeries'
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

function Scene3DPanel({ spec, clock, data }: PanelProps) {
  const sceneCfg = (spec.config.scene ?? {}) as { path?: unknown }
  const scenePath = String(sceneCfg.path ?? 'scene/scene.json')
  const robotCfg = (spec.config.robot ?? {}) as { body?: unknown; source?: TimeSeriesBinding }
  const body = String(robotCfg.body ?? 'base_link')
  const binding: TimeSeriesBinding = robotCfg.source ?? {
    table: 'poses',
    filter: { frame: body },
  }

  const containerRef = useRef<HTMLDivElement | null>(null)
  const viewportRef = useRef<SceneViewport | null>(null)
  const modelRef = useRef<SceneModel | null>(null)
  const poseRef = useRef<TimeSeriesSource | null>(null)
  const [sceneError, setSceneError] = useState<string | null>(null)
  const [sceneLoading, setSceneLoading] = useState(true)

  const poseQuery = useTimeSeries(binding, data, POSE_COLUMNS)

  // Seat the robot at the recorded pose nearest `t`. The viewport renders continuously, so
  // mutating the body matrix (basePose) is all an update takes -- no React state per frame.
  const applyPose = useCallback((t: number) => {
    const model = modelRef.current
    const src = poseRef.current
    if (!model || !src) return
    const row = src.at(t)
    if (!row) return
    const px = Number(row['position.x'])
    const py = Number(row['position.y'])
    const pz = Number(row['position.z'])
    const roll = Number(row['orientation.roll'])
    const pitch = Number(row['orientation.pitch'])
    const yaw = Number(row['orientation.yaw'])
    if (![px, py, pz, roll, pitch, yaw].every(Number.isFinite)) return
    // ROS RPY (extrinsic x-y-z) == three intrinsic 'ZYX'; basePose wants a wxyz quaternion.
    const q = new Quaternion().setFromEuler(new Euler(roll, pitch, yaw, 'ZYX'))
    model.basePose(body, [px, py, pz], [q.w, q.x, q.y, q.z])
  }, [body])

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

  // Load the run's scene descriptor into the viewport.
  useEffect(() => {
    let disposed = false
    setSceneLoading(true)
    setSceneError(null)
    loadScene(data.runFileUrl(scenePath))
      .then((model) => {
        if (disposed) return
        modelRef.current = model
        viewportRef.current?.setSceneRoot(model.root)
        if (model.view) viewportRef.current?.setView(model.view)
        setSceneLoading(false)
        applyPose(clock.t)
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
  }, [data, scenePath, clock, applyPose])

  // Index the loaded pose series, then follow the clock.
  useEffect(() => {
    if (!poseQuery.data) return
    poseRef.current = poseQuery.data
    applyPose(clock.t)
  }, [poseQuery.data, clock, applyPose])
  useEffect(() => clock.subscribe(() => applyPose(clock.t)), [clock, applyPose])

  const poseEmpty = poseQuery.data && !poseQuery.data.all().length

  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: '#12171f' }}>
      <Box ref={containerRef} sx={{ position: 'absolute', inset: 0 }} />
      {sceneLoading ? (
        <CircularProgress size={24} sx={{ position: 'absolute', top: 16, left: 16 }} />
      ) : null}
      {sceneError ? (
        <Alert severity="warning" sx={{ position: 'absolute', top: 8, left: 8, maxWidth: 480 }}>
          No 3D scene for this run ({sceneError}). Runs export one when the simulation sets{' '}
          <code>SIM_SUITE_SCENE_EXPORT_DIR</code>.
        </Alert>
      ) : null}
      {poseQuery.isError ? (
        <Alert severity="error" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 480 }}>
          {(poseQuery.error as Error).message}
        </Alert>
      ) : poseEmpty ? (
        <Alert severity="warning" sx={{ position: 'absolute', bottom: 8, left: 8, maxWidth: 480 }}>
          No rows in <code>{binding.table}</code> for this run — the robot stays at its rest pose.
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
