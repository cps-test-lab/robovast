# PX4 offboard flight envelope

A **Holybro X500 V2 class quadrotor**, flown by **PX4** from ROS 2 offboard setpoints, flies a
10 × 10 m square in a 20 × 20 m room while the campaign eats its thrust margin.

This is [`../drone_flight_envelope/`](../drone_flight_envelope/) asked of a real flight stack. The
physical question is identical — does the aircraft hold its commanded course as its thrust margin
disappears? — and everything above the airframe is different. There is no controller of ours
anywhere in the loop:

```text
scenario (px4_msgs offboard setpoints)
    --uXRCE-DDS, UDP 8888-->  PX4 SITL
    --MAVLink HIL, TCP 4560--> roqsim (MuJoCo physics)
```

EKF2, the position and attitude controllers, the control allocator, the arming checks and the
failsafes are all PX4's, running the code an operator flashes onto a vehicle. That is what this
example is for, and it is why it is kept alongside the Crazyflie one rather than replacing it.

```bash
# look at the world, no campaign and no PX4 involved (the aircraft will sit on the pad --
# nothing commands the rotors without a flight stack attached, which is the point)
roqsim sim world/px4_envelope.yaml

# the factorial campaign: 12 configurations x 3 runs
vast config validate px4_envelope.vast
vast workspace run px4-offboard-envelope px4_envelope.vast
```

> **This example is the first user of `ros_packages:`.** Neither `px4_msgs` nor the Micro XRCE-DDS
> Agent is available as a Debian package for any ROS 2 distribution (checked against
> `ros/rosdistro` `jazzy/distribution.yaml`, 2026-08-31: `px4_msgs` has a `source:` entry and no
> `release:`; `microxrcedds_agent` is absent from the distribution entirely), and neither is on
> PyPI — so `system_packages` (apt) and `python_packages` (pip) could not express what a ROS 2 PX4
> setup needs at all. That was a gap in the framework rather than a property of this example, and
> the fix is a *generic* one: a container may now declare `ros_packages:`, pinned git sources that
> are colcon-built into an overlay in the campaign's own derived image. The drone stack therefore
> lives in the campaign that flies drones, not in a base image every campaign pulls.
>
> Worth knowing, because it is the non-obvious half: the DDS agent is a plain CMake project with no
> `package.xml`, and `ros_packages:` still covers it. colcon identifies a CMake project by its
> `CMakeLists.txt`, and the agent ships a `colcon.pkg` declaring its name, its `cmake` type, and
> the cmake arguments (`UAGENT_USE_SYSTEM_FASTDDS`, `UAGENT_USE_SYSTEM_FASTCDR`) that make it link
> the Fast DDS the ROS distribution already ships. Both packages build in one workspace from one
> key. An image built without them fails at `import px4_msgs` in `files/offboard_stream.py`, with
> the cause named.
>
> The rule is applied to the simulator side too, where it is the easy case: the `simulation`
> container declares `python_packages: [pymavlink]` rather than the `robovast-roqsim` image
> carrying it. `roqsim_aerial` makes MAVLink an optional extra (`roqsim_aerial[px4]`) so that a
> world flying the in-process `quadrotor_controller` does not pull a MAVLink stack in, and an image
> installing it unconditionally would undo that decision one layer up. **Nothing PX4-specific is in
> any shared image**; the whole flight stack is declared by the campaign that needs it.

## Why the thrust margin is still the experiment

PX4 hovers this airframe at **60 % throttle** (`MPC_THR_HOVER 0.6`), so the four rotors together
deliver `2.0 kg × 9.80665 / 0.6 = 32.7 N` against a 19.6 N airframe — an unloaded thrust-to-weight
ratio of **1.67**. Payload walks it to the boundary:

| payload | total mass | weight | T/W |
| --- | --- | --- | --- |
| 0.0 kg | 2.0 kg | 19.613 N | **1.667** |
| 0.6 kg | 2.6 kg | 25.497 N | **1.283** |
| 1.2 kg | 3.2 kg | 31.381 N | **1.042** |

T/W reaches exactly 1 at a total mass of 3.334 kg, so the top level sits just above the cliff
rather than comfortably inside the envelope: a grid can only bracket a boundary between two cells,
and this bracket is a narrow one.

