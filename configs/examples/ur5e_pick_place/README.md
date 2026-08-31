# UR5e pick-and-place: how accurate does the reported box pose have to be?

A UR5e with a Robotiq 2F-85 is bolted to a workbench. A box sits in front of it and an open bin
stands to one side. Each trial reads the box's pose from a simulated detector, plans with **MoveIt 2**,
picks the box up, carries it across and drops it in the bin — in roqsim (MuJoCo), over ROS 2.

The single factor is `object_detector.position_stddev`: the noise on the *reported* box pose. The
campaign asks how accurate perception has to be before the task stops working.

```bash
vast run configs/examples/ur5e_pick_place/ur5e_pick_place.vast
```

4 noise levels × 5 repetitions = 20 trials, about two and a half minutes on an idle cluster lane.

## What it measures

The jaws open to about 87 mm around a 60 mm box, so there is roughly **13 mm of clearance per side**,
and that is the error budget for detection, IK and execution together.

Measured, pooled over 45 runs (three identical sweeps, 15 per level):

| `position_stddev` | success | typical failure |
| --- | --- | --- |
| 0 mm  | 15/15 (100%) | — |
| 8 mm  | 11/15 (73%)  | the grasp slips |
| 16 mm | 8/15 (53%)   | the grasp slips |
| 28 mm | 2/15 (13%)   | the jaws miss, close on nothing, or the goal cannot be planned |

The decline is the result. An example where every cell passes would not show you where the edge is.

### Which axis of the error matters

**The 3D error magnitude does not order the runs**, and that is the most transferable finding here.
A run with 53.2 mm of total error succeeded; one with 20.2 mm failed. The axis is what matters, so
the error is recorded per axis — `detect_error_y_m` across the jaws and `detect_error_z_m` along the
approach — and each produces a different, recognisable failure:

| signature | across-jaw | approach | what happened |
| --- | --- | --- | --- |
| `lift:box_did_not_rise` | large | any | the jaws miss sideways, or catch an edge and lose it |
| `close:jaws_closed_fully_empty` | **0.5 mm** | **19.7 mm** | they shut *above* the box, missing it entirely |
| `descend:moveit_error_99999` | any | **36-48 mm** | the goal is below the bench, so MoveIt refuses to plan |

The three largest approach-axis errors in the last sweep were all planner refusals: the robot believes
the box is inside the table, and no plan reaches there. Meanwhile a 20.1 mm approach error succeeded
outright when the lateral error was small. So the ~13 mm clearance bounds the **lateral** miss
specifically; approach-axis error fails the task by different routes and at a different scale.

## Reading `out.csv`

One row per run. `success` is `placed`; read it together with `failure`, whose `<phase>:<reason>` names
where the trial died — half the diagnosis.

| column | what it says |
| --- | --- |
| `picked`, `placed` | recorded apart: gripping cleanly then mis-dropping is a different result from never gripping |
| `detect_error_m` | the realised \|detected − true\|, so the *nominal* stddev becomes the error this grasp actually had to absorb |
| `detect_error_y_m` | the part across the jaws, which the ~13 mm clearance bounds |
| `detect_error_z_m` | the part along the approach axis, which fails the task by other routes |
| `max_rise_m` | latched **during** the lift; read at the end it could not tell "never picked" from "picked then dropped" |
| `grip_closed_to` | where the jaws stopped. Reaching the commanded 0.8 means they closed on **nothing** |
| `worst_arm_residual_rad` | joint-space, in radians: how far the arm still was from its plan. The part of the budget that is *not* perception |
| `failure` | the phase that failed, empty on success |

A failed trial is this campaign's **result, not its error**: the trial always exits 0, because at the
wider noise levels failing is the measurement.

## How it is put together

```text
ur5e_pick_place.vast      the campaign: the sweep, three containers, the generate step
scenario.osc              the ORDER: clock -> stack -> record -> trial -> end
world/ur5e_pick_place.yaml   the cell; world/drop_bin.xml is its own prop
moveit/moveit_ur5e.launch.py move_group + robot_state_publisher
moveit/gen/               GENERATED, git-ignored: everything move_group loads
files/pick_place.py       the trial: phases, and the measurements behind the verdict
files/planning_scene.py   what MoveIt is allowed to know about the bench, bin and carried box
files/grasp_goal.py       the one motion goal: put the grasp point HERE, pointing down
```

**Nothing generated is committed.** `roqsim export moveit` derives the URDF, its meshes, the SRDF and
the four YAMLs from *the world the simulator loads*, as an `execution.generate` step, so the robot
MoveIt plans against cannot drift from the robot being simulated — `--check` fails the build when they
disagree, and the SRDF's home state is read back out of the compiled model.

The factor is applied as a `sim:` override (`components.ur5e.object_detector.position_stddev`), a
property of the world, so it needs no scenario parameter and no `.osc` plumbing. Its cells therefore
show **no parameters** in `preview_configurations`, which is correct.

## Things this cell learned the hard way

Each of these is a real failure that the campaign produced, and each is commented at the line that
now prevents it. They are worth knowing before writing an arm cell of your own.

- **MoveIt only knows the robot.** With an empty planning scene, OMPL routed `shoulder_lift` the long
  way round — the wraparound — and swept the arm straight through the bench. MuJoCo stopped it, the
  servos saturated 5.59 rad from the commanded vector, and the bridge still reported the trajectory
  `SUCCEEDED`. The bench and bin are added as collision objects before the first plan.
- **A free wrist roll changes which axis you grasp.** With the roll unconstrained the jaws closed
  across the box's 100 mm diagonal against an 87 mm aperture and squirted it out sideways, 40 mm in
  0.3 s, then shut on air. Pinned too tightly (0.05 rad), OMPL failed to solve at all.
- **Start facing the work.** The model's own home points the arm away from the box, so every trial
  opened with a 159° base swing and a 41 s approach. Setting `spawn_arm.home` cut that out and removed
  the planner failures with it.
- **Neither bridge action means "arrived".** `FollowJointTrajectory` ends a trajectory on time, and
  `GripperCommand` infers a grasp from a stall — the jaws here began moving *after* a 1.5 s pause had
  already expired, so a fixed wait read them open and recorded that as the grasp. Every motion is
  verified against measured `/joint_states` instead.
- **Give the arm time to settle.** At a 4 s settle the arm still had 0.037 rad on a joint — about
  17 mm at this radius, more than the whole clearance budget, before any noise was added. A sweep run
  like that varies `position_stddev` while the arm's own lag decides the outcome.
- **Attach the carried box from what you know.** Laterally that is the tool pose (kinematics), not the
  stale detection; vertically it is the bench plus half the box, because the tool's own height carries
  the arm's settle error. Getting either wrong made MoveIt refuse the lift with
  `START_STATE_INVALID` and looked like a grasp failure.

The through-line: **score from ground truth, never from the stack's own verdict.** Every one of these
would have reported success. The verdict here is the box's true pose on `/tf`.
