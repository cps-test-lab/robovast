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
on a Docker host, serves the web UI and the agent endpoint, and stores the results. Add
[`robovast-cluster`](https://pypi.org/project/robovast-cluster/) to run them across a
Kubernetes cluster instead. Either one includes this client.

## What you can do with it

| Command | Does |
|---|---|
| `vast login <url>` / `vast logout` | connect to a service, or forget it |
| **`vast workspace run <workspace>`** | **launch a campaign** — pushes the project and starts it; `--wait-and-download` blocks until it is done and fetches the results |
| `vast campaign wait <campaign-id>` | wait for a running campaign; the exit code is the verdict |
| `vast campaign stop / stop-job / log` | stop a campaign, kill one stuck job, read its infrastructure log |
| `vast workspace init / update / list / delete` | manage your projects on the service |
| `vast image build / wait / status / log` | have the service build the container images your project needs |
| `vast files get / put` | move a single file to or from the service |
| `vast ui` | open the service's web UI in your browser |
| `vast doctor` | check the login, the service and your PATH |

Everything a command does, the service does. Verbs that need a kubeconfig — deploying or
tearing down a cluster — arrive with `robovast-cluster`; `vast --help` always lists exactly
what is installed, nothing stubbed.

## Working with an AI agent

The same service has an MCP endpoint, and `vast login` prints the one line that registers it
with an LLM agent such as Claude Code. The agent then authors a campaign, launches it, waits
for it and analyses the results through the same operations this CLI exposes — the two are
deliberately equal, with bulk transfers and long waits on this side and results queries and
diff-based authoring on the agent's.

## Waiting for a campaign

A campaign can run for hours or days, so nothing blocks on one. `vast campaign wait` polls the
service and returns when the campaign is genuinely finished — past postprocessing, not merely
past its last run:

```bash
vast campaign wait basic-nav-2026-08-16-101500
```

Its **exit code is the answer**: `0` finished, `1` failed, `2` you interrupted the wait, and a
distinct code for "no such campaign" so a typo cannot be mistaken for a failed run. Run it as
the whole command — chaining anything after it makes the shell report the wrapper's status
instead, which turns a failed campaign into a reported success.

## What it is not

No simulator, no Kubernetes client, no Docker, no MuJoCo, no ROS. It cannot execute a campaign
or build an image itself; it asks a service to. Three dependencies (`pydantic`, `click`,
`requests`) and nothing else.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
