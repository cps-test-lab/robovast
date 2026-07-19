.. _architecture:

============================
Client–Server Architecture
============================

RoboVAST's operations are exposed behind **one interface**, implemented **once**
server-side, so the ``vast`` CLI, the MCP server, and the :ref:`web UI <web_ui>`
are all thin **clients** of the same contract. This lets an LLM (or any client)
author, run, re-postprocess, and reason over large-scale simulation campaigns
through an interface that can live on a **different host** from the client.

One interface, one implementation, many clients
-----------------------------------------------

There is a single operation contract,
:class:`robovast.service.interface.RobovastInterface`, mirrored **1:1** in three
bindings:

* the **service** HTTP endpoints (:mod:`robovast.service.app`, FastAPI, OpenAPI
  at ``/docs``);
* the **client** :class:`robovast.service.client.RobovastClient`;
* the **MCP tools** and **``vast`` CLI** commands, which wrap the client.

Campaign status reuses :class:`robovast.execution.control_server.Status`
verbatim — the live state the campaign driver publishes — so every client reads
one status vocabulary regardless of where the campaign runs.

Where the driver runs
----------------------

A campaign is driven by one :class:`robovast.execution.controller.CampaignController`
(batch or search) that runs **in the driving process**, against the backend for
that deployment:

* **local** — the ``vast`` CLI process *is* the driver, over ``DockerBackend``;
* **cluster** — the ``robovast-service`` runs the driver in a worker thread (one
  per campaign) over ``KubernetesBackend``, which creates the scenario Jobs.

There is **no separate per-campaign controller pod**. The service is already a
long-lived, in-cluster process with the cluster config, the Kubernetes API, the
object store and the workspace (plugins installed) — so it hosts the driver
directly instead of staging the project out and launching a second process to do
it. ``stop`` and live status are therefore in-process operations the service
exposes over the interface, not a network hop to a controller. Two things a
campaign still runs as their own Kubernetes workloads, because each needs to be
one: the scenario/postprocessing **Jobs**, and — only for variations that declare
one — a per-campaign **auxiliary-container pod** the driver execs into.

Because the driver's batch wait loop blocks on the running Jobs and the
cooperative-stop flag is only checked *between* batches (or search generations),
``ClusterService.stop`` also tears down that campaign's in-flight Jobs — reusing the
Kueue-aware, campaign-scoped ``cleanup_cluster_campaign`` (the same cleanup
``vast exec cluster run-cleanup`` performs). Deleting the Jobs unblocks the wait
loop (``get_remaining_jobs`` treats a gone Job as finished) so the campaign winds
down promptly; the ``"Hold"`` (never ``"HoldAndDrain"``) queue policy means other
queued/running campaigns are not preempted. **Service shutdown** (Ctrl+C on
``vast serve``, e.g. an off-cluster ``--backend cluster -x <context>`` driver) runs
the same teardown for *every* running campaign via the
``_terminate_running_campaigns`` hook — the cluster analogue of the local backend's
single-container kill — so a bare exit never orphans in-flight Jobs on the cluster.

