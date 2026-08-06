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

**One project binding.** ``workspace_id`` is the only project binding the service
accepts, on every backend: a campaign always runs a **workspace's** ``.vast``, and
``config_path`` selects among several ``.vast`` files in that workspace. There is no
server-side "current project" — ``.robovast_project`` / ``vast init`` bind the *CLI's*
project (``vast exec local run``, ``vast results``, ``vast eval``) and never select
what the service runs. Omitting ``workspace_id`` is refused rather than resolved from
somewhere else, because the fallback that used to exist ignored ``config_path`` and so
could run a different ``.vast`` than the caller named.

**Pinned (read-only) workspaces.** ``vast serve --workspace-dir DIR`` registers a
directory as a workspace used *in place* rather than copied into the store: no
upload, present at start-up, and stable across restarts (the id is derived from
the resolved path). These entries live only in memory — never in
``registry.json`` — carry ``read_only=True``, and every mutating store op refuses
them (``WorkspaceStore._require_writable``); MCP/CLI/HTTP surface that as a clear
error.

Exactly **one** directory may be pinned. It holds as many ``.vast`` files as you
like — selected per campaign by ``config_path`` — so several pins would add no
expressiveness while leaving the service with no single sources root to report.
Pin the collection (a repo root), not each project.

Pinning needs the service to run on the host holding the directory, which decides
availability by deployment rather than by backend:

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Deployment
     - Pinning
     - How to bind a project
   * - ``--backend local``, ``local+cluster``
     - yes
     - ``--workspace-dir``; edits on disk are live
   * - ``--backend cluster`` (off-cluster driver)
     - yes
     - ``--workspace-dir``; the driver reads inputs from this filesystem
   * - ``--attach``, in-pod
     - **no**
     - upload with ``vast workspace init`` / ``create_workspace`` +
       ``update_workspace``; edits need a re-push

.. _container-exec-architecture:

Container exec: a diagnostic that cannot become a run
------------------------------------------------------

``exec_in_container`` runs one command in an experiment image. Everything about its design
follows from one requirement: it must be **structurally incapable of producing a result**,
not merely discouraged from it. An exec that could write ``/out`` or leave a
campaign-shaped directory would become the path of least resistance, and its output would
*look* like a campaign's while having no pinned provenance and no repetitions.

So: no campaign id, no campaign directory, ``/out`` never mounted, and ``OUTPUT_DIR``
pointing at a container-local path that dies with the container. A staged ``/config`` is
read-only, and the workspace — mounted at its own ``/sources/<id>`` address so a path from
``write_file`` is usable verbatim — is read-only too.

**Two sources, one path.** A workspace project (``workspace_id`` + ``config_path``) and an
existing campaign (``campaign_id``) both resolve to a *project directory*, because a
campaign's ``_config/`` is one. From there the staging is identical:
``build_campaign_data`` → ``filter_configs_by_name`` → ``prepare_campaign_configs``, and
the image resolves from that project's ``.vast`` exactly as a run resolves it.

**The entrypoint is rendered, never inherited.** ``prepare_campaign_configs`` substitutes
lane-specific init and post-run blocks into ``entrypoint.sh`` (``fixuid`` locally, config
fetch and S3 mirroring in-cluster), so a script rendered for one lane is wrong on the
other. Reusing a cluster campaign's staged entrypoint for a local exec would run cluster
post-run logic on a developer's machine. ``render_entrypoint`` exists so the bare-image
case can have that script without expanding a config tree it does not need.

**Why reuse the entrypoint at all**, rather than building an environment: the environment a
run sees is the ROS overlay, ``/ws/install``, the init block, ``execution.env`` and
``PRE_COMMAND`` — and it will grow. A diagnostic that reconstructed it would drift and
answer a different question than the run. Reuse also has a visible consequence worth
knowing: the entrypoint redirects its own stdout when given no argv, so a *started
scenario* logs to a file inside the container and the result carries ``log_path`` instead
of that output.

**A running campaign is never a target.** There is no code path from this operation to a
job's container or pod. An earlier draft made exec'ing a live campaign "safe" by annotating
its log; that was an admission the operation was wrong rather than a fix. To inspect a live
stack, the caller starts that stack in its own exec container.

