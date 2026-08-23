# scene3d — the 3D scene viewer core

Renders the browser scene descriptor (`scene.json` + `scene.bin`, plus `tex_<i>.png`) that
roqsim exports — via the `roqsim-export-web` CLI or the `MujocoSim` adapter's
`ROQSIM_SCENE_EXPORT_DIR` hook. Any simulator that emits the same descriptor renders here too;
the format is owned by roqsim (`roqsim/export_web.py`), the reference loader is this one.

- `sceneLoader.ts` — descriptor → three.js `Group`, plus an imperative animation API:
  `jointMap[name](value)` for hinge/slide joints and `basePose(body, pos, quat)` for world-frame
  body poses.
- `sceneTf.ts` — TF-chain composition (`map -> odom -> base_link`) for data sources that deliver
  raw `/tf` transforms; unused by the playback path (poses arrive already map-frame), kept for the
  future live view.
- `viewport.ts` — a plain-three viewport (renderer, camera, lights, grid, orbit controls, its own
  wheel/pointer handlers, a frustum that tracks the camera, Z-up wrapper group, resize, dispose).
- `pivot.ts` — where the orbit pivot goes, and the reason the other two files are shaped as they are.
  Rotate, pan and the wheel are all scaled by the pivot distance, so it is measured against the scene
  under the cursor at the start of every gesture. A pivot left at the world's authored framing
  distance is right in the opening frame and wrong everywhere the camera travels to.
- `cursorDolly.ts` — the wheel: fly along the ray under the cursor, stepping a fraction of the pivot
  distance and leaving the pivot behind, so the approach slows as it closes on the surface aimed at.
  That is radius-scaling, which this file was originally written to avoid; the docstring says at
  length why the objection (a series converging on a pivot that can never be passed) does not apply
  to a pivot re-chosen every gesture with a floored step.

## Texture mapping

Two contract points a second implementation of this loader has to get right, both of them things
the descriptor cannot express any other way:

- **`meshes[].uv` is optional, and when present it wins.** This mirrors MuJoCo: a mesh with baked
  UV coordinates is mapped by those coordinates and *ignores* the material's `texrepeat` /
  `texuniform`; a mesh without them is auto-projected, and then `texrepeat` sets the tile scale
  (metres per tile when `texuniform`, tiles across the object otherwise). Getting this backwards
  projects a texture *atlas* by world position, which samples arbitrary regions of it — every
  surface ends up wearing some other surface's texture.
- **UVs are in MuJoCo's convention: `v` is measured from the image's top row.** three's
  `TextureLoader` defaults to `flipY = true` and would sample from the bottom, so image textures
  are loaded with `flipY = false`. `DataTexture` (the raw procedural textures packed into
  `scene.bin`) already defaults to `flipY = false`, so both paths agree.

`texture.repeat` is a property of the *Texture*, but the repeat a geom needs depends on the geom's
size — so a material shared across geoms of different size needs one Texture clone per distinct
repeat, not one mutated in place.

**Extractability rule: files in this directory import only `three` (and `three/addons`) — never
`@/…` or anything robovast-specific.** The directory is shared-candidate code for other projects
rendering the same descriptor, so keeping this boundary makes extraction into a common package
mechanical. Host-specific wiring (data providers, clocks, panel/config plumbing) belongs in the
consumer — here, `frontend/ui/src/panels/Scene3DPanel.tsx`.
