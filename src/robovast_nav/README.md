# robovast-nav

**Test a mobile robot's navigation across the environments it will actually meet.**

A navigation stack that works in one map has been demonstrated, not tested. `robovast-nav`
extends [RoboVAST](https://cps-test-lab.github.io/robovast/) — the framework that runs your
robot software through hundreds of varied, recorded simulation runs — with the variation
that matters for navigation: **generated indoor floorplans** with different rooms and
connectivity, **start and goal poses** drawn across the free space, **static obstacles**, and
**obstacles that appear** as the robot approaches them, the case that forces re-planning.
Cross them in one campaign, and the framework does the rest.

```bash
pip install "robovast[nav]"
```

## Seeing what the robot did

Each run comes back with a web view of the trajectory on the map, the costmap as the stack
saw it, and the Nav2 behaviour tree as it ticked; a health check flags a control loop that
could not keep its rate. An AI agent connected to the service can read the same map,
trajectory and path deviation for any run.

## Is this the package for me?

Yes, if your robot drives and you want to know where its navigation breaks. Nav2 is the
reference stack, and RoboVAST's ready-made set of environments and scenarios is built on these
variation types. If your robot does not drive, RoboVAST without this package still varies
everything else; the navigation-specific parts are here so nobody else has to carry them.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