**One container, so there is no state to manage.** At most one exists at a time, under a
fixed name (``robovast-exec`` — deliberately not the campaign container's single-flight
``robovast``, which ``LocalTransport`` force-removes to unblock a stop). That removes
session ids, a listing operation, and the leak class: a stray container is found and reaped
by name at service start. Its cost is reported instead of hidden — ``ResourceUsage`` carries
``exec_container``, so a lane with no room can be attributed to the caller's own container.

Two clocks bound it, and keeping them apart is load-bearing: an **idle** reap that runs
only while nothing the tool started is alive (otherwise a running scenario would be killed
while it worked), and a **hard deadline** at ``max(idle cap, limit + grace)`` so a project
declaring ``timeout: 900`` is not truncated at the idle cap. Limits are derived rather than
passed — ``ExecRequest`` has no timeout field — and the result reports which rule applied,
so a ``timed_out`` result names its own remedy.

The lane-specific half is a small protocol (``ExecLane``): ``DockerExecLane`` runs
``docker run``/``docker exec``, ``KubeExecLane`` an aux pod plus ``pods/exec``. Everything
else — validation, staging, limits, the lifetime state machine — is shared, as are the
pod primitives both in-cluster users need (``wait_pod_ready``, ``wait_pod_gone``,
``exec_stream`` in ``robovast.common.kube``; they live in ``common`` because the execution
engine may not import ``robovast.service``).

**The diagnostic stages the way a run stages.** In-cluster, ``/config`` is uploaded to the
object store and mirrored down by an ``mc`` init container on the shared sidecar image —
the same transport a campaign Job and an image-build context use. This replaced a
ConfigMap, which was simpler but capped the tree at ~900 KiB and answered "too big" with
"run it as a campaign instead", the exact expense this tool exists to save. It is
deliberately not the aux pod's tar-over-``pods/exec`` either: that needs the pod *running*
before its files exist, and needs ``tar``/``base64`` in an image we do not control, while
``mc`` is already required of every cluster experiment image. The deeper reason is that a
diagnostic with its own staging path can pass on a config the run would fail to stage.

Staging fits an init container because it happens exactly once per pod: the manager
discards a redundant staging when it reuses a held pod, and replaces the pod outright when
the identity changes, so ``/config`` never needs refreshing under a live pod. A staged tree
is torn down with its pod; a tree left by a dead service process is reaped at the next
stop, since nothing else would.

**The per-campaign aux pod stages the same way**, so there is one in-cluster transport
rather than three. Its contract is harder than the exec lane's — a *bidirectional* mirror
around every ``run()`` on a kept-alive pod, which an init container running once before
the pod starts cannot serve — so the init container injects ``mc`` into an ``emptyDir``
instead of fetching anything, and the runner mirrors through the store on each call. The
binary has to be injected because the aux image belongs to a plugin author and is not ours
to add tools to; that is the trick the rosbag postprocess Job already used to run ``mc``
inside the system-under-test's own image.

What this replaced was a base64 tarball piped through ``pods/exec``. It worked, but the
exec channel is a text websocket the client cannot half-close, so a receiver waiting for
EOF waited forever — observed for 2m47s against a live pod, on an *empty* workspace. Going
through the store needs no stdin at all, which removes that failure mode by construction
along with the ~1.33x encoding overhead and the whole-tarball buffering in the service.
The cost is that campaign *composition* now needs the object store reachable; it fails
loudly rather than falling back, since a second transport is the thing being removed.

.. _file-address-space:

One address space for files
---------------------------

Every file the service can reach has a single address, and that address **is** the URL
that serves it — the string a caller passes to ``read_file`` is the string it can
``GET``:

.. list-table::
   :header-rows: 1
   :widths: 34 46 20

   * - Address
     - Root
     - Writable
   * - ``/results/<campaign_id>/<path>``
     - the campaign directory (on the cluster, its object-store prefix)
     - **no**
   * - ``/sources/<workspace_id>/<path>``
     - the workspace's project directory
     - yes

