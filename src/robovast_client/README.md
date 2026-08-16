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
| `vast workspace init/update/list/delete` | push a project directory to the service |
| `vast files get/put` | move a single file by address |
| `vast wait <campaign-id>` | block until a campaign is genuinely over |
| `vast results download -i <id>` | fetch a finished campaign's results |
| `vast doctor` | check the login, the service and your PATH |

Everything else — authoring, validating, launching, querying results — is available to an
LLM agent through the service's MCP endpoint, which needs nothing installed at all.
`vast login` prints the `claude mcp add` line that registers it.

## Waiting for a campaign

A campaign can run for days, so nothing blocks a request on one. `vast wait` polls
the service and exits when the campaign is genuinely finished — past postprocessing, not
merely past its last run:

```bash
vast wait basic-nav-2026-08-16-101500 --interval 10
```

Its **exit code is the answer**: `0` finished, `1` failed, `2` you interrupted the wait,
and a distinct code for "no such campaign" so a typo cannot be mistaken for a failed run.
Run it as the whole command — chaining anything after it makes the shell report the
wrapper's status instead, which turns a failed campaign into a reported success.

## What it is not

No simulator, no Kubernetes client, no Docker, no MuJoCo, no ROS. It cannot execute a
campaign, only ask a service to. If you need to *run* campaigns on your own machine,
install `robovast`; to host them on a cluster, add `robovast-cluster`.

## Licence

Apache-2.0.
