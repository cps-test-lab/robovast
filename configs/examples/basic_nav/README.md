# basic_nav

One nav2 navigation trial, run on **two simulators** — Gazebo and roqsim (MuJoCo) — so they can be
compared directly. A TurtleBot 4 navigates the nav2 **Depot** world to a per-configuration goal with
nav2 + AMCL. Same world, same map, same nav2 params, same ROS graph (down to the ground-truth
`turtlebot4_base_link_gt` frame), so roqsim is a drop-in replacement for Gazebo.

## Files

```
basic_nav_gazebo.vast  # one container: the scenario launches gz + TB4 + nav2 itself
basic_nav_roqsim.vast     # three containers: roqsim, a vanilla nav2 SUT, the scenario
scenario_gazebo.osc    # Gazebo bring-up + the measurement half
scenario_roqsim.osc       # nav2-only bring-up (remote, into the SUT) + the same measurement half
world/
  depot_nav2.yaml      # the roqsim world: Depot + TB4 + ROS bridge + ground-truth frame
files/                 # mounted at /config/files, shared by both halves
  nav2_params.yaml     #   nav2 planner/controller/AMCL/BT — pinned via params_file:=
  depot.yaml/.pgm      #   the nav2 Depot occupancy map — pinned via map:=
  nav2_bt.xml          #   behavior tree, for the nav2_bt_tree postprocessing and panel
  nav2_sut.launch.py   #   nav2 bring-up in the vanilla SUT container   (roqsim only)
  gazebo_tb4_launch.py #   gz + TB4 + nav2 bring-up                     (Gazebo only)
  depot_gt.sdf         #   ground-truth pose publisher                  (Gazebo only)
analysis/              # the three Results-explorer notebooks, shared by both halves
```

## Run it

```bash
# from the repo root, with the RoboVAST tooling available (`make venv`)
robovast validate configs/examples/basic_nav/basic_nav_roqsim.vast
robovast start    configs/examples/basic_nav/basic_nav_roqsim.vast     # MuJoCo backend
robovast start    configs/examples/basic_nav/basic_nav_gazebo.vast  # Gazebo backend
```

5 goals × 5 runs = 25 trials per half. Nothing is built first: roqsim runs from its own published
image, named by the `backend: roqsim` entry point. Campaign runs are headless on both backends —
the MuJoCo viewer is deliberately not wired in, because roqsim's viewer loop runs
`while viewer.is_running()`, so closing the window ends the simulator with exit code 0 and the run
looks clean while nav2 goes on planning against a dead sim.

## Why the goals match

Gazebo spawns the TB4 at world `(-8, 0)` and AMCL seeds at the map origin, so `map = world + (8, 0)`.
`world/depot_nav2.yaml` reproduces exactly this. The same map-frame goals (`x ≈ 21`) and the same
`turtlebot4_base_link_gt` convention (the world pose, labelled `map`; analysis shifts it `+8 m` in x)
therefore apply to both halves.

## What the Config tab shows

Both halves declare a `map2d` panel bound to `files/depot.yaml` — the very map they hand nav2 — with
`start` at the map origin and `goal` bound to the `goal_pose` parameter, so clicking through the five
configurations walks the goal marker down the map while everything else stays put. The markers are in
the **map** frame, which is why neither declares an `offset:` (a world-frame `scene3d` marker would
need `[-8, 0, 0]`).

2D and not 3D on purpose: `scene3d` keys its geometry on the simulator image, and an experiment `.vast`
names no image — the deployment's project resolves it per campaign, after `apply_backend`. The Config tab
has no campaign to resolve against, so it asks the file and is refused for want of an image, and the 3D
panel has nothing to show. The map needs nothing but the two checked-in files.

## What differs between the halves

Only the bring-up. The measurement half — `init_nav2`, `bag_record`, `nav_to_pose` — is identical in
both scenarios on purpose: if it drifts, a difference in results stops being attributable to the
simulator.

Gazebo bundles its simulator into the stack it launches, so one local `ros_launch` is the whole
bring-up, and the scenario gates nav2's **activation** (`autostart:=False`, then two lifecycle
`service_call`s). In the roqsim half the simulator is already running in its own container, so only nav2
is launched, `remote()`-modified into the SUT — and there the activation gate cannot work:
`service_call` fires `call_async` once with no `wait_for_service`, so across a container boundary the
request is sent before its client has finished endpoint matching, and is silently dropped. That half
gates the **launch** on the simulator's first `/clock` and `/scan` instead, and lets nav2 autostart
itself. OSC2 has no conditionals, so one file cannot express both.

Both gates are bounded (`timeout(60s)`), so a simulator that never comes up fails the trial rather
than hanging it.
