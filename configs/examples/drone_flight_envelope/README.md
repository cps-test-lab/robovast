# Drone flight envelope

A Bitcraze **Crazyflie 2** flies a square course in a 6 × 6 m room while the campaign eats its
thrust margin.

This is RoboVAST's aerial example. It exists to show that the framework applies to drones as
directly as it does to ground robots — same substrate, same `.vast`, same variation plugins — and to
be a template worth copying for an aerial campaign of your own.

> Not to be confused with [`../quadrotor_landing/`](../quadrotor_landing/), which also has a
> quadrotor in it. That one is a *search* example whose SUT is a numpy point mass, kept because it
> runs in milliseconds and needs no simulator. This one is a real airframe in MuJoCo, flown over
> ROS 2, and needs a simulator image.

```bash
# look at the world, no campaign involved
roqsim sim world/drone_envelope.yaml

# the factorial campaign: 12 configurations x 3 runs
vast config validate drone_envelope.vast
vast workspace run drone-envelope drone_envelope.vast
```

## Why the thrust margin is the experiment

The Crazyflie's MJCF gives it 0.35 N of collective thrust against a 27 g airframe — a
thrust-to-weight ratio of **1.32**. That number, rather than the controller, is what an aerial
campaign is usually about, and three things eat it. They are also the three things you cannot test
reproducibly outdoors, which is the argument for simulating them at all:

| factor | where it is written | what it does |
| --- | --- | --- |
| payload mass | `components.drone.payload.mass` | 6 g takes T/W from 1.32 to 1.08 |
| air density | `sim.density` | density altitude: thinner air is less thrust *and* less damping |
| wind | `components.wind_field.*` | steady flow, gusts, Dryden turbulence |

Measured against a 1 m altitude hold, the boundary sits where physics says it does:

| payload | T/W | mean altitude error | outcome |
| --- | --- | --- | --- |
| 0 g | 1.32 | 0.000 m | `held` |
| 4 g | 1.15 | 0.121 m | `held` |
| 8 g | 1.02 | 0.243 m | `sagged` |

The graded sag before the collapse is real: `quadrotor_controller` has no integral term, so added
weight buys steady-state altitude error rather than a cliff. The cliff arrives at T/W = 1, and a
grid can only bracket it between two cells: the top payload level is placed just above it rather
than comfortably inside the envelope so that the bracket is a narrow one.

## What is in here

```text
drone_envelope.vast            factorial grid, 3 x 2 x 2 = 12 configurations   <- start here
scenario.osc                   the trial: take off, fly a square, land on the pad
world/drone_envelope.yaml      the roqsim world -- drone, weather, props, camera
world/room.xml                 the room itself: floor, walls, lighting
variations/wind.py             (speed, heading, turbulence) -> the simulator's wind field
files/metrics.py               odometry -> trajectory.csv + metrics.csv
```

## Three things worth knowing before you copy it

**An aerial world needs air, and forgetting it fails silently.** MuJoCo defaults `density` and
`viscosity` to 0 — a vacuum. The drone still hovers there, but nothing damps it, so a lateral step
rings forever and wind does nothing at all. The run then reads as badly tuned gains rather than as
missing weather. Both `quadrotor_controller` and `wind_field` warn rather than let it pass quietly.

**A quadrotor has no stabiliser.** Unlike a ground robot, an uncommanded drone is not a robot
standing still — it is a falling brick. `quadrotor_controller` therefore runs *inside* the
simulation, pulled in by the model's own manifest, which is why `scenario.osc` launches no SUT
container and is short compared with the nav2 examples. The scenario's job is to command waypoints
and record what happened.

**It needs a simulator image that carries `roqsim_aerial`.** That package holds the Crazyflie, the
controller and `wind_field` (`payload` is a core roqsim plugin), and it is part of the standard
`robovast-roqsim` image. An
image built before the package existed fails at world load with `model 'crazyflie_2' not found`,
listing the model providers it searched — which names the cause precisely enough to act on. If you
need a package the base image does not carry, a container may declare `python_packages` and RoboVAST
builds a derived image with the provenance recorded automatically.

## Seeing what the parameters did

The roqsim `scene3d` panel is added automatically and is the centrepiece: the drone flying the
course, tracked by the world's own camera. The panels declared in the `.vast` exist so the numbers
driving that picture sit beside it — without them a windy run and a heavy one both just look like
"the drone wobbled".

- **run view** — a live `state` readout (altitude, tilt, speed, position), a `timeseries` of the
  same, and a `scene` ground track with a trail that visibly bows downwind.
- **data browser** — the only built-in *cross-run* visual. A SQL query joins `runs` (which carries
  every parameter as `param_*`) to the metrics table, so the envelope shows up as a surface rather
  than as twelve separate flights.

## Metrics

`files/metrics.py` writes `trajectory.csv` (what the panels bind to) and `metrics.csv` (one row of
scalars) per run. The outcome label is deliberately three-valued rather than pass/fail, because the
interesting part of this campaign is *how* a configuration fails:

| outcome | meaning |
| --- | --- |
| `held` | flew the course within 0.15 m of the commanded altitude |
| `sagged` | airborne, but below the altitude it was told to hold — out of margin |
| `could_not_hover` | never left the pad — T/W ≤ 1 |

`could_not_hover` and `sagged` are both "did not fly the commanded course" and completely different
physics: the first is a thrust-to-weight ratio at or below 1, the second is a position loop with no
integral term trading altitude for weight. Collapsing them into pass/fail would throw the result
away.

The grid's factor levels come from a dry run rather than from taste — a factor whose every level
passes has not been varied over a range where it does anything. Measured over the course, per
payload level:

| payload | T/W | mean altitude error | outcome |
| --- | --- | --- | --- |
| 0 g | 1.32 | 0.000 m | `held` |
| 4 g | 1.15 | 0.121 m | `held` |
| 8 g | 1.02 | 0.243 m | `sagged` |

Wind shows up in `tracking_rmse` (0.273 calm → 0.363 at 2.5 m/s), and density shows up as an
*interaction* with it rather than as a main effect: at altitude the same wind pushes less hard
(0.363 → 0.328). Density does not change thrust here — the airframe's `ctrlrange` is fixed — so
payload is the thrust-margin factor and density is not.
