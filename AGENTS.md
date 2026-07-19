# AGENTS.md — working agreements for RoboVAST

Guidance for any agent or contributor changing this repository. These are project
**invariants**, not suggestions — check a change against them before finishing.

## 1. A change must work across every surface and backend

RoboVAST exposes one operation contract — `RobovastInterface`
(`src/robovast/service/interface.py`) — behind **four clients** and **two execution
backends**. A feature is not done until it works on all of them:

- **CLI** (`vast …`) **and MCP** (the `robovast.mcp_plugins` tools) — both are thin
  clients of the same interface; the web UI (`ui/`) is a third. When you add or change
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

## 2. Documentation is split into user-facing and internal

`docs/` separates the two audiences — keep them distinct and both current:

- **User-facing** — how to *use* RoboVAST: `how_to_run.rst`, `configuration.rst`,
  `variation.rst`, `results_processing.rst`, `evaluation.rst`, `deployment.rst`,
  `web_ui.rst`, `mcp.rst`, `setup.rst`, `example.rst`. Task-oriented, no
  implementation detail beyond what a user needs.
- **Internal / developer** — how RoboVAST *works*: `developer_guide.rst`,
  `architecture.rst` (the client–server design), and internals sections. Design,
  seams, and extension points.

When behaviour changes, update the user page(s) **and** the developer docs; don't mix
implementation detail into user pages or leave internals undocumented.

## 3. Keep the code clean — no deprecated paths, no fallbacks

Change code in place and delete what it replaces. Fix problems at the root — one
correct path, not a primary path plus a compatibility shim or silent workaround.
(Handling genuinely-absent data is fine; tolerating the old way of doing things is not.)