A stopped campaign is reported as a **clean terminal**, not a failure. Ctrl+C also
tears down the storage port-forward (it shares the process group), so the driver's
finish work — upload-to-share (when enabled), postprocessing, finalize upload — would
otherwise fail against a dead endpoint and dump misleading tracebacks. Instead, the batch wait loop
raises ``CampaignStopped`` the moment it sees the cooperative-stop flag (before any
download), the controller sets phase ``"stopped"``, and the builders' finish tail
(``_finish_campaign``) is skipped — the per-run results the jobs already uploaded are
left as the campaign's output. The ``"stopped"`` outcome is persisted like a failure
(``_record_controller_outcome`` writes ``_execution/outcome.json`` and, on the cluster,
publishes it to the object store when the tunnel is still up — a Stop-button stop), so
the phase survives a service restart instead of reconstructing as an ambiguous
``"finished"``. Every terminal-phase filter (listings, the ``--wait-and-download``
waiter, cleanup's live-set) counts ``"stopped"`` as done.

.. code-block:: text

           MCP tools ─┐
      vast CLI cmds ──┤→  RobovastClient (one interface)
         web UI ──────┘        │
          ┌────────────────────┼──────────────────────────────┐
     LocalTransport      HTTPTransport → single-host service   HTTPTransport → cluster service
     (in-process,        (`vast serve`, localhost or VM,       (in-cluster Deployment,
      DockerBackend,      DockerBackend, local FS)              KubernetesBackend, object store)
      local FS)

The service core is **backend-agnostic**: it dispatches execution to
``DockerBackend`` (local) or ``KubernetesBackend`` (cluster). See
:ref:`deployment` for the three deployment modes and how a client reaches each.

Workspaces vs. campaigns
------------------------

Two independent identifiers:

* **``workspace_id``** — a server-side folder of *editable project inputs*
  (``.vast`` / ``.osc`` / run files / binaries). Clients need no filesystem of
  their own (:mod:`robovast.service.workspaces`).
* **``campaign_id``** — a *self-contained* campaign produced by running a
  workspace's project. It snapshots its project into ``_config/`` and is
  addressed by ``campaign_id`` alone.

**Workspaces are independent of campaigns.** Editing or deleting a workspace
never affects an existing campaign, and there is no campaign→workspace link.
Results/query operations key on ``campaign_id`` only.

**Pinned (read-only) workspaces.** ``vast serve --workspace-dir DIR`` registers a
directory as a workspace used *in place* rather than copied into the store: no
upload, present at start-up, and stable across restarts (the id is derived from
the resolved path). These entries live only in memory — never in
``registry.json`` — carry ``read_only=True``, and every mutating store op refuses
them (``WorkspaceStore._require_writable``); MCP/CLI/HTTP surface that as a clear
error. Local backend only.

Token-efficient file transfer
------------------------------

Because inline tool content passes through an LLM's context (costing tokens
twice — once to generate, once per later turn), the file API is split:

* **Inline authoring is limited to ``.vast`` / ``.osc``** — the small text an LLM
  writes. ``edit_project_file`` sends a diff, so the validate→fix loop stays cheap.
