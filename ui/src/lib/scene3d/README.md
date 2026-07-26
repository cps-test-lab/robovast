# scene3d — the 3D scene viewer core

Renders the browser scene descriptor (`scene.json` + `scene.bin`, plus `tex_<i>.png`) that
rst exports — via the `rst-export-web` CLI or the `MujocoSim` adapter's
`SIM_SUITE_SCENE_EXPORT_DIR` hook. Any simulator that emits the same descriptor renders here too;
the format is owned by rst (`rst/export_web.py`), the reference loader is this one.

- `sceneLoader.ts` — descriptor → three.js `Group`, plus an imperative animation API:
  `jointMap[name](value)` for hinge/slide joints and `basePose(body, pos, quat)` for world-frame
  body poses.
- `sceneTf.ts` — TF-chain composition (`map -> odom -> base_link`) for data sources that deliver
  raw `/tf` transforms; unused by the playback path (poses arrive already map-frame), kept for the
  future live view.
- `viewport.ts` — a plain-three viewport (renderer, camera, lights, grid, orbit controls, Z-up
  wrapper group, resize, dispose).

**Extractability rule: files in this directory import only `three` (and `three/addons`) — never
`@/…` or anything robovast-specific.** The directory is shared-candidate code for other projects
rendering the same descriptor, so keeping this boundary makes extraction into a common package
mechanical. Host-specific wiring (data providers, clocks, panel/config plumbing) belongs in the
consumer — here, `ui/src/panels/Scene3DPanel.tsx`.
