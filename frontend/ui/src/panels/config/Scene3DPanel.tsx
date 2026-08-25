// Scene3DPanel (config-view type `scene3d`): the world a .vast declares, with what the selected
// configuration's variations put in it drawn on top.
//
// It reuses the run view's renderer wholesale — the same loadScene, the same SceneViewport (so the
// same wheel-to-fly navigation), the same useSceneGeometry protocol against the workspace endpoints.
// What it does not reuse is the motion side: nothing has run, so there is no capture and no clock.
//
// **The world is the campaign's base world.** Geometry is keyed on the .vast's world and its
// campaign-level overrides, not on the selected configuration, so switching configuration is a group
// swap rather than a container run. What a variation *placed* is drawn from its contribution
// instead. The caption says so, because a reader must not take this for a compiled preview of the
// exact model the run will load: an override that changes geometry through a plugin is not in the
// mesh, only what the variation contributes as markers.
//
// Bindings (vast visualization.config.panels):
//   markers:                       # literal markers, and ones bound to a resolved parameter
//     - {kind: pose, pos: [-8, 0], yaw: 0, label: start}
//     - {kind: pose, param: goal_pose, offset: [-8, 0, 0], label: goal}

import { useEffect, useMemo, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { declaredMarkers, type ConfigPanelProps, type SceneMarker } from '@robovast/panel-kit'
import { registerConfigPanel } from '@/lib/panels/registry'
import { robovast } from '@/lib/robovastClient'
import { CANVAS } from '@/colors'
import { buildMarkers, type MarkerLayer } from '@/lib/scene3d/markers'
import { loadScene, type SceneModel } from '@/lib/scene3d/sceneLoader'
import { useSceneGeometry } from '@/lib/scene3d/useSceneGeometry'
import { SceneViewport } from '@/lib/scene3d/viewport'

function Scene3DPanel({ spec, config, source }: ConfigPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const viewportRef = useRef<SceneViewport | null>(null)
  const modelRef = useRef<SceneModel | null>(null)
  const layerRef = useRef<MarkerLayer | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  const { status, error: resolveError, url, buildingText } = useSceneGeometry(
    () => robovast.workspaceSceneStatus(source.workspaceId, source.vastPath),
    () => robovast.runWorkspaceScene(source.workspaceId, source.vastPath),
    `${source.workspaceId}/${source.vastPath}`,
  )

  // What the variations contributed, plus what the .vast declared itself. Concatenated rather than
  // one overriding the other: a campaign whose factor is a plain parameter list contributes nothing,
  // and declaring the endpoints in the file is the only way to see them.
  const markers = useMemo<SceneMarker[]>(
    () => [...(config.contribution?.markers ?? []), ...declaredMarkers(spec.config, config)],
    [config, spec.config],
  )
  const groups = layerRef.current?.groups ?? []

  // One viewport per mount.
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

  // The world. Loaded once per descriptor; the markers below re-attach without touching it, which
  // is what makes clicking through configurations instant.
  useEffect(() => {
    if (!url) return
    let cancelled = false
    loadScene(url)
      .then((model) => {
        if (cancelled) {
          model.dispose()
          return
        }
        modelRef.current?.dispose()
        modelRef.current = model
        viewportRef.current?.setSceneRoot(model.root)
        if (model.view) viewportRef.current?.setView(model.view)
        // The markers belong to the scene root, so re-attach whatever is current: the world may
        // well arrive after them.
        if (layerRef.current) model.root.add(layerRef.current.root)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoadError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [url])

  // The selected configuration's markers.
  useEffect(() => {
    layerRef.current?.root.removeFromParent()
    layerRef.current?.dispose()
    const layer = buildMarkers(markers)
    layerRef.current = layer
    modelRef.current?.root.add(layer.root)
    setHidden(new Set())
    return () => {
      layer.root.removeFromParent()
      layer.dispose()
    }
  }, [markers])

  useEffect(
    () => () => {
      modelRef.current?.dispose()
      modelRef.current = null
    },
    [],
  )

  const toggle = (group: string) => {
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(group)) next.delete(group)
      else next.add(group)
      layerRef.current?.setGroupVisible(group, !next.has(group))
      return next
    })
  }

  const failure = resolveError || loadError || status?.error

  return (
    <Box sx={{ position: 'relative', width: '100%', height: '100%', bgcolor: CANVAS }}>
      <Box ref={containerRef} sx={{ position: 'absolute', inset: 0 }} />

      {buildingText ? (
        <Alert
          severity="info"
          icon={<CircularProgress size={16} />}
          sx={{ position: 'absolute', top: 8, left: 8, right: 8 }}
        >
          {buildingText}. Built once per world, then cached — every configuration, and every
          campaign that uses this world, is instant afterwards.
        </Alert>
      ) : null}

      {failure ? (
        <Alert severity="warning" sx={{ position: 'absolute', top: 8, left: 8, right: 8 }}>
          No 3D geometry: {failure}
        </Alert>
      ) : null}

      {config.contribution?.errors?.length ? (
        <Alert severity="warning" sx={{ position: 'absolute', top: 8, left: 8, right: 8 }}>
          {config.contribution.errors.join('; ')}
        </Alert>
      ) : null}

      {/* The legend doubles as visibility toggles, and the caption is the honesty note: this is the
          base world plus what the variations placed, not a compiled preview of the exact model. */}
      <Stack
        direction="row"
        spacing={0.5}
        sx={{ position: 'absolute', bottom: 6, left: 8, right: 8, flexWrap: 'wrap', gap: 0.5 }}
        alignItems="center"
      >
        {groups.map((group) => (
          <Chip
            key={group}
            size="small"
            label={group}
            variant={hidden.has(group) ? 'outlined' : 'filled'}
            onClick={() => toggle(group)}
            sx={{ height: 20, fontSize: '0.65rem' }}
          />
        ))}
        <Typography variant="caption" sx={{ color: 'rgba(255,255,255,0.5)', ml: 'auto' }}>
          base world + this configuration&apos;s placements
        </Typography>
      </Stack>
    </Box>
  )
}

registerConfigPanel({
  manifest: { type: 'scene3d', label: 'Scene' },
  component: Scene3DPanel,
})

export default Scene3DPanel
