.. _run-capture:

===========================================
Replay artifacts: the scene and the capture
===========================================

The :ref:`run view <run-view>`'s 3D panel replays a run from **two artifacts**, and from nothing else —
no ROS, no rosbag, no postprocessing, no ``data.db``. Both are formats RoboVAST defines and a simulator
*may* produce; this page is the contract a producer implements against.

.. list-table::
   :header-rows: 1
   :widths: 12 40 20 28

   * - artifact
     - what it carries
     - when
     - who can produce it
   * - **scene**
     - geometry: a body tree with rest transforms, named joints, geoms, materials, textures, skins,
       an initial camera
     - static, per world
     - **any tool that can read the world** — it need not be the simulator that ran
   * - **capture**
     - motion: a time base plus named joint-value and body-pose tracks
     - per run
     - **only the simulator that ran**

That split is what lets a second simulator be admitted later. Geometry is world-authored, so it can be
compiled offline from an SDF, a USD, an MJCF or a floorplan by whatever tool reads that format; only the
motion is a property of the execution.

Producing them is an **optional capability**. A simulator that emits both gets a replay; one that does
not simply has no ``scene3d`` panel, exactly as a Gazebo campaign has none today. The dependency is
declared per campaign and fails by *absence* — which is visible — rather than through a fallback that
renders something misleading.

rst is the first producer of both: ``rst export web`` for the scene, ``rst export capture`` (or its
scenario adapter, at shutdown) for the capture. The reference readers are
``frontend/ui/src/lib/scene3d/sceneLoader.ts`` and ``frontend/ui/src/lib/scene3d/runCapture.ts``.

**When each is produced.** A capture is written by the run, because only the simulator that ran can
write it. A scene descriptor is *not* written by the run: it is a function of the world, so the service
compiles it on first view — inside the campaign's own pinned image — and caches it by world identity
(image digest + ``world`` + ``overrides``), shared across every run and every campaign that used that
world. That is why the capture's world identity is a contract and not decoration: it is the input to
obtaining geometry. See :ref:`its delivery section <scene-descriptor-delivery>`.

The scene descriptor
====================

``scene.json`` + ``scene.bin`` + one ``tex_<i>.png`` per image texture, in one directory: the loader
fetches the binary and the textures as **relative siblings** of ``scene.json``, which is why the file
address space preserves path segments.

The format is defined by its producer, ``rst/export_web.py``, and its full field list lives there. Two
notes matter to a *second* producer:

* It is a plain scene graph — bodies with rest transforms and a parent index, named joints carrying
  ``type``/``axis``/``pos``, geoms as primitives or indexed meshes — and nothing in it is MuJoCo-specific
  in substance.
* ``joints[].qposadr`` is **optional and legacy**. It names an index into MuJoCo's state vector, and the
  reference loader has never read it: animation is addressed entirely by joint *name*. A new producer
  should omit it.

.. _run-capture-format:

The run capture
===============

Two files in one directory, the same sibling convention:

.. code-block:: text

   <run>/capture/capture.json     manifest: time base + track index + provenance
   <run>/capture/capture.bin      float32/float64 tracks, addressed by byte offset

A capture is **per run** — it describes one execution — so it is always addressed in the run's file
space, never the campaign's.

The manifest
------------

.. code-block:: json

   {
    "format": "robovast.run_capture",
    "version": 1,
    "complete": true,
    "frame": "world",
    "time": {"base": "sim", "t0": 0.002, "t1": 29.324,
             "off": 0, "dtype": "f8", "samples": 734, "width": 1},
    "producer": "rst",
    "producer_version": "0.1.0",
    "world": "rst_tiago_pick:tiago_pick",
    "overrides": {},
    "packages": {"rst": "0.1.0", "mujoco": "3.11.0", "numpy": "2.5.1"},
    "seed": 1869948900,
    "tracks": [
     {"kind": "joint", "name": "arm_1_joint", "unit": "rad",
      "width": 1, "samples": 734, "dtype": "f4", "off": 5872},
     {"kind": "pose", "name": "base_footprint",
      "width": 7, "samples": 734, "dtype": "f4", "off": 114576}
    ]
   }

``format`` and ``version`` are required; a reader refuses an unknown format, and a *newer* version by
name rather than guessing at it. Guessing would render something plausible and wrong, which is the
failure this whole design exists to avoid.