1.67 is worth a second look, because it is nothing like the 3-plus a datasheet for X500-class
motors suggests. It is PX4's own hover throttle that sets the scale: the airframe file says the
aircraft hovers at 60 % command, so full command is `1/0.6` of a hover, and 1.67 is the margin *the
flight stack believes it has*. Numbers like this are the reason to fly a real stack — the belief is
what gets flown, not the datasheet.

The same three factors eat it, and they are still the three you cannot test reproducibly outdoors:

| factor | where it is written | levels |
| --- | --- | --- |
| payload mass | `components.drone.payload.mass` | 0.0 / 0.6 / 1.2 kg |
| air density | `sim.density` | 1.225 (sea level) / 0.905 (~3000 m) |
| wind | `components.wind_field.steady` | calm / 4 m/s from the east |

The wind level is higher than the Crazyflie example's 2.5 m/s for a plain reason: a 2 kg airframe
under PX4's position controller shrugs that off, and a factor with no effect is not a factor.

## What PX4's default simulator provides, and what roqsim must therefore supply

This is the analysis that decides how a non-Gazebo simulator can carry PX4 at all, so it is worth
setting out rather than assuming.

**gz-sim is PX4's default, and its bridge is not reusable.** PX4 drives Gazebo (gz Harmonic)
through a *native* `gz_bridge` module speaking gz-transport — PX4-internal code against gz's own
IPC. Nothing that is not gz can attach through it. **Every other simulator PX4 supports** —
jMAVSim, Gazebo Classic, AirSim, FlightGear, JSBSim — attaches through the documented,
simulator-agnostic **Simulator MAVLink API** instead, and that is the interface roqsim implements:

- The **simulator listens on TCP 4560**; PX4 connects out to it. (Port is `4560 + instance`.)
- **Simulator → PX4:** `HIL_SENSOR` (accelerometer m/s², gyro rad/s, magnetometer gauss, barometric
  pressure hPa, temperature °C, with a `fields_updated` bitmask), `HIL_GPS`, and
  `HIL_STATE_QUATERNION` (ground truth, for logging and for PX4's own diagnostics).
- **PX4 → simulator:** `HIL_ACTUATOR_CONTROLS` — up to 16 normalized outputs plus an armed flag.
  For a quad-X, outputs 0..3 are the four rotors, normalized 0..1.
- **Lockstep.** PX4 blocks until the next `HIL_SENSOR` arrives; the simulator blocks until
  `HIL_ACTUATOR_CONTROLS` arrives. That is what makes a run deterministic and lets it go faster or
  slower than realtime without the flight changing — and it is what roqsim's stepping loop has to
  respect rather than free-run.

**So the sensor set is the contract.** PX4's EKF2 does not estimate from poses; it estimates from
an IMU, a barometer, a magnetometer and a GNSS fix. roqsim supplies all four: the IMU from the
model's own MJCF sensors, the barometer and magnetometer synthesised from world state inside the
`px4_sitl` plugin (they have no other consumer, so they live with the bridge rather than as
plugins of their own), and the fix from a separate `gnss` plugin — separate because a datum is a
property of the *world*, not of the flight stack, and because GNSS has real independent uses
(outdoor experiments, GNSS-denial studies).

**And the airframe has to expose per-rotor actuators.** `HIL_ACTUATOR_CONTROLS` is four normalized
motor commands. The Crazyflie model in the other example exposes collective thrust plus three body
moments — the abstraction a Python controller wants, and one that cannot accept those four numbers
at all. Hence the new `x500` model with four independent force actuators and the
`multirotor_motors` plugin that turns normalized commands into rotor forces plus each rotor's yaw
reaction torque. That plugin is roqsim's counterpart of gz's `MulticopterMotorModel`, and it is
what makes the airframe drivable by *any* flight stack instead of only by a controller of ours.

### A finding: PX4's default airframe and PX4's portable interface do not meet

PX4's default SITL airframe is `4001_gz_x500`. Its first act is `param set-default SIM_GZ_EN 1`,
and `ROMFS/px4fmu_common/init.d-posix/px4-rc.simulator` branches to the gz bridge as soon as
`SIM_GZ_EN` is 1 — before it can ever reach `px4-rc.mavlinksim`, the simulator-agnostic path. The
only airframe in the tree that reaches that path is `10016_none_iris`, an Iris.

So the default simulator's airframes are welded to the default simulator's transport: PX4's
*default airframe* and PX4's *portable interface* are not available together out of the box. That
is a real, citable statement about how portable a PX4 setup actually is, and surfacing it is one of
the things this example is worth having for.

The workaround is small and is not a patch: [`files/10020_none_x500`](files/10020_none_x500) is
`10016_none_iris` with the x500's control-allocation geometry from `4001_gz_x500` substituted, and
PX4 resolves airframes by name from `${R}etc/init.d-posix/airframes` at run time, so dropping a
file in beside the shipped ones is the documented extension point.
[`files/px4_start.sh`](files/px4_start.sh) copies it there and starts PX4 with
`PX4_SIM_MODEL=none_x500`.

### What gz gives that we do not

Named honestly, because a comparison that only lists our advantages is not one:

- **Sensor breadth.** gz-sim ships camera, depth, lidar, optical-flow and airspeed models wired
  into PX4 through the same native bridge. This example supplies the four sensors EKF2 needs and
  nothing more; a vision-guided PX4 experiment is not expressible here today.
- **Rotor aerodynamics.** gz's `MulticopterMotorModel` models rotor drag, rolling moment and
  ground effect. `multirotor_motors` models thrust, first-order motor lag and yaw reaction torque.
  The difference shows up close to surfaces and at speed.
- **Worlds and airframe library.** PX4 ships a maintained set of gz worlds and gz airframes, all of
  which work on day one. Everything here is authored.
- **It is the supported path.** A bug in the gz bridge is a PX4 bug someone else fixes. A bug in
  `px4_sitl` is ours.

What we get in exchange is what the rest of RoboVAST is for: one substrate for ground, arm and
aerial experiments, campaign-level variation of physics parameters (`sim.density`, payload mass,
wind field) that gz exposes far less directly, MuJoCo's determinism, and a simulator that is
already wired into the campaign runner, the bag capture and the run view.

## Compared with `drone_flight_envelope`

Both are kept. They test different things and neither subsumes the other.

| | `drone_flight_envelope` | `px4_offboard_envelope` (this one) |
| --- | --- | --- |
| airframe | Crazyflie 2, 27 g, 0.1 m | X500 V2 class, 2.0 kg, 0.5 m |
| actuation | collective thrust + 3 body moments | four per-rotor forces |
| what stabilises it | `quadrotor_controller`, a plugin **inside** the simulator | **PX4**, in its own container |
| what is under test | the framework's aerial plumbing: models, wind, payload, variation, panels | a real flight stack: EKF2, MPC, control allocation, arming, failsafes |
| command interface | `geometry_msgs/PoseStamped` on a roqsim-internal topic | `px4_msgs` offboard setpoints over uXRCE-DDS |
| frame | ENU, altitude positive up | **NED, altitude negative down** |
| position measured from | simulator ground truth | PX4's estimate, with ground truth kept alongside |
| unloaded T/W | 1.32 | 1.67 |
| containers | 2 (simulation, scenario) | 3 (simulation, px4, scenario) |
| runs today | yes | yes, once the derived image with this campaign's `ros_packages:` is built |
| cost | seconds of bringup | PX4 bringup + EKF2 convergence before anything arms |

The short version: the Crazyflie example is the one to copy when you want to test *the framework*
against an aerial problem, and it stays the cheap one. This one is the one to copy when the flight
software is the subject.

## What is in here

```text
px4_envelope.vast              factorial grid, 3 x 2 x 2 = 12 configurations   <- start here
scenario.osc                   the trial: gate on PX4, arm, offboard, fly, land, disarm
world/px4_envelope.yaml        the roqsim world -- airframe, motors, GNSS, HIL bridge, weather
world/flight_room.xml          the room itself: 20 x 20 m, floor, walls, lighting
files/px4_start.sh             the px4 container's command: install airframe, start SITL
files/10020_none_x500          the airframe PX4 does not ship (see the finding above)
files/offboard_stream.py       the 20 Hz OffboardControlMode + TrajectorySetpoint stream
files/metrics.py               odometry + PX4 estimate -> trajectory.csv + metrics.csv
```

## Three things worth knowing before you copy it

**NED. Altitude is negative.** `px4_msgs/TrajectorySetpoint.position` is [north, east, **down**], so
3 m up is `z = -3.0`. This is the single most common mistake when moving from a roqsim-internal ENU
pose topic to PX4, and it does not produce a mediocre flight — it commands the aircraft into the
floor under full thrust, and every cell of the campaign reads as a crash. The conversion happens in
exactly two places in this example and nowhere else: the scenario writes NED, and `files/metrics.py`
converts back to ENU on the way in. `files/offboard_stream.py` deliberately does *no* conversion —
a frame flip hidden inside a relay is how a sign error survives review.

**Offboard is a streaming protocol, and scenario-execution has no action that streams.** PX4 drops
out of Offboard if `OffboardControlMode` stops for more than about half a second, and it refuses to
*enter* Offboard unless a setpoint stream is already running. The available ROS actions
(`scenario_execution_ros/lib_osc/ros.osc`) are `topic_publish`, `topic_monitor`, `service_call`,
`action_call`, the `wait_for_*` family, `bag_record`, `ros_launch` and `ros_run` — none of them
publishes at a rate, and the only repetition in the language is `osc.helpers`' `repeat` modifier,
whose period is whatever the behaviour tree costs that tick rather than a control loop's. So every
px4_msgs message this example sends — the two streams *and* the four `VehicleCommand`s for mode,
arm, land and disarm — is formed in `files/offboard_stream.py`, started with `run_process`, and
`scenario.osc` is the sequencer: it publishes a phase word and a course leg and waits.

The commands followed the streams into the node for a sharper reason than tidiness. `VehicleCommand`
carries a `timestamp` in microseconds, and a `topic_publish` cannot ask the ROS clock — it can only
send a literal. A literal timestamp is either wrong or a bet that PX4 ignores the field, and in
*this* campaign that bet has the worst possible failure mode: if PX4 rejects a stale command the
aircraft never arms, which is precisely what a run at T/W ≤ 1 looks like. The campaign would have
reported the result it was built to measure, produced by a bug. One component owning the protocol
removes the question. The cost is that the MAVLink command ids are no longer literally in the
scenario; they are in the node's `PHASES` table, one line each, with the same citation.

**PX4 renames its own ROS 2 topics between releases.** `dds_topics.yaml` has always said
`/fmu/out/vehicle_local_position`; the `_vN` suffix is appended *at run time* by the uXRCE-DDS
client from each message's `MESSAGE_VERSION` constant, and message versioning was introduced in
PX4 v1.16. So the topic name is a property of the release: no suffix at v1.15 and earlier,
`vehicle_local_position` still unsuffixed at v1.16, `vehicle_local_position_v1` on `main`.
`VehicleStatus` moves faster still (`_v1` at v1.16, `_v4` on `main`), which is why the scenario
gates on local position instead. This is why the PX4 image is pinned to a concrete tag and not
`latest`, and why the topic name appears in exactly three files that must change together:
`scenario.osc` (the gate and the bag), `px4_envelope.vast` (`rosbags_to_csv`), and `files/metrics.py`
(the glob). Verified against a running stack for the pinned image (v1.18.0-beta2): local
position is `_v1`, but `vehicle_status` is already `_v4` — the two version independently, so
`_v1` is not a safe default for any other topic. Re-check after any image bump with
`ros2 topic list | grep fmu/`.

## Metrics

`files/metrics.py` writes `trajectory.csv` (what the panels bind to) and `metrics.csv` (one row of
scalars) per run. The change from the Crazyflie example is that there are now **two** position
sources, and the primary one is PX4's estimate:

- `/fmu/out/vehicle_local_position_v1` — what EKF2 believed, and therefore what PX4 actually flew
  on. The tracking metrics are computed against it, because that is what a real flight test
  measures: a field test has no ground truth, only an autopilot log.
- `/drone/odom` — ground truth from the roqsim bridge, which the field cannot have. Kept because
  the *difference* between the two is a first-class observable that did not exist in the other
  example: with a controller reading ground truth, belief and truth were the same object.

The outcome label stays three-valued in spirit and gains a fourth, because a real flight stack has
one more way to fail than a PD loop does:

| outcome | meaning |
| --- | --- |
| `never_armed` | no flight at all — PX4 refused to arm, or T/W ≤ 1 and it could not lift itself |
| `estimator_diverged` | airborne, but EKF2's belief drifted from reality; checked first, because a diverged estimate makes every tracking number below it meaningless |
| `sagged` | airborne and correctly estimated, but below the commanded altitude — out of thrust margin |
| `held` | flew the course within tolerance |

Collapsing these into pass/fail would throw the result away. `never_armed` and `sagged` are the
same two physics the Crazyflie example separated; `estimator_diverged` is new and is the one that
only a real stack can produce — PX4 flying a perfect course against a position that is wrong.

Note the honest limitation in `never_armed`: "PX4 declined to arm" and "the aircraft could not lift
itself" land in the same bucket, because both produce a trajectory that never leaves the pad. The
run log separates them (PX4 says why it refused); the metrics cannot.

## Seeing what the parameters did

The roqsim `scene3d` panel is added automatically and is the centrepiece. The panels declared in
the `.vast` put the numbers driving that picture beside it, and one of them is new: **truth vs PX4
estimate** on one altitude axis. A run where those two diverge is a run whose tracking error is an
*estimation* problem rather than a control or thrust one — and telling those apart is exactly what
a campaign against a real flight stack can do and one against our own controller cannot.

The data browser carries the same cross-run envelope plot as the other example, plus estimator
error against thrust margin: if the estimate degrades with payload, the stack is not merely running
out of thrust, its state estimate is decaying with the attitude excursions that running out of
thrust produces.

## What has actually been verified

Stated precisely, because "it validates" and "it flies" are very different claims:

| claim | how |
| --- | --- |
| Real PX4 flies the roqsim bridge | PX4 v1.18.0-beta2 against the `x500` world: `Simulator connected on TCP port 4560`, EKF2 reached attitude + local + global position, `commander takeoff` armed, the aircraft climbed and held a hover with all four rotor commands at 0.60 = `MPC_THR_HOVER` |
| The `/fmu` topic names | PX4 + the DDS agent + `ros2 topic list` run together: `vehicle_local_position_v1`, `vehicle_status_v4`, and unsuffixed `/fmu/in/*` |
| The PX4 invocation in `files/px4_start.sh` | `px4 --help` on the pinned image plus a real run; the rootfs is the ROMFS **root**, `-d` takes no argument, and `${ROOT}/bin` must be on `PATH` or `rcS` dies sourcing `px4-alias.sh` |
| `python3` missing from the PX4 image | probed directly in the pinned tag |
| Both PX4 packages colcon-build in one workspace | built on `ros:jazzy`: `px4_msgs` (260 interfaces) as an ament\_cmake package and `microxrcedds_agent` as a `cmake` package, from the two pinned refs this `.vast` declares; `MicroXRCEAgent --help` runs from the resulting overlay |
| The agent's Fast DDS constraint | agent 3.x configures against `fastdds` 3.x and fails on this distribution, which ships Fast DDS 2.14 exporting `fastrtps`; the pinned v2.4.3 is the 2.x-compatible line |
| The `.vast` itself | `vast config validate` → 12 configurations, 36 runs |

**Not verified: the campaign has never been executed, and neither has its derived image build.**
The two packages were built from these refs directly with colcon; they have *not* been built
through RoboVAST's `ros_packages:` image build, and none of this has been run together as a
campaign, which also needs a `robovast-roqsim` image carrying the new roqsim. Until that happens
this example is authored and component-verified, not a result.

## What is still open

- **The prebuilt PX4 SITL images are pre-release tags only.** There is no stable-release prebuilt
  SITL image, so `px4io/px4-sitl:v1.18.0-beta2` is pinned in full knowledge that it is a beta —
  and `px4_msgs` is pinned to its `release/1.18` branch to match, because interfaces from the wrong
  branch produce topic names that do not match the running autopilot.
- **eProsima publishes no Micro XRCE-DDS agent image any more.** The one its README links
  (`hub.docker.com/r/eprosima/micro-xrce-dds-agent`) is 404 as of 2026-08-31, so the agent cannot
  be a container of its own the way PX4 is; it is built from source by `ros_packages:` and started
  inside the scenario container, which is where the ROS 2 graph is anyway. Naming an unofficial
  mirror in a public example would be worse.
- **The agent pin is tied to the base ROS distribution's Fast DDS.** v2.4.3 is pinned because agent
  3.x requires Fast DDS 3.x while this distribution ships 2.14. Bumping the ROS distribution is
  what should move that pin, not a desire for a newer agent.
- **`px4io/px4-sitl` does not carry `python3`** — measured against the pinned tag: it has `stdbuf`
  and `tee` but not `python3`, and RoboVAST's `secondary_entrypoint.sh` needs all three. The `px4`
  container therefore declares `system_packages: [python3]`, which triggers a derived image build
  on top of the pinned tag.
