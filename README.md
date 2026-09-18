# RoboVAST

**Variation Automation and Scalable Testing for Robotic Systems.**

Robot software that passes one demo has not been tested. RoboVAST takes a scenario, varies
it along the dimensions that matter — floorplans, start and goal poses, obstacles, sensor
noise, parameters of the stack under test — and runs the whole sweep as repeated, recorded
simulation runs: on your machine with Docker, or across a Kubernetes cluster, with the same
`.vast` file. Every run keeps its provenance, every campaign lands in a queryable index, and
the results come back as tables, plots and web panels rather than a directory of bags.

![Framework Overview](https://raw.githubusercontent.com/cps-test-lab/robovast/main/docs/images/overview.png)

## What you get

- **A variation language.** A campaign is a `.vast` file: which scenario, which factors, how
  many levels, how many repetitions. Variation types are plugins — floorplans, paths,
  obstacles, parameter sweeps, or your own.
- **One campaign, two lanes.** The same file runs on a laptop's Docker daemon or as
  Kubernetes Jobs across a cluster. Nothing about the campaign changes; only where it runs.
- **Reproducible by construction.** Pinned container images, recorded seeds and configuration,
  a run's full provenance beside its data. A campaign can be re-run, and a result traced.
- **Simulator by name.** `backend: roqsim` gives you [roqsim](https://github.com/cps-test-lab/roqsim),
  a MuJoCo simulator for mobile robots, arms and mobile manipulators; a backend is an entry
  point, so another simulator is another package.
- **Results you can query.** Campaign data goes into Postgres; ask it with SQL from the web
  UI's explorer, a notebook, or an agent — and download it as tables and plots from the CLI.
- **Four clients, one contract.** The `vast` CLI, a web UI, an HTTP API and an MCP server for
  LLM agents all expose the same operations. An agent can author, launch, wait for and analyse
  a campaign end to end.
- **Built on [scenario-execution](https://cps-test-lab.github.io/scenario-execution/)** for
  the single run, and the [Floorplan-DSL](https://secorolab.github.io/FloorPlan-DSL/) for
  generated indoor environments. Mobile robot navigation with Nav2 is the reference use
  case, with a ready-made dataset of environments and scenarios.

## Install

```bash
pip install robovast-client              # drive a deployed service: the CLI alone, ~30 MB
pip install "robovast[nav,roqsim]"       # run campaigns yourself: the service, the local Docker lane
pip install robovast-cluster             # add the Kubernetes lane
```

Then, on a machine with Docker:

```bash
vast serve                               # the service, the web UI and the MCP endpoint on one port
vast workspace run my-experiment         # a campaign from a .vast
```

Documentation, from the quickstart to writing your own variation type or simulator backend:
[cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