``frame`` is the frame pose tracks are expressed in. It must match the scene's geometry — for rst that is
the simulator's **world** frame. This is not bookkeeping: a nav stack's ``base_link`` lives in a *map*
frame that can be metres from the world origin, and no reader can tell the two apart from the numbers.
In ``configs/examples/basic_nav`` the offset is 8 m, so getting it wrong draws the robot in the wrong
room.

``time.base`` is ``sim`` (simulated seconds from the run's start) or ``wall``. Declaring it is what lets a
capture-driven panel and a rosbag-driven panel be told whether they share a clock.

``world`` + ``overrides`` are the **world identity**, not decoration: together they name the world this
motion belongs to, precisely enough for a consumer to *obtain the matching geometry* — which is how the
run view gets a scene descriptor without the campaign declaring one. A producer must therefore write the
overrides it actually built with.

``overrides`` distinguishes three states, and conflating two of them renders confidently wrong geometry:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - value
     - meaning
   * - ``{"plugins": …}``
     - these overrides were applied; compile the world with them
   * - ``{}``
     - **none** were applied; compile the bare world
   * - *key absent*
     - **unknown** — this producer did not record them. A consumer must not read this as ``{}``: doing so
       compiles the unoverridden world for a run that varied it, and the picture looks perfectly fine.
       Generate from the bare world if you must, but say so.

``producer``, ``producer_version``, ``packages`` and ``seed`` are provenance. A reader shows ``world`` and
``producer`` when track names fail to resolve, because "this capture was recorded against a different
world" is the diagnosis in nearly every such case. ``packages`` carries the producer's own library
versions (for rst: ``rst``/``mujoco``/``numpy``), so a format or geometry mismatch across a version bump
is legible rather than mysterious. ``seed`` may be ``null`` when the producer had none.

Tracks
------

Two kinds, which are exactly the two things a scene graph can be driven by:

.. list-table::
   :header-rows: 1
   :widths: 10 12 78

   * - kind
     - width
     - payload
   * - ``joint``
     - 1
     - one scalar per sample, in the joint's own unit (``unit``: ``rad`` for a revolute joint, ``m`` for a
       prismatic one), keyed by the scene's **joint name**
   * - ``pose``
     - 7
     - one world-frame pose per sample — ``x y z`` then a ``w x y z`` quaternion — keyed by the scene's
       **body name**

Names are the entire addressing scheme: **no indices cross the interface.** A producer resolves names
from its own model, which is why ``qpos`` and ``qposadr`` cannot appear here — no other simulator has a
MuJoCo state vector. The test to apply to any future field is "could this be written from
``/joint_states`` and ``/tf``?"

Rules a producer must hold
--------------------------

**Emit a joint track for every joint whose value moves, and a pose track only for a body a reader could
not otherwise place.** A body is placeable when its parent is *and* its own joints are all named
revolute/prismatic ones *and* it is not a mocap body — recursively, from the world body down. So a link
welded to a moving parent, or hanging off it through joints, needs no pose track: the joint tracks and
the geometry's rest transforms already determine it. Emitting one anyway is not merely wasteful, it is
**two writers for one transform**, and which wins is then an accident of ordering. (rst's exporter got
this wrong first time by testing only whether a body *owns* a joint, which produced 36 pose tracks
instead of 2 on a real recording.)

**Order pose tracks parents-first.** A reader turns a world pose into a transform relative to the
parent, so the parent must already be seated within the same frame.

**Store each track sample-major** — all of sample 0's values, then sample 1's. This is what makes a time
window a contiguous byte range, so a reader can range-read the part it is showing rather than the whole
file. Nothing needs that yet (a 30 s capture of a mobile manipulator is ~150 KiB), but it is the property
that keeps windowing an implementation change rather than a format change.

**Align every offset to its dtype.** A browser's ``new Float64Array(buffer, off, n)`` *throws* unless
``off`` is a multiple of 8. A producer that aligned everything to 4 would work for most captures and fail
for some, which is the worst of both.

**Use float64 for the time track** (``dtype: "f8"``) and float32 for values. A float32 second degrades
with magnitude, and timestamps are exactly where that shows.

**Samples are in strictly increasing time order**, and a track sharing the manifest's time base has one
value per time sample.

Reading it
----------

A sample is addressed **by time, never by index**: nearest sample, ties to the earlier one, and
**never interpolated** — blending two states produces a pose the simulation never had. Values are pushed
into a sink rather than returned, so a 60 Hz redraw of a few dozen tracks allocates nothing.