Three properties are load-bearing.

**Content lives apart from control.** ``/campaigns/{id}/`` and ``/workspaces/{id}/`` are
*control* namespaces — a fixed vocabulary of service-owned verbs (``status``, ``logs``,
``query``, ``validate`` …, plus every name a service-endpoint plugin registers). A user's
config or output file may be called anything, so putting content there means a route can
shadow a file name — today, or the next time a route is added. Separate content
namespaces make "no reserved words, ever" true by construction.

**The namespace is the permission**, dispatched once rather than checked per operation:
there is no ``PUT``/``POST``/``DELETE`` route under ``/results`` at all, so a write there
is a 405 from the router. Results are immutable — on the cluster the local tree is a
cache of object-store objects, so a write would be a cache edit that silently vanishes.
Each namespace is confined against **its own** root via
``robovast.common.safe_path.safe_join``; a results address must never resolve inside a
workspace, or the read-only tree would inherit the writable one's permissions. The
cluster's results lane has no filesystem to resolve against, so it uses ``check_relative``
— the substrate-independent half of the same check — before composing an object key.

**The path is the real on-disk path.** ``<config_name>/<run_id>/<file>`` for a run
artifact; ``_config/``, ``_execution/``, ``_transient/``, ``_jobs/`` by their actual
names. (The predecessor route interposed a synthetic segment that matched no directory
and collided with the ``.vast`` ``run_files:`` inputs — two opposite meanings for one
name.)

A trailing slash means "directory": ``GET`` on it returns a listing, **non-recursive by
default** with a ``total`` — a campaign holds one directory per configuration and one per
run, so a recursive listing of its root is thousands of entries. (A bare owner,
``/results/<campaign>``, also lists: it can only be a directory.) The slash is how a
*client* says which representation it wants, not part of the path: ``list_files`` and
``read_file`` mean the same thing whichever way the address was spelled, because the
HTTP client normalizes before it builds the URL. Making the character load-bearing would
give one interface call two answers depending on which client made it. Listing entries are
plain names, directories suffixed ``/``, relative to the echoed address, so the next
address is a concatenation. ``GET`` on a file returns its bytes (mime-typed, so a browser
resource fetching siblings by relative URL resolves within its own directory);
``?as=text`` returns a paginated, binary-refusing text view. Paging happens **server
side** — reading 100 lines of a cluster log transfers 100 lines.

On the cluster, ``/results`` is served straight from the object store: a read is
``StorageClient.read_object`` (one object, not ``fetch_campaign``'s whole-prefix
download), and a non-recursive listing is a *delimited* ``list_entries``, so it is
non-recursive at the store and not merely in the response.

.. _fetch-what-the-caller-needs:

Fetch what the caller needs, not the campaign
----------------------------------------------

``ClusterService`` resolves a campaign to a local directory through **two** seams, and which
one a caller uses decides whether it moves kilobytes or gigabytes:

``_data_dir`` → ``fetch_campaign``
    The whole campaign prefix, downloaded into ``/tmp/robovast-campaigns/<id>``. For callers
    that genuinely need arbitrary files from it: notebook rendering, panel assets, and the
    endpoint plugins reached via ``resolve_data_dir``.

``_query_dir``
    Just ``_execution/data.db`` and ``campaign.db`` — the only two objects
    ``data_query._open_db`` opens. Used by ``describe_campaign_data`` and
    ``query_campaign_data_sql``.

The split exists because those two are the same answer locally and orders of magnitude apart
on the cluster. A query used to arrive through ``_data_dir``, so ``SELECT COUNT(*)`` over a
40 MB ``data.db`` pulled every rosbag the campaign produced — in the deployment where
campaigns are largest, and inside the HTTP request, where the client's timeout was 30 s. The
web UI survived it only because ``fetch`` sets no timeout at all.

Two properties of the narrow path are load-bearing:

* **The cached copy is validated by size, not existence.** ``data.db`` is the one campaign
  object that is *mutable* — re-postprocessing rewrites it in place, which is what
  ``fetch_campaign(force=True)`` exists for. An existence check would pin the first version a
  service ever saw and serve stale metrics indefinitely.
