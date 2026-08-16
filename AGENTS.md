# AGENTS.md — working agreements for RoboVAST

Guidance for any agent or contributor changing this repository. These are project
**invariants**, not suggestions — check a change against them before finishing.

## 1. A change must work across every surface and backend

RoboVAST exposes one operation contract — `RobovastInterface`
(`src/robovast/service/interface.py`) — behind **four clients** and **two execution
backends**. A feature is not done until it works on all of them:

- **CLI** (`vast …`) **and MCP** (the `robovast.mcp_plugins` tools) — both are thin
  clients of the same interface; the web UI (`frontend/ui/`) is a third. When you add or change
  an operation, thread it end-to-end:
  1. `interface.py` — the abstract method + `Routes` entry + request/response models;
  2. `client.py` — **both** transports (`LocalTransport` in-process **and**
     `HTTPTransport`);
  3. `app.py` — the HTTP route;
  4. surface it in **both** the `vast` CLI **and** the MCP tools.
  Don't implement an operation in only one client (e.g. an MCP tool that calls a local
  function directly) — put it on the interface so every client gets it.
- **Local (Docker) and cluster (Kubernetes) execution** must both honor the change.
  Anything the controller/pod needs (e.g. which `.vast` to run) has to be passed
  through to the cluster path too (`cluster_service.py` → pod env → `cluster_bootstrap.py`),
  not just the local path. Verify against a local `vast serve` **and** the cluster flow.

Keep the CLI, MCP, and web UI behaviourally consistent — same inputs, same results,
regardless of which client or backend is used.

### On the cluster lane, say which part of a campaign you need

`ClusterService._data_dir` **raises**. It is `LocalTransport`'s "the campaign's
directory", which on the cluster has no cheap answer — it used to mean `fetch_campaign`,
so every inherited method that touched it silently became a whole-campaign download.
Nothing errored; the page was just slow and the pod moved gigabytes. `list_campaign_plots`
pulled every rosbag to read one small `.vast`, once per campaign, on every Results load.

So a caller states its need, and pays only that:

- `_query_dir` — the two databases a SQL query opens;
- `_config_dir` — the frozen `_config` snapshot (declared plots, panel assets);
- `_whole_campaign_dir` — everything, when the caller genuinely cannot know which files
  it will read (notebook rendering, the `/results` address space).

If you add a method that wants "the campaign directory", pick one of those. Reaching for
`_data_dir` fails immediately rather than quietly costing a terabyte in production.

### Every request is authenticated

There is no unauthenticated mode, in development or in production
(`src/robovast/service/auth.py`). An unset `ROBOVAST_AUTH_TOKEN` is *minted*, not ignored,
so "reachable but open" is not a state anyone reaches by forgetting a variable.

Two carriers, and the distinction is load-bearing: **browsers use a cookie** because
`EventSource` cannot set request headers, so a header-only scheme would break every live
stream in the web UI; **the CLI and MCP send `Authorization: Bearer`**. A new route is
covered automatically — the gate is ASGI middleware, not a FastAPI dependency, because a
mounted sub-app (`/mcp`) does not run the parent's dependencies, and `BaseHTTPMiddleware`
buffers streaming responses.

The middleware resolves a `Principal`, not a boolean. Read identity from it rather than
re-parsing headers, so swapping the shared secret for an identity provider later replaces
one resolver instead of every route.

## 2. Documentation is split into user-facing and internal

`docs/` separates the two audiences — keep them distinct and both current:

- **User-facing** — how to *use* RoboVAST: `quickstart.rst`, `how_to_run.rst`,
  `configuration.rst`, `variation.rst`, `results_processing.rst`, `evaluation.rst`,
  `deployment.rst`, `web_ui.rst`, `mcp.rst`, `setup.rst`, `example.rst`. Task-oriented, no
  implementation detail beyond what a user needs. `quickstart.rst` is split by *role* —
  operator versus user — because only one of them touches Kubernetes; keep that split when
  editing it.
- **Internal / developer** — how RoboVAST *works*: `developer_guide.rst`,
  `architecture.rst` (the client–server design), and internals sections. Design,
  seams, and extension points.

When behaviour changes, update the user page(s) **and** the developer docs; don't mix
implementation detail into user pages or leave internals undocumented.

## 3. Keep the code clean — no deprecated paths, no fallbacks