A reader reports track names that resolve to no joint or body in the scene, with the manifest's ``world``
and ``producer``, rather than replaying a partly-static world in silence.

.. _run-capture-live:

Shaped for a live source
========================

The capture is defined as a **motion source** — a time base plus named tracks — of which these two files
are one *serialization*. A live source is another, so following a running simulation lands as a second
implementation of ``MotionSource`` (``frontend/ui/src/lib/scene3d/motionSource.ts``) with no change to the panel
and none to this format. Five rules exist for that, each cheap now and expensive to retrofit:

#. ``t1`` may be absent and ``complete`` may be ``false`` when the upper bound can still move. A consumer
   re-reads the range instead of caching it. A file is complete by construction.
#. **The track set may grow.** A live source gains a track when a robot spawns or a pedestrian appears. A
   file's never does — but a consumer that assumed otherwise would have to be rewritten rather than
   extended.
#. Samples are addressed by time, as above. For a live source "nearest" is simply "latest".
#. The timebase is declared, so a live source can be told whether it shares a clock with other panels.
#. Reads are **windowed** (``fetch(t0, t1)``), because a live source is inherently windowed. The file
   reader satisfies any window from the buffer it already has.

Every update reaches a consumer through ``subscribe``, and the file reader fires it once for data that is
already loaded. An interface method nothing calls is a guess; one the only shipping implementation uses
is a contract.

Deliberately **not** specified until something produces and consumes them, because inventing a format
for an absent consumer is how formats rot: array tracks (``width > 1``, for a lidar scan) and a media
index (for camera video). Both are additive — a reader skips a track ``kind`` it does not know, so an
older viewer keeps replaying the motion it does understand.

Producing a capture with rst
============================

Two ways, one implementation, so they cannot drift.

**During a campaign run.** The scenario adapter records while the run proceeds and derives the capture at
shutdown, against the still-live model — no world rebuild, and no GL backend in the run container:

.. code-block:: yaml

   execution:
     simulation: rst.scenario_adapter:MujocoSim
     env:
     - ROBOSITO_WORLD: "/config/files/depot.yaml"
     - ROBOSITO_RECORD: "run.npz"              # the recording (the run's ground truth)
     - ROBOSITO_CAPTURE_EXPORT_DIR: "capture"  # -> <run dir>/capture/capture.json
     # - ROBOSITO_CAPTURE_FPS: "25"            # optional; samples per *simulated* second

   visualization:
     panels:
     - scene3d:
         position: { fill: true }
         scene:   { path: _config/files/scene/scene.json, scope: campaign }
         capture: { path: capture/capture.json }

``vast config validate`` refuses a ``scene3d`` panel whose campaign never asks its runs to record: the
campaign would otherwise run, pass, and show a world that never moves.

**After the fact**, from a recording that already exists — which is how a different capture is produced
without re-running anything:

.. code-block:: bash

   rst export capture --state run.npz --out capture/

``run.npz`` keeps its own role either way: it is rst's ground truth, feeding ``rst render --state`` and
``rst state --state``, and everything else is re-derivable from it — the same relationship a ROS
campaign has with its rosbags.

.. note::

   Both artifacts are written on a **clean stop** — the scenario ending, the adapter's ``shutdown``, a
   closed viewer, one Ctrl+C, or a **SIGTERM**. A producer is expected to treat SIGTERM as a clean stop,
   because under a campaign it is the *usual* exit: a container teardown, an eviction and a per-run
   timeout all send it, and its default action would end the process with no flush at all. Only
   ``SIGKILL`` leaves neither artifact, and the panel says so instead of showing an empty scene.

Adding a producer
=================

For a simulator that is not rst:

#. **Geometry.** Either emit the scene descriptor directly, or compile it offline from the world the
   simulator ran — for an SDF world, ``rst scenes sdf-to-scene`` → ``scene-to-mjcf`` → ``rst export web``
   already does this, and a campaign can run it as an ``execution.generate`` step so the descriptor is a
   frozen campaign input with a freshness manifest (see :ref:`its delivery section <scene-descriptor-delivery>`).
#. **Motion.** Write ``capture.json`` + ``capture.bin`` per run, holding to the rules above. From ROS
   data that is ``/joint_states`` for the joint tracks and ``/tf`` for the pose tracks — with the caveat
   that the poses must be in the scene's frame, not a map frame.
#. **Declare it.** Add a ``scene3d`` panel with ``scene:`` and ``capture:`` paths. Nothing else in the
   campaign changes, and no panel code is involved.
