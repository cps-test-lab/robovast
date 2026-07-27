.. _mcp:

==========
MCP Server
==========

.. _mcp-overview:

Overview
--------

RoboVAST ships an `MCP (Model Context Protocol) <https://modelcontextprotocol.io>`_
server that exposes RoboVAST to AI assistants (Claude, Open WebUI, etc.). The
server spans two concerns:

* **Analyze** — inspect campaigns, configurations, runs, logs, and tabular run
  data (read-only).
* **Control** — validate a project (``.vast``), then start, monitor, and stop
  campaigns on the local Docker backend or on a Kubernetes cluster.

A campaign always runs a **workspace's** ``.vast``: ``workspace_id`` is the only
project binding the service accepts, and ``config_path`` selects among several
``.vast`` files in that workspace. There is no server-side "current project" —
``.robovast_project`` / ``vast init`` bind the *CLI's* project (for
``vast exec local run``, ``vast results``, ``vast eval``) and never select what
the service runs. Get a ``workspace_id`` either by pinning a directory in place
with ``vast serve --workspace-dir <dir>`` (no upload; edits on disk are live —
only for a service running on that host) or by uploading one with
``create_workspace`` + ``update_workspace`` (required for ``vast serve --attach``
and in-pod services, where the directory does not exist locally).

A ``.vast`` file defines a **project**; a **campaign** is one execution of it; a
**config** is one scenario parameter set within a campaign.

.. code-block:: bash

   vast mcp serve                                # SSE on 127.0.0.1:8801 (default)
   vast mcp serve --transport stdio              # stdio (local clients, e.g. Claude Desktop)
   vast mcp serve --transport streamable-http    # modern HTTP
   vast mcp serve --transport streamable-http --host 127.0.0.1 --port 9000
   vast mcp serve --debug                        # log all MCP messages

.. warning::

   **The control tools launch and kill real compute, and the server has no
   authentication.** By default the server binds ``127.0.0.1`` (localhost only)
   precisely because the control surface is unauthenticated. Only pass
   ``--host 0.0.0.0`` (or another routable address) on a network you fully
   trust: anyone who can reach the port can start or stop campaigns.

**Options**

.. option:: --transport {stdio,sse,streamable-http}

   Transport layer. ``sse`` is the default HTTP transport; ``stdio`` is used by
   local MCP clients such as Claude Desktop; ``streamable-http`` is the modern
   HTTP transport.

.. option:: --host HOST

   Host to bind when using an HTTP transport (default ``127.0.0.1``). See the
   safety warning above before binding a routable address.

.. option:: --port PORT

   Port to bind when using an HTTP transport (default ``8801``).

.. option:: --debug

   Enable ``DEBUG`` logging for all MCP messages.


.. _mcp-taxonomy:

Tool Taxonomy
-------------

The MCP server organizes its tools along two dimensions: **operations**
(verbs) and **resources**. Tool names follow the pattern
``<verb>_<resource>[_<detail>]``.

**Operations**

.. list-table::
   :header-rows: 1
   :widths: 15 55

   * - Verb
     - Meaning
   * - ``get``
     - Retrieve a specific structured metadata object
   * - ``list``
     - Enumerate objects within a resource
   * - ``search``
     - Filter and query resources by criteria
   * - ``inspect``
     - Compute derived analysis or statistics
   * - ``validate``
     - Check a project (``.vast``) without running it, reporting all problems
   * - ``start``
     - Launch a campaign (local or cluster)
   * - ``stop``
     - Terminate a running campaign

**Resources**

.. list-table::
   :header-rows: 1
   :widths: 20 50

   * - Resource
     - Description
   * - ``campaign``
     - An experiment dataset containing configurations and runs.
       Defines the shared input files (scenario, .vast config)
       available to every configuration and run.
   * - ``configuration``
     - A specific parameterized experiment setup.
       May add configuration-specific files generated during variation.
   * - ``run``
     - An individual execution of a configuration.
       Inherits all input files from its configuration and campaign.
       Produces output files (test results, logs, rosbags).
   * - ``run_data``
     - Structured tabular run output exposed via
       dedicated query/inspect tools.
   * - ``artifact``
     - Files generated or consumed during execution


.. _mcp-control:

Campaign control
----------------

The ``campaign_control`` plugin lets an assistant drive campaigns. It is a
**strict client of a running** ``robovast-service`` — a ``vast serve`` locally,
or a tunnel / ``vast serve --attach`` to a remote VM or cluster. The service is
the single execution authority and owns run-state tracking; there is **no local
subprocess path**. When no service is reachable the control tools fail loudly
(``{"error": "no robovast-service reachable — start a 'vast serve' …"}``) rather
than silently running a divergent local lane. (For a serviceless local run, use
the ``vast exec local run`` CLI directly.)

