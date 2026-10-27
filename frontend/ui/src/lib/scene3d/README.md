# scene3d — the 3D scene viewer core

Renders the scene a simulator backend exports for the run view: one binary glTF file (`.glb`),
written for roqsim by `roqsim export gltf`. Any simulator that writes the same file renders here
too; what it carries beyond core glTF -- bodies and their joints, sensor cameras, the joint values
and the initial view -- is in `extras`, specified in `docs/simulators.rst` ("The scene"). The
scene's extras state `format: "roqsim.scene"` and a `version`; the loader refuses another format
or a version newer than `SCENE_VERSION`, naming both.

- `sceneLoader.ts` — the file → three.js `Group` (Z-up, the world's frame), parsed by three's
  `GLTFLoader` with the meshopt decoder (both fetched with the first scene), plus an imperative
  animation API: `jointMap[name](value)` for hinge/slide joints and `basePose(body, pos, quat)` for
  world-frame body poses.
- `sceneCameras.ts` — the scene's sensor cameras: glTF perspective cameras on
  their bodies (`docs/simulators.rst`, "Camera sensors"), each built into a three
  `PerspectiveCamera` parented to its body's node; `viewport.ts`'s `setViewpoint` renders through
  one, letterboxed to the sensor's shape. `parseFreeView`/`buildFreeCamera` make the same kind of
  camera from a position and a look-at point, in the world's frame or a body's. Used by the camera
  panel's `kind: scene` only; the `scene3d` panel stays on the orbit camera.
- `motionSource.ts` — what a run's *motion* looks like to a viewer: a time base plus named joint
  and pose tracks, pushed into the scene model as the sink. `rowMotion.ts` is the source over a
  run's `sim_poses` / `joint_states` rows, windowed around the clock and paged at the query's row
  cap, following a run still recording through a reader the panel adapts its data provider to.
- `sceneModelCache.ts` — parsed models leased per viewer mount and parked on release, keyed by the
  content-addressed scene URL, so a remount on the same world skips the fetch and the rebuild.
- `sceneTf.ts` — TF-chain composition (`map -> odom -> base_link`) for data sources that deliver
  raw `/tf` transforms; unused by the playback path (poses arrive already map-frame), kept for the
  future live view.
- `viewport.ts` — a plain-three viewport (renderer, camera, lights, grid, orbit controls, its own
  wheel/pointer handlers, a frustum that tracks the camera, Z-up wrapper group, resize, dispose).
  `panOffset` there is how far a two-finger pan moves the camera.
- `touch.ts` — fingers to gestures: tap, double tap, one-finger drag, and for two fingers a pinch, a
  pan and a twist. A pinch and a twist act only past a dead zone, the pan follows the midpoint
  through a low-pass filter, and the finger left when one of two lifts starts nothing.
- `touchNavigation.ts` — what each gesture moves, against the few operations a viewport offers
  (`TouchView`): a pinch is the wheel's flight toward the fingers' midpoint, two fingers pan and
  twist, a double tap frames what was tapped, and one finger is the orbit controls' rotate, which it
  switches off while a second finger is down.
- `pivot.ts` — where the orbit pivot goes, and the reason the other two files are shaped as they are.
  Rotate, pan and the wheel are all scaled by the pivot distance, so it is measured against the scene
  under the cursor at the start of every gesture. A pivot left at the world's authored framing
  distance is right in the opening frame and wrong everywhere the camera travels to.
- `cursorDolly.ts` — the wheel: fly along the ray under the cursor, stepping a fraction of the pivot
  distance and leaving the pivot behind, so the approach slows as it closes on the surface aimed at.
  That is radius-scaling, which this file was originally written to avoid; the docstring says at
  length why the objection (a series converging on a pivot that can never be passed) does not apply
  to a pivot re-chosen every gesture with a floored step.

**Extractability rule: files in this directory import only `three` (and `three/addons`) — never
`@/…` or anything robovast-specific.** The directory is shared-candidate code for other projects
rendering the same scene, so keeping this boundary makes extraction into a common package
mechanical. Host-specific wiring (data providers, clocks, panel/config plumbing, the palette the
viewport and the markers are drawn in, and `useSceneGeometry`, the hook asking the service for a
scene) belongs in the consumer — here, the two `Scene3DPanel.tsx` under `frontend/ui/src/panels/`
and `frontend/ui/src/lib/useSceneGeometry.ts`. `imports.test.ts` holds the directory to the rule.