* **It writes through** ``_download_atomic`` **and under the campaign's fetch lock.** The
  results explorer fires one query per sub-view on first load; without both, one request
  opens a ``data.db`` another is still streaming and SQLite reports "no such table: runs".

Both seams write into the *same* cache directory, so a later whole-campaign fetch finds the
two databases already at the right size and skips them, and ``delete_campaign`` still clears
one place.

The data-status probe (``GET /campaigns/{id}/data-status``, exposed as ``describe_campaign_data(preflight_only=True)``) reports whether a query would
transfer anything and what it would cost, so a caller can explain the wait *before* it waits.
It is bounded to two ``stat_object`` calls — a probe that itself enumerated the prefix would
only move the cost it exists to warn about. It is a **control** route, not a ``/results``
path: every segment there is a user-chosen file name, so a literal ``data-status`` under it
would shadow a campaign file of that name.

``get_service_info`` publishes both address templates, plus ``results_root`` /
``sources_root`` filesystem paths — but only when the service is local-filesystem *and*
the request came from loopback, so a caller is never handed a path it cannot open.

Token-efficient file transfer
------------------------------

Because inline tool content passes through an LLM's context (costing tokens
twice — once to generate, once per later turn), the file API is split:

* **Inline authoring is limited to ``.vast`` / ``.osc``** — the small text an LLM
  writes. ``edit_file`` sends a diff, so the validate→fix loop stays cheap.
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
Because it is config (not captured data), an edit **overwrites that block in the
campaign's own ``_config/<name>.vast`` in place** — no override files, no revisions
(:mod:`robovast.service.postprocessing_edit`); the raw rosbags and the as-ran
``configuration``/``execution`` are untouched. A re-run is **dispatched in the
background** and shows up in the campaign view as a live ``postprocessing`` phase (see
:ref:`results-retrigger`).

.. _database-or-address-space:

What goes in the database, and what stays a file
------------------------------------------------

A campaign writes a lot of artifacts, and each is reachable in exactly one of two ways:
as rows in a database, or as a file through the :ref:`one address space
<file-address-space>`. The rule deciding which:

   An artifact belongs **in the database** when it is a *per-entity record you filter,
   aggregate, or join across runs* — and stays **a file** when it is a *whole document
   read once*.

"One row" is not the test; *aggregatable* is. A campaign's ``_execution/execution.yaml``
is a single document, but its contents (which robovast, which image) are exactly what one
compares *across* campaigns — and the SQL interface can attach several campaigns at once —
so it is lifted onto the ``campaign`` row. Applied to what a campaign writes:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Artifact
     - Home
   * - each run's ``test.xml``
     - DB — ``campaign.run`` (per-run record, counted constantly)
   * - each job's ``sysinfo.yaml``
     - DB — ``campaign.job``; "did the slow runs share a host?" is a join
   * - per-run metric CSVs
     - DB — one ``data.db`` table per CSV stem, after postprocessing
   * - ``_execution/execution.yaml``
     - DB — provenance columns on ``campaign.campaign`` (+ ``execution_json``)
   * - ``_transient/postprocessing.yaml``
     - DB — ``data.db``'s ``postprocessing_steps``; the file stays, as the PROV-O input
   * - the ``.vast``
     - Both — ``campaign.config_json`` for effective values, the file for authored intent
   * - ``_execution/outcome.json``
     - File — the campaign's terminal status, read by ``get_campaign_status``
   * - ``_transient/configurations.yaml``
     - File — already duplicated into ``campaign.unit``; a second copy would drift
   * - ``rosout`` (from the rosbag)
     - DB — structured messages with severity/node/stamp
   * - ``system.log``, ``controller.log``
     - File + the log tools — unstructured, and the live case is the point of reading
       them. The tools reduce them on read (``min_severity``, ``summarize`` — see
       :ref:`mcp-liveness`); nothing derived from them is stored, so no aggregate here
       competes with the tables above