``start_campaign`` validates and launches through the service and returns
immediately; poll ``get_campaign_status``. Results live wherever the service
keeps them — local disk for a local ``vast serve``, the object store for a
cluster service (retrieve via the web UI or ``get_campaign_download``).

**Choosing a lane on a dual-backend serve.** A ``vast serve --backend
local+cluster`` (a dev host with both Docker and kubeconfig) offers *both* lanes
in one service and routes per campaign. There, three tools take an optional
``backend`` argument:

* ``start_campaign(backend=…)`` — ``"local"`` pilots on the serve host's Docker;
  ``"cluster"`` dispatches Kubernetes Jobs. Empty uses the service's **default
  lane (cluster when available)**.
* ``build_experiment_image(backend=…)`` — build for the lane you will run on.
* ``resource_usage(backend=…)`` — size the lane you target.

Every other tool is scoped to an existing ``campaign_id`` (or ``build_id``), so
the service resolves the lane itself and no ``backend`` argument is needed.
Single-backend services (a plain local or in-cluster ``vast serve``, or
``--attach``) offer one lane and ignore ``backend``.

.. note::

   ``stop_campaign`` is a cooperative stop through the service, which owns the
   teardown (terminating a local Docker container, or the cluster's in-flight
   scenario Jobs). ``list_running_campaigns`` reports the campaigns the service
   considers live (all lanes).

.. note::

   ``list_campaign_jobs`` and ``get_job_log`` give an assistant the same **live
   per-job** view the web UI Monitor shows: the current batch's jobs with their
   status (running / pending / completed / failed) and aggregate counts, and the
   live log of a single **running** job (its scenario container's output — the
   running pod's log on the cluster, the live ``system.log`` file locally). A
   finished job whose pod has been garbage-collected has no live log.

.. note::

   ``resource_usage`` reports an execution lane's CPU/memory capacity and current
   usage, plus a ``parallel_runs`` flag. The fields mean the same thing on either
   lane, so an assistant reads them uniformly. Use it to size a ``.vast`` run
   against free capacity: with ``free_cpu = cpu_capacity - cpu_used`` (and the same
   for memory), a run's concurrency is ``1`` when ``parallel_runs`` is false,
   otherwise ``min(⌊free_cpu / run_cpu⌋, ⌊free_mem / run_mem⌋)`` from the per-run
   reservations in the ``.vast`` — and the wall time is roughly
   ``⌈num_runs / concurrency⌉ × per_run_time``.


Building experiment images
---------------------------

When an experiment needs new code or system packages **baked into its container
image** (a new ``sim_suite`` package, an apt dependency), the assistant declares a
:ref:`build section <config-build-section>` in the ``.vast`` and sets
``execution.image: build:<tag>``. The ``campaign_control`` plugin then exposes:

* ``build_experiment_image`` — build (or reuse) the image from the project's
  ``build:`` section. Returns ``{build_id, tag, cached}``.
* ``get_image_build_status`` — poll a build: ``phase`` / ``done`` plus, on failure,
  a **structured** ``error_detail`` (``phase`` = apt / pip / source-build /
  base-pull / push / resource, the offending ``build:`` ``entry``, and
  ``fixable_by`` = ``agent`` or ``infra``).
* ``get_image_build_log`` — the raw builder log for deep dives.

The workflow is three steps and stays entirely in the ``.vast`` the assistant
already edits:

#. edit the ``build:`` section (and drop in the new package);
#. ``build_experiment_image`` — **idempotent**, so it is safe to always call (a
   no-op cache hit when nothing changed);
#. ``start_campaign`` — the image is wired in automatically.

The assistant may even skip step 2: ``start_campaign`` on a ``build:<tag>`` project
(re)builds the image as its first step. The build runs **where the backend runs**
(local ``docker buildx`` for a local ``vast serve``, an in-cluster BuildKit Job on
the cluster), and the assistant **never handles a registry reference or
credentials** — the symbolic ``build:<tag>`` is all it ever sees. Requires a
reachable ``robovast-service`` (a ``vast serve`` or a tunnel); on the cluster it
also requires a registry configured at ``vast exec cluster setup`` (see
:doc:`cluster_execution`).

.. _mcp-tools:

Available Tools
---------------

All tools are provided by plugins loaded at startup via the
``robovast.mcp_plugins`` entry-point group. The table below is generated from the
**registered** plugins, so it always reflects the tools the server actually
exposes (installing extras such as ``nav`` adds more).

Use MCP Inspector or a compatible client to explore the available tools and
their input/output schemas.

.. code-block:: bash

    npx @modelcontextprotocol/inspector

.. mcp-tools::
