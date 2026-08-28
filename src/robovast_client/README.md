# robovast-client

Talk to a running [RoboVAST](https://github.com/cps-test-lab/robovast) service — upload a
project, launch a campaign, wait for it, fetch the results — without installing RoboVAST.

```bash
pip install robovast-client
vast login https://robovast.example.org
```

## What it is for

RoboVAST runs simulation campaigns: a scenario, a sweep of configurations, repeated runs,
recorded provenance. That work happens **on the service**, on a Docker host or a
Kubernetes cluster. If you are the person *driving* it rather than the person *hosting*
it, this is all you need.

The full `robovast` distribution pulls in a simulator stack, an array library and a
dataframe library — 88 packages, around 290 MB — because it can execute campaigns itself.
This one is three dependencies (`pydantic`, `click`, `requests`) and about 30 MB, because
it only talks to something that can.

> **Not yet on PyPI.** Install it from a checkout with `pip install src/robovast_client`.

## What you get

`vast` grows commands as capability is installed; with only the client, it is:

| Command | Does |
|---|---|
| `vast login <url>` / `vast logout` | store or forget the service credentials |
| **`vast workspace run`** | **launch a campaign** — pushes the project and starts it; `--wait-and-download` blocks and fetches the results |
| `vast campaign stop/stop-job/log` | stop a campaign, kill one wedged job, read its infrastructure log |
| `vast workspace init/update/list/delete` | push a project directory to the service |
| `vast files get/put` | move a single file by address |
| `vast image build/wait/status/log` | have the service build a project's derived images |
| `vast campaign wait <campaign-id>` | block until a campaign is genuinely over |
| `vast doctor` | check the login, the service and your PATH |

`vast cluster` and `vast service` are partly here on purpose: a subcommand exists exactly
when something that can perform it is installed. The verbs above only drive a service, so
they are complete here. `vast cluster setup/cleanup/jobs-cleanup/monitor` and `vast service
upgrade/token` need a kubeconfig and come with `robovast-cluster`. So `vast cluster --help`
lists `store-cleanup` and not `setup` — nothing is stubbed and nothing fails on use.

Both groups spanning two distributions is the design: a group is named after the **object**
it acts on, not after what you installed.

There is no ambient project. Every command names its own input, and a campaign runs a
*workspace's* project — `vast workspace run <workspace> [vast]`.

An LLM agent reaches the same service through its MCP endpoint, which needs nothing
installed at all. `vast login` prints the `claude mcp add` line that registers it. The
control verbs are deliberately on both sides; each side additionally owns what only it can
do — bulk bytes and long waits here, results queries and diff-based authoring there.

## Waiting for a campaign

A campaign can run for days, so nothing blocks a request on one. `vast campaign wait` polls
the service and exits when the campaign is genuinely finished — past postprocessing, not
merely past its last run:

```bash
vast campaign wait basic-nav-2026-08-16-101500
```

Its **exit code is the answer**: `0` finished, `1` failed, `2` you interrupted the wait,
and a distinct code for "no such campaign" so a typo cannot be mistaken for a failed run.
Run it as the whole command — chaining anything after it makes the shell report the
wrapper's status instead, which turns a failed campaign into a reported success.

## What it is not

No simulator, no Kubernetes client, no Docker, no MuJoCo, no ROS. It cannot execute a
campaign or build an image itself, only ask a service to. If you need to *run* campaigns on your own machine,
install `robovast`; to host them on a cluster, add `robovast-cluster`.

## Licence

Apache-2.0.
