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
* **Control** — validate and initialize a project (``.vast``), then start,
  monitor, and stop campaigns on the local Docker backend or on a Kubernetes
  cluster.

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
   * - ``init``
     - Initialize a project (write ``.robovast_project``) from a ``.vast``
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

The ``campaign_control`` plugin lets an assistant drive campaigns. It is backed
by a crash-safe run-state registry under ``<results_dir>/_control/`` that
records every launched campaign and provides a **single-flight guarantee for
local runs**: while a local campaign is live, a second local ``start`` is
refused. This prevents concurrent local launches from colliding on the shared
Docker resources they use. Cluster campaigns are concurrent by design and are
not guarded.

Both backends behave the same way from the assistant's point of view: ``start``
launches a detached child process and returns immediately, and the results end
up on local disk. For ``cluster`` this runs
``vast exec cluster run --wait-and-download`` under the hood — it launches the
in-cluster controller, waits for the campaign to finish and upload, and
downloads the results into the project results directory. So a cluster campaign
is as transparent as a local one; ``get_campaign_status`` reports on-disk progress
once results land, and the live controller phase is visible in the status
``log_tail`` meanwhile.

.. note::

   The local ``stop`` is an abrupt kill (it terminates the launched process
   group and reaps the campaign container). The cluster ``stop`` also sends the
   controller a *cooperative* stop (it finishes the current batch and then
   stops) before terminating the local waiter — killing the waiter alone would
   not stop the in-cluster controller.

.. note::

   ``list_running_campaigns`` reports liveness from the local process registry
   and makes no Kubernetes call, so it never blocks on an unreachable cluster.
   For in-cluster detail beyond the local waiter's state, use
   ``vast exec cluster monitor``.

.. note::

   The registry uses file locking (``flock``) and atomic renames, which require
   ``<results_dir>`` to be on a local filesystem. On an NFS share, locking may
   be unreliable.


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
