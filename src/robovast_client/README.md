# robovast-client

**Test your robot software at scale, from the command line.**

[RoboVAST](https://cps-test-lab.github.io/robovast/) runs your robotics stack — a Nav2
navigation setup, a manipulation pipeline, whatever drives your robot — through hundreds of
simulated runs that differ in the ways that matter: different floorplans, start and goal
poses, obstacles, sensor noise, parameters. Every run is recorded, every result is queryable,
and the whole campaign is reproducible. `robovast-client` is the `vast` command that puts
that in your terminal:

```bash
pip install robovast-client
vast login https://robovast.example.org          # a RoboVAST service your team runs
vast workspace run my-experiment --wait-and-download
```

Push your project, launch the campaign, get the results back. The simulations run on the
service — a Docker host or a Kubernetes cluster — so your machine needs nothing but Python.

## Is this the package for me?

**Yes, if someone runs a RoboVAST service and you want to use it.** You have a robot software
project and a `.vast` file that says how to vary it; you want the campaign to run and the
results to come back. That is what this does, and it is small enough to live on a laptop, a
CI runner or a teammate's machine without a second thought.

**Not yet, if you want to run the simulations yourself.** Then install
[`robovast`](https://pypi.org/project/robovast/), which is the service: it executes campaigns
on a Docker host, serves the web UI and an endpoint for AI agents, and stores the results. Add
[`robovast-cluster`](https://pypi.org/project/robovast-cluster/) to run them across a
Kubernetes cluster instead. Either one includes this client.

## What it is not

No simulator, no Kubernetes client, no Docker, no MuJoCo, no ROS. It cannot execute a campaign
or build an image itself; it asks a service to. Three dependencies (`pydantic`, `click`,
`requests`) and nothing else.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
