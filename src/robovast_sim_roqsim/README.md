# robovast-sim-roqsim

**A physics simulator for your robot tests, selected by one word.**

[RoboVAST](https://cps-test-lab.github.io/robovast/) runs robot software through hundreds of
varied, recorded simulation runs — and needs something to simulate them in.
[roqsim](https://github.com/cps-test-lab/roqsim) is a MuJoCo-based simulator with a ROS 2
bridge and ready-made robots (wheeled bases such as the TurtleBot 4, arms and grippers,
quadrupeds, humanoids), sensors (lidar, cameras, depth, IMU, force-torque), generated
floorplans and walking pedestrians, built to run headless and in numbers. This package makes
it a RoboVAST backend: a campaign names it, and the image, the command, the recording setup
and how the run is driven and observed all follow.

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

## Is this the package for me?

Yes, if you want a simulator that comes with the robots and sensors already in it, so that a
campaign is about *your* stack and *your* variation rather than about building a world first.
Your own worlds, robot models and plugins travel with the project; the simulator does not
have to.

RoboVAST itself names no simulator — `pip install robovast` gets you nothing from here — so a
backend is always something you add: this one, or your own, the same way.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/),
"Simulators"; roqsim itself at [cps-test-lab.github.io/roqsim](https://cps-test-lab.github.io/roqsim/).

## Licence

Apache-2.0.
