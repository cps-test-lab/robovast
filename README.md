# RoboVAST

**Variation Automation and Scalable Testing for Robotic Systems.**

Robot software that passes one demo has not been tested. RoboVAST takes your robotics stack —
a Nav2 navigation setup, a manipulation pipeline, whatever drives your robot — and runs it
through hundreds of simulated runs that differ in the ways that matter: floorplans, start and
goal poses, obstacles, sensor noise, the parameters of the stack itself. Every run is recorded
with its provenance, every campaign lands in a queryable index, and the results come back as
tables, plots and a web view of each run rather than a directory of bags.

![Framework Overview](https://raw.githubusercontent.com/cps-test-lab/robovast/main/docs/images/overview.png)

## What you get

- **Say what to vary, not how.** A campaign is one file: which scenario, which factors, how
  many levels, how many repetitions. RoboVAST does the expansion, the scheduling and the
  bookkeeping.
- **One campaign, two places to run it.** The same file runs on your laptop's Docker daemon
  or as jobs across a Kubernetes cluster. Nothing about the campaign changes; only where it
  runs and how fast it finishes.
- **Reproducible by construction.** Pinned container images, recorded seeds and
  configuration, a run's full provenance beside its data. A result can be traced, and a
  campaign re-run.
- **A simulator by name.** `backend: roqsim` gives you [roqsim](https://github.com/cps-test-lab/roqsim),
  a MuJoCo simulator for mobile robots, arms and mobile manipulators. Another simulator is
  another package.
- **Results you can ask questions of.** Campaign data goes into a database; explore it in the
  web UI, in a notebook, or through an AI agent that can author, launch and analyse campaigns
  through the same interface you use.
- **Built on [scenario-execution](https://cps-test-lab.github.io/scenario-execution/)** for
  the single run and the [Floorplan-DSL](https://secorolab.github.io/FloorPlan-DSL/) for
  generated indoor environments. Mobile robot navigation is the reference use case, with a
  ready-made set of environments and scenarios to start from.

## Install

```bash
pip install robovast-client              # drive a RoboVAST service someone runs for you
pip install "robovast[nav,roqsim]"       # run campaigns yourself, with Docker
pip install robovast-cluster             # run them across a Kubernetes cluster
```

Then `vast serve` starts the service, and `vast workspace run` launches a campaign.

Documentation, from the quickstart to writing your own variation type or simulator backend:
[cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
