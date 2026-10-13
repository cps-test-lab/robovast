# UR5e pick-and-place: how accurate does the reported box pose have to be?

A UR5e with a Robotiq 2F-85 is bolted to a workbench. A box sits in front of it and an open bin
stands to one side. Each trial reads the box's pose from a simulated detector, plans with **MoveIt 2**,
picks the box up, carries it across and drops it in the bin — in roqsim (MuJoCo), over ROS 2.

The single factor is `object_detector.position_stddev`: the noise on the *reported* box pose. The
campaign asks how accurate perception has to be before the task stops working.

```bash
vast workspace run ur5e_pick_place ur5e_pick_place.vast --push configs/examples/ur5e_pick_place
```

4 noise levels × 5 repetitions = 20 trials.

## What it measures

The jaws open to about 87 mm around a 60 mm box, so there is roughly **13 mm of clearance per side**,
and that is the error budget for detection, IK and execution together. Success is expected at the
0 mm control and to fall as the noise grows: an example where every cell passes would not show
where the edge is. Five repetitions per level are there to separate failure *rates*, because the
outcome of one trial depends on the noise draw.

### Which axis of the error matters

**The 3D error magnitude does not order the runs.** The clearance bounds the error *across* the
jaws; error *along* the approach axis fails the task by other routes and at a different scale. So
the error is recorded per axis — `detect_error_y_m` across the jaws and `detect_error_z_m` along the
approach — and each produces a recognisable failure:

| signature | across-jaw | approach | what happened |
| --- | --- | --- | --- |
| `lift:box_did_not_rise` | large | any | the jaws miss sideways, or catch an edge and lose it |
| `close:jaws_closed_fully_empty` | small | large, upward | they shut *above* the box, missing it entirely |
| `descend:moveit_error_99999` | any | large, downward | the goal is below the bench, so MoveIt refuses to plan |

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

## What an arm cell has to get right

Each of these is commented at the line that handles it, and each is worth knowing before writing an
arm cell of your own.

- **MoveIt only knows the robot.** With an empty planning scene OMPL may route a joint the long way
  round and sweep the arm through the bench, while the bridge still reports the trajectory
  `SUCCEEDED`. The bench and bin are added as collision objects before the first plan.
- **Constrain the wrist roll, but not tightly.** Left free, the jaws can close across the box's
  diagonal, which is wider than the aperture; pinned too tightly, OMPL finds no solution.
- **Start facing the work.** The model's own home points the arm away from the box, so `spawn_arm.home`
  sets one that does, which spares every trial a long base swing and the planner failures it brings.
- **Neither bridge action means "arrived".** `FollowJointTrajectory` ends a trajectory on time, and
  `GripperCommand` infers a grasp from a stall, so every motion is verified against measured
  `/joint_states` instead.
- **Give the arm time to settle.** A residual joint error is magnified by the arm's reach, and an arm
  that has not settled spends the clearance budget before any noise is added — the sweep would then
  measure the arm's lag rather than the perception noise.
- **Attach the carried box from what you know.** Laterally that is the tool pose (kinematics), not the
  stale detection; vertically it is the bench plus half the box, because the tool's own height carries
  the arm's settle error. Either one wrong makes MoveIt refuse the lift with `START_STATE_INVALID`,
  which looks like a grasp failure.

The through-line: **score from ground truth, never from the stack's own verdict.** The verdict here
is the box's true pose on `/tf`.
