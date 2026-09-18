# robovast-nav

**Mobile-robot navigation for [RoboVAST](https://github.com/cps-test-lab/robovast): the
variation types that generate the worlds, routes and obstacles a navigation stack is tested
against, and the panels that show what it did.**

```bash
pip install "robovast[nav]"
```

## Variation types

Declared in a `.vast` like any other factor, each one turns into a sweep:

| Type | Varies |
|---|---|
| `FloorplanVariation` / `FloorplanGeneration` | the indoor environment itself — rooms, sizes, connectivity — from a [Floorplan-DSL](https://secorolab.github.io/FloorPlan-DSL/) model |
| `PathVariationRandom` / `PathVariationRasterized` | start and goal poses, drawn at random or on a raster over the free space |
| `ObstacleVariation` | static obstacles placed in the environment |
| `ObstacleVariationWithDistanceTrigger` | an obstacle that appears when the robot comes within a distance — the re-planning case |

A campaign that crosses floorplans with routes with obstacles is a few lines; the framework
does the expansion, the repetitions and the bookkeeping.

## Seeing a run

Three web panels for the run view, served by the RoboVAST web UI: the **2D map** with the
trajectory, the **costmap** as the stack saw it, and the **Nav2 behaviour tree** as it ticked.
A postprocessing command extracts the behaviour-tree trace from a run's bags, and a health
check flags a control loop that fell below its rate. The MCP plugin gives an agent the map, the
obstacles, the trajectory and the path deviation of any run.

## Where it fits

`robovast-nav` is one of RoboVAST's extension packages: variation types, panels, postprocessing
commands, health checks and MCP tools are all entry points, and this package provides the
navigation set. It requires `robovast`; a service without it simply lists no navigation
variation types. Nav2 is the reference stack, and the RoboVAST dataset of environments and
scenarios is built on these types.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
