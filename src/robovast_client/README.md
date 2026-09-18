# robovast-client

**The `vast` command line for a running [RoboVAST](https://github.com/cps-test-lab/robovast)
service** — push a project, launch a campaign of simulation runs, wait for it, fetch the
results — in about 30 MB, with no simulator, no Docker and no Kubernetes client on your
machine.

```bash
pip install robovast-client
vast login https://robovast.example.org
vast workspace run my-experiment --wait-and-download
```

## Who it is for

RoboVAST runs simulation campaigns: a scenario, a sweep of configurations, repeated runs,
recorded provenance. That work happens **on the service**, on a Docker host or a Kubernetes
cluster. If you are the person *driving* it rather than the person *hosting* it, this is all
you need — and it is all your CI runner, your laptop or your teammate's need either.

The full `robovast` distribution can execute campaigns itself and carries a simulator stack,
an array library and a dataframe library to do it. This one is three dependencies
(`pydantic`, `click`, `requests`) because it only talks to something that can.

## What you get

`vast` grows commands as capability is installed; with only the client, it is:

| Command | Does |
|---|---|
| `vast login <url>` / `vast logout` | store or forget the service credentials |
| **`vast workspace run`** | **launch a campaign** — pushes the project and starts it; `--wait-and-download` blocks and fetches the results |
| `vast campaign wait <campaign-id>` | block until a campaign is genuinely over, exit code as the verdict |
| `vast campaign stop/stop-job/log` | stop a campaign, kill one wedged job, read its infrastructure log |
| `vast workspace init/update/list/delete` | push a project directory to the service |
| `vast files get/put` | move a single file by address |
| `vast image build/wait/status/log` | have the service build a project's derived images |
| `vast ui` | open the service's web UI |
| `vast doctor` | check the login, the service and your PATH |

Everything a command does, the service does; nothing here is stubbed, and a verb exists exactly
when something that can perform it is installed. `vast cluster setup` and `vast service
upgrade` need a kubeconfig, so they arrive with `robovast-cluster` — `vast cluster --help`
lists what is there and not what is missing.

## Agents get the same service

`vast login` prints the one line that registers the service's MCP endpoint with an LLM agent,
which then authors, launches, waits for and analyses campaigns through the same operations
this CLI exposes. The two sides are deliberately equal: bulk bytes and long waits live here,
results queries and diff-based authoring live there.

## Waiting for a campaign

A campaign can run for days, so nothing blocks a request on one. `vast campaign wait` polls
the service and exits when the campaign is genuinely finished — past postprocessing, not
merely past its last run:

```bash
vast campaign wait basic-nav-2026-08-16-101500
```

Its **exit code is the answer**: `0` finished, `1` failed, `2` you interrupted the wait, and a
distinct code for "no such campaign" so a typo cannot be mistaken for a failed run. Run it as
the whole command — chaining anything after it makes the shell report the wrapper's status
instead, which turns a failed campaign into a reported success.

## What it is not

No simulator, no Kubernetes client, no Docker, no MuJoCo, no ROS. It cannot execute a campaign
or build an image itself, only ask a service to. To *run* campaigns on your own machine,
install `robovast`; to host them on a cluster, add `robovast-cluster`.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/).

## Licence

Apache-2.0.
