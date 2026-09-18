# robovast-sim-roqsim

**[roqsim](https://github.com/cps-test-lab/roqsim) as a [RoboVAST](https://github.com/cps-test-lab/robovast)
simulator backend: MuJoCo physics for mobile robots, arms, mobile manipulators, quadrupeds
and humanoids, selected by one word in a campaign.**

```yaml
execution:
  mode: ros2
  containers:
    simulation:
      backend: roqsim
      config: worlds/depot.yaml
```

```bash
pip install "robovast[roqsim]"
```

## What it gives you

roqsim is a plugin-driven MuJoCo simulator with a ROS 2 bridge, ready-made robots (TurtleBot 4
and other wheeled bases, arms and grippers, quadrupeds, humanoids), sensors (lidar, cameras,
depth cameras, IMU, force-torque), generated floorplans, walking pedestrians and 2D
navigation of its own, headless by default. This package is the piece that lets a RoboVAST
campaign name it: the image to run, the command, the GL and recording environment, which files
a world is made of, and how the simulation is driven and observed during a run all come from
the backend rather than from the `.vast`.

The RoboVAST service builds a campaign's simulator image on top of its roqsim image, so a
world, a robot model or an extra plugin travel with the project; the simulator itself does not.

## Where it fits

RoboVAST names no simulator: `pip install robovast` gets you nothing from here, and a
backend is always something you add — this one, or your own, through the same
`robovast.simulators` entry point. The default service and controller images install this
extra, which is what lets `backend: roqsim` resolve on a cluster without the campaign
shipping anything.

This package imports **without roqsim installed**: it runs inside the long-lived RoboVAST
service, which has no reason to carry a MuJoCo runtime. It declares strings and container
specs; anything that genuinely needs the simulator — enumerating the files a world is built
from, asking a scene what it contains — runs inside roqsim's own image.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/),
"Simulators"; roqsim itself at [cps-test-lab.github.io/roqsim](https://cps-test-lab.github.io/roqsim/).

## Licence

Apache-2.0.