Two consequences worth stating. A **file** is never *also* a table: putting
``configurations.yaml`` in the DB would create a second source of truth for the resolved
configuration list. And a **document in a cell** is not a queryable artifact:
``campaign.config_json`` holds the whole ``.vast``, which exceeds the per-cell limit and
comes back truncated, so it is queried through ``json_extract`` or the ``config_view``
rows — never ``SELECT config_json``.

Querying results
----------------

Per-run metrics are consolidated into ``<campaign>/_execution/data.db`` (one
table per CSV stem) plus a ``runs`` **dimension table** — per-run
``status``/``duration_s`` and each scenario parameter as a ``param_*`` column.
That ``runs`` table is the analytics-wide *view* over ``campaign.db``'s ``run``
table (the operational source of truth for per-run outcomes, written live from
each ``test.xml``); see :ref:`the store schema <campaign-store>`. The MCP
``run_data`` plugin exposes read-only **SQL**
(``query_campaign_data_sql`` + ``describe_campaign_data``), with ``campaign.db``
attached as schema ``campaign`` — so ``campaign.run`` is queryable for raw
pass/fail even before postprocessing builds ``data.db``. Joining ``runs`` to any
metric table on ``(config_name, run_id)`` answers "how does *<param>* affect
*<metric>*" in one query. The ``vast eval gui`` notebook path reads the same ``data.db`` directly and
is unaffected.

**Two flat views carry the joins, so a caller cannot omit one.** ``run_view`` (one row per
run: config, status, duration, params, host record) and ``config_view`` (the ``.vast`` as
one row per key) are created on the query connection as ``TEMP`` views, and are queried
unqualified. They exist because a forgotten join does not raise — ``run_id`` is unique only
*within* a configuration, so a query filtering on ``run_id`` alone silently returns rows
from every configuration and averages across them. Making the join part of the schema
removes that failure mode rather than documenting it.

They are views on the *connection*, not objects in the file, because ``campaign.db`` is
attached read-only (nothing may be written to it), because a store predating the ``job``
table would otherwise carry a view over a table it does not have, and because a change to
a view then never needs a schema migration. Where the underlying tables are missing,
``run_view`` keeps its column set and reports NULL host columns — one query shape for every
store version, with "not recorded" reading as NULL rather than as a broken query.

``describe_campaign_data`` lists both views first and carries the canonical query for each
question a caller is likely to ask — the per-run lookup, a configuration's parameters, how
a metric was produced. That is deliberate: the replacement for a tool has to be discoverable
from the tool output an assistant already reads, not only from this page.

**Columns are typed from the data, not left as text.** A CSV yields only strings, so an
untyped ingest makes every comparison lexicographic — ``ORDER BY timestamp`` puts
``"10.022"`` before ``"9.5"``, shuffling a trajectory and producing a path length that is
wrong by a factor rather than an error. So ingest infers a type per column
(:mod:`robovast.results_processing.csv_types`): a column whose every non-empty value is a
plain decimal number becomes ``INTEGER``/``REAL`` and is stored numerically, and everything
else stays ``TEXT`` verbatim. The rule is deliberately strict — one ``n/a`` demotes the
column, and ``"007"``/``"nan"`` are text (a zero-padded identifier must keep its text, and
NaN has no SQLite representation, so accepting it would delete data instead of typing it).
Scenario ``param_*`` columns are typed the same way from their resolved values.
``describe_campaign_data`` reports each column as ``"name TYPE"``, which is what tells a
caller whether a column can be ordered directly or needs ``CAST(col AS REAL)``. A
``data.db`` built before typed ingest is all-``TEXT``; re-running postprocessing retypes it.

**The declaration never outlives the evidence.** A column is declared by the first run that
writes it, but the evidence is every run — a later one can turn an ``INTEGER`` column real,
or a numeric column textual. A stale declaration is not cosmetic: a schema claiming ``REAL``
over a column holding one ``'n/a'`` makes ``AVG()`` return a plausible wrong number (SQLite
reads the text as 0) and ``MAX()`` return the text itself — the original bug wearing a
different hat. So after the last run is ingested, any table whose verdict moved is rebuilt
with the corrected types and its values carried over (``_retype_table``), which also
normalizes a mixed column so every row in it is text. Only affected tables are touched, so
the usual case rebuilds nothing.