* **Everything else uses the HTTP PUT side channel**: ``create_upload`` returns a
  one-time, TTL-scoped URL the client ``curl``\ s the bytes into
  (``curl -X PUT --data-binary @file <url>``), so run files, notebooks and
  binaries never enter the token stream. Executability is preserved (explicit
  flag or ``#!`` shebang auto-detect).

Data flow and result access
---------------------------

For cluster campaigns, results live in the **object store** (the durable home);
the service is a stateless gateway that streams finished campaigns from it —
``GET /campaigns/{id}/archive`` tars the campaign's objects **on the fly** into the
response (no scratch on the service, nothing buffered in memory), which is what
``vast results download`` and the web UI **Download** button use. The external
``tar.gz`` share is **opt-in at launch** (``upload_to_share``): when set, the driver
streams a raw, pre-postprocessing archive to the share the moment the runs finish,
*before* postprocessing — so the shared copy stays minimal while the object store
(and the postprocessed download) carry the derived data. For a local ``vast serve``,
the durable home is simply the local filesystem, so it has no share and refuses the
archive route.

Analysis postprocessing is **editable and re-runnable**: the raw rosbags are
always preserved, so ``results_processing.postprocessing`` entries can be changed
and re-run to compute different metrics later without re-executing the campaign.
Edits are **versioned overrides** under ``<campaign>/_control/postprocess/rev-N.vast``;
the immutable ``_config/`` snapshot is never mutated
(:mod:`robovast.service.postprocessing_edit`).

Querying results
----------------

Per-run metrics are consolidated into ``<campaign>/_execution/data.db`` (one
table per CSV stem) plus a ``runs`` **dimension table** — per-run
``status``/``duration_s`` and each scenario parameter as a ``param_*`` column.
The MCP ``run_data`` plugin exposes read-only **SQL**
(``query_campaign_data_sql`` + ``describe_campaign_data``), with ``campaign.db``
attached as schema ``campaign``. Joining ``runs`` to any metric table on
``(config_name, run_id)`` answers "how does *<param>* affect *<metric>*" in one
query. The ``vast eval gui`` notebook path reads the same ``data.db`` directly and
is unaffected.

Run view (web panel framework)
------------------------------

The web :ref:`Run view <run-view>` is a small **panel framework**, built so it can grow
(more panels, a future 3D scene, and eventually a live/streaming data source) without
reworking the core. It has four decoupled contracts, and panels only ever see the bottom
three:

* **Declaration** — the campaign's ``.vast`` carries a top-level ``visualization.panels``
  list (:class:`robovast.common.config.VisualizationConfig`), served verbatim to the UI by
  ``list_campaign_panels`` (the same raw-load pattern as ``list_campaign_plots``). The
  frontend normalizes it against each panel type's registry defaults
  (``ui/src/lib/dashboard/parseVastPanels.ts``).
* **Registry + host** — panel plugins self-register (``ui/src/lib/dashboard/registry.ts``);
  ``PanelHost`` resolves each spec's anchor/size to CSS and mounts the component. Adding a
  panel is one ``registerPanel`` call.
* **Clock** — ``PlaybackClock`` (``ui/src/lib/dashboard/clock.ts``) is the single shared
  time source (seconds on the rosbag timeline). The playback panel is the only writer; the
  rest subscribe. It is an external store, so the ~display-rate ``t`` updates while playing
  don't re-render the tree.
* **Data seam** — ``DataProvider`` (``ui/src/lib/dashboard/dataProvider.ts``) is how a panel
  gets rows/frames by table + time, decoupled from transport. Today ``dbDataProvider``
  reads one run's rows from ``data.db`` through the existing ``query``/``describe`` endpoints,
  plus the dedicated ``costmap`` endpoint for grids. The interface (``nearest`` / ``series`` /
  ``timeRange`` / ``has`` / ``costmapFrame``) is shaped so a future ``liveDataProvider`` over a
  live topic buffer drops in without touching any panel.

**Costmap delivery.** Occupancy grids can't ride the generic CSV flatten (a grid becomes
thousands of per-cell columns, past SQLite's column limit; and the read path caps a cell at
2 KB). The ``rosbags_costmap_to_csv`` handler
(:class:`robovast.results_processing.data.rosbags_process.CostmapToCsvHandler`) instead
decodes each grid once during postprocessing and re-encodes it compactly — int8 cells
zlib-compressed, base64 in a ``costmaps`` table row with the pose/geometry metadata. The
``get_costmap_frame`` interface method + ``/campaigns/{id}/costmap`` endpoint
(:func:`robovast.results_processing.data_query.read_costmap_frame`) deliver the frame nearest
a time **untruncated**; the browser inflates it with the native ``DecompressionStream``. The
``costmaps`` table description in ``describe_data_db`` gives an LLM the map's size in meters,
resolution, and layers for spatial reasoning without decoding grids.

The interface surface
---------------------

The operation contract (Phase 0 + workspaces + postprocessing shown; data-query
lives in the ``run_data`` MCP plugin):

* **Workspaces** — ``create_workspace`` / ``list_workspaces`` / ``get_workspace``
  / ``delete_workspace``; ``write_project_file`` / ``edit_project_file``
  (``.vast``/``.osc`` only) / ``read_project_file`` / ``list_project_files`` /
  ``delete_project_file`` / ``create_upload``.
* **Campaigns** — ``create_campaign`` (backend implicit in the deployment;
  ``upload_to_share`` is a per-campaign launch flag on the request, not a separate
  operation) / ``get_status`` / ``list_campaigns`` / ``list_jobs`` / ``get_job_log``
  / ``stop`` / the ``/archive`` stream. ``list_jobs`` + ``get_job_log`` are the **live per-job** view
  (the current batch's execution units and a single running job's log); each transport
  implements them over its own source — the local run dirs + their ``logs/system.log``
  (``LocalTransport``), or the campaign's Kubernetes Jobs + ``read_namespaced_pod_log``
  (``ClusterService``). They report live state only; the persisted per-run logs remain
  part of the campaign result data, served by ``get_campaign_logs`` unchanged.
* **Postprocessing** — ``get_postprocessing`` / ``update_postprocessing`` /
  ``run_postprocessing``.
* **Data query** (MCP ``run_data``) — ``describe_campaign_data`` /
  ``query_campaign_data_sql``.