Change code in place and delete what it replaces. Fix problems at the root — one
correct path, not a primary path plus a compatibility shim or silent workaround.
(Handling genuinely-absent data is fine; tolerating the old way of doing things is not.)

## 4. Report only what the caller can rely on

An interface's job is to tell the truth about what happened. A wrong answer that
looks right is worse than an error, because nothing downstream can detect it.

- **Never ignore an argument.** If a value cannot be honoured, fail; do not quietly
  act on something else and return success. Argument-*free* policy (a default
  location, say) may have a precedence — a supplied argument may not.
- **Never advertise a path, id, or capability the caller cannot use.** Offer it only
  when it is usable from where the caller is.
- **Distinguish absent data from a failed lookup.** "None yet" is an empty result;
  "this should exist and does not" is an error. Collapsing them either way misleads.
- **A status must be able to say "unhealthy".** If the only way to learn that work is
  failing is to know which log to grep, the status is incomplete.
- **Long-running work returns a handle, not a blocked call.** An inline wait turns a
  succeeding operation into a client timeout, i.e. a false failure.
- **One source of truth per fact.** Derive or reference it; a second copy will
  disagree eventually. Prefer answering a narrower question over duplicating state.

## 5. Which distribution owns what

RoboVAST ships as several distributions so each audience installs what it needs and — the part
that matters — can decline what it does not.

| Distribution | Contains | Adds |
|---|---|---|
| `robovast-client` | the `vast` root command group, the interface models, the HTTP client, the credential store | `pydantic`, `click`, `requests` |
| `robovast` | service core, config/variation, results, MCP, controller, the local Docker lane | no kubernetes |
| `robovast-cluster` | the Kubernetes execution lane, its cluster-config plugins, and the deploy/operator commands | `kubernetes`, `boto3`, `google-cloud-storage` |
| `robovast-nav` | navigation variation types, panels | `pyside6`, `scipy`, … |
| `robovast-sim-roqsim` | the roqsim simulator backend | `pydantic` only |

Four rules keep this working:

- **The dependency direction is the whole design.** `robovast-client` depends on nothing of
  ours; `robovast` depends on it; the lanes and `robovast-nav` depend on `robovast`. So
  **`robovast` must never depend on a lane** -- the edge back is
  what would make the graph cyclic. An innocent-looking `robovast[cluster]` extra recreates exactly
  the cycle `src/robovast_sim_roqsim/pyproject.toml` warns about. A lane is therefore **not
  installable via `--extras`** — anywhere that installs one (the controller Dockerfile, `make
  venv`, `make build`) needs its own step, and a test guards the Dockerfile because that omission
  cannot fail before deployment.
- **A plugin must import without the thing it drives.** Entry points are resolved to *list* what is
  available, in processes that may have no Docker and no kubeconfig; reaching for either belongs in
  the factory that runs once a caller has asked for that plugin by name. Stated for simulators in
  `common/simulators.py` and for execution lanes in `service/serve_backends.py`.
- **Missing means missing, not broken.** With a lane absent, `vast` must still start, `vast exec`
  must list only what is installed, `vast doctor` must warn rather than fail, and asking for the
  absent lane must name the lanes that exist — never a `ModuleNotFoundError` for a module the
  caller never mentioned. Core degrading correctly is covered by
  `tests/execution/test_core_without_cluster_package.py`; keep it that way.

**A client install must stay a working install.** `robovast-client` ships without the core,
and every leak found so far has been a *deferred* import of it -- the module imports fine and
the command dies at call time, in exactly the install the distribution advertises. An import
check cannot see that; `tests/service/test_client_needs_no_core.py` drives the commands with
the core made un-importable. Anything the client needs must live in the client: a wire
constant like `COMMAND_LIMIT_S` belongs in `interface.py`, not in the server module that
enforces it.

`robovast-cluster` and `robovast-client` ship into the **same import namespace** as the core: `robovast/` and
`robovast/execution/` deliberately carry no `__init__.py` in either distribution, which makes them
PEP 420 namespace packages so the two source trees merge at import time. Adding an `__init__.py` to
either directory silently breaks the merge — do not.

A corollary for tests: a guard that scans a source tree by path must scan **both** trees. One that
scanned only `src/robovast` kept passing after the cluster code moved out from under it, which is
the worst failure a guard has — a green tick over an unchecked tree.