Because a type alone cannot say "numeric except in the runs that failed", such a column is
also recorded in ``data.db``'s ``_column_notes`` and surfaced by ``describe_campaign_data``
as ``column_notes`` on the owning table — postprocessing logs a warning too, but no SQL
caller ever reads that log. The note names the fix (exclude the text rows, e.g.
``WHERE col GLOB '[0-9-]*'``) because ``CAST`` alone would silently read them as 0.

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
  / ``delete_workspace`` / ``create_upload``. A workspace's *files* are not separate
  operations: they are the writable half of the file address space below.
* **Files** — ``list_files`` / ``read_file`` / ``read_file_bytes`` / ``write_file``
  (``.vast``/``.osc`` only) / ``edit_file`` / ``delete_file`` / ``create_upload``, each
  taking one **address** (see :ref:`file-address-space`). The upload grant is
  ``POST /uploads`` rather than a workspace sub-route: its request already names the
  workspace in its address, and a path segment that had to agree with it would be an
  argument the handler either ignores or has to re-check. Bulk directory sync is **client-side
  glue over these primitives**, not a new operation: ``sync_directory_to_workspace``
  (in ``robovast.service.project_push``) walks a local directory and drives
  ``write_file`` for ``.vast``/``.osc`` and ``create_upload`` for everything
  else, with an optional ``prune`` that deletes workspace files absent locally. It is
  the single implementation behind three callers — the
  ``vast workspace init`` / ``vast workspace update`` CLI commands, the
  ``update_workspace`` MCP tool, and the web UI's drag-a-folder upload — so all three
  stay transport-agnostic (in-process ``LocalTransport`` or HTTP client) and can
  never drift.
* **Campaigns** — ``create_campaign`` (backend implicit in the deployment;
  ``upload_to_share`` is a per-campaign launch flag on the request, not a separate
  operation) / ``get_status`` / ``list_campaigns`` / ``list_jobs`` / ``get_job_log``
  / ``stop`` / the ``/archive`` stream. ``list_jobs`` + ``get_job_log`` are the **live per-job** view
  (the current batch's execution units and a single running job's log); each transport
  implements them over its own source — the local run dirs + their ``logs/system.log``
  (``LocalTransport``), or the campaign's Kubernetes Jobs + ``read_namespaced_pod_log``
  (``ClusterService``). They report live state only; the persisted per-run logs remain
  part of the campaign result data, served by ``get_campaign_logs`` unchanged.
  ``GET /campaigns/events`` is a **browser-only** Server-Sent-Events transport over
  the same ``list_campaigns`` pull (the same server-side-loop idiom as the campaign
  log stream, so there is no second enumeration to drift): it pushes the full list on
  connect and on every change, which is how the webui shows a launched campaign — and
  its ``building`` / ``variation`` / ``running`` / … phase — the instant it is
  registered, without polling. ``list_campaigns`` stays the authoritative pull for
  MCP and the CLI. Both draw from one rule: a campaign tracked in the in-process
  registry reports its live ``ControllerState``; an untracked one is reconstructed
  from its recorded facts (``reconstruct_status_from_disk`` over ``_record_dir``) — the
  same precedence ``get_status`` uses. Which campaigns *exist* is the union of three
  sources: the results directory, the registries of what is being driven, and — for a
  lane whose durable home is not that directory — an object-store index
  (:ref:`campaign-discovery`).
* **Postprocessing** — ``get_postprocessing`` / ``update_postprocessing`` /
  ``run_postprocessing``. The structured ``*_postprocessing`` pair is the programmatic
  API (MCP, CLI); ``get_postprocessing_source`` / ``update_postprocessing_source`` are
  the YAML-text twin the web UI re-run dialog edits in Monaco (mirroring the run-view
  ``*_panels_source`` visualization editor). Both overwrite the edited block in the
  campaign's own ``_config/<name>.vast`` in place. ``run_postprocessing`` dispatches the
  re-run in the background and returns at once (watch the campaign view for progress).
* **Data query** (MCP ``run_data``) — ``describe_campaign_data`` /
  ``query_campaign_data_sql``.
