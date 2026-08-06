.. _web_ui:

======
Web UI
======

RoboVAST ships a small **web frontend** — a browser client of the
``robovast-service`` (see :ref:`architecture`). It is a thin client of the same
:class:`robovast.service.interface.RobovastInterface` contract the CLI and MCP
server use, so it works identically against a local ``vast serve`` or an
in-cluster service.

It provides four views, one per desktop GUI:

* **Monitor** — lists campaigns and shows each one's live progress (phase, per-batch
  run progress, budget/stopping criteria), with a **Stop** action and a collapsible
  **live log** panel. The campaign list itself is **streamed** over Server-Sent Events
  (``GET /campaigns/events``), not polled: a launched campaign appears in the list
  immediately — with its true live phase and its **start time** (shown in your
  browser's locale and timezone) — and every phase change is pushed within a second.
  The phase reflects the whole lifecycle, including the two pre-run steps that used to
  be invisible: ``building`` (the campaign is **waiting for its experiment image** —
  builds are content-addressed and shared, so it may be waiting on one another campaign
  triggered) and ``variation`` (the campaign's configurations are being expanded), then
  ``running`` → ``finishing`` → ``postprocessing`` → ``finished`` (or ``failed`` /
  ``stopped``). A build that fails is shown as a ``failed`` campaign in the list rather
  than vanishing, and its builder output is in that campaign's own log under a ``BUILD``
  divider. The per-batch run bar also distinguishes **finished** runs (the
  solid fill) from those **currently running** (a lighter segment on top), with the
  exact ``running N · pending M`` counts beside it. While a fixed-size campaign runs,
  an **ETA** (``~12m left (≈ 14:35)``, the estimated finish time in your locale)
  appears next to that count once at least one run has
  finished, extrapolating from the average time per completed run. A collapsible **Jobs** list shows
  each execution unit of the current batch — a *run* locally, a Kubernetes *Job* on the
  cluster — with its status; expanding a running one streams that **job's own live log**
  (its scenario container's output). The campaign **live log** panel below is the
  campaign's unified *infrastructure* log — the variation (config generation), run
  (controller) and postprocessing phases assembled into one stream with
  ``===== PHASE =====`` dividers — streamed live and shown in full once it finishes.
  **Stop** cooperatively ends the campaign *and* terminates its in-flight jobs, so
  running work halts promptly (not only after the current batch). A finished cluster
  campaign also shows a **Download** button that streams its postprocessed
  ``tar.gz`` straight from the object store (offered only for a cluster service — a
  local service's results are already on its filesystem). A finished campaign's
  actions menu offers **Retrigger postprocessing**, which opens a dialog to *adapt
  the* ``results_processing.postprocessing`` *block* (in a Monaco YAML editor) and
  re-run the analysis against the preserved raw rosbags — to compute different metrics
  after the fact without a new run. Because a campaign is self-contained (it carries
  the ``.vast`` that ran), the edit is saved as a *new versioned override* of that
  file (``_control/postprocess/rev-N.vast``); the immutable ``_config/`` snapshot is
  never touched. The browser equivalent of ``vast exec cluster monitor``.
* **Launcher** — starts a campaign from a workspace (which ``.vast``, config filter,
  runs per configuration, *Postprocess when done* and *Upload to share when done*
  toggles) and watches its live status. The browser equivalent of ``vast exec
  cluster run``. *Upload to share when done* streams a raw, pre-postprocessing
  ``tar.gz`` to the configured external share the moment the runs finish (off by
  default; the share destination comes from the service's ``.env``).
* **Config** — a workspace-based ``.vast`` editor with live validation and a
  generated-configuration preview. The browser equivalent of ``vast config gui``.
* **Results** — browse a campaign's data, run read-only SQL, and chart it. The
  browser equivalent of ``vast eval gui`` (SQL + charts rather than notebooks).

.. _web-ui-config:

Config editor
-------------

The **Config** tab replicates ``vast config gui`` in the browser. Because a browser
has no working directory, the project lives in a server-side **workspace** (see
:ref:`architecture`): select or create one, **upload** the scenario file and any
run files, and author the ``.vast`` in the Monaco editor.

.. tip::

   To seed a workspace from an existing project directory in one command (instead of
   uploading files by hand), use the CLI — it mirrors the MCP workspace tools and
   drives the same service:

   .. code-block:: bash

      vast workspace init configs/examples/ros2_basic --name ros2demo
      vast workspace list        # confirm it
      # then open the Config tab and pick "ros2demo" from the workspace dropdown

   ``vast workspace init`` writes ``.vast``/``.osc`` inline and uploads the rest
   (preserving sub-directories and the executable bit). Re-running it on the same
   directory creates a *new* workspace each time; a name that already exists gets
   an incrementing ``-2``/``-3`` suffix (printed in the command's output) so the
   copies stay distinguishable in the dropdown.

   To push a fresh version of a project **into the workspace that already exists**
   (instead of making another copy), use ``vast workspace update`` with the
   workspace's ``ws-…`` id or name:

   .. code-block:: bash

      vast workspace update ros2demo configs/examples/ros2_basic          # add + overwrite
      vast workspace update ros2demo configs/examples/ros2_basic --prune  # also delete removed files

   To work on individual files rather than sync a whole directory, a workspace's
   contents are addressable as ``/sources/<workspace_id>/<path>`` — the writable
   half of the same address space a campaign's outputs use (see
   :ref:`reading-result-files`):

   .. code-block:: bash

      vast files ls  /sources/ros2demo/                   # what the workspace holds
      vast files cat /sources/ros2demo/demo.vast
      vast files put /sources/ros2demo/files/run.sh ./run.sh
      vast files rm  /sources/ros2demo/files/old.osc

   ``put`` writes ``.vast``/``.osc`` directly and streams everything else through
   the upload side channel, preserving the executable bit — the same two paths the
   Config tab's drag-a-folder upload uses. A workspace pinned with
   ``vast serve --workspace-dir`` is read-only: edit those files on disk.

   ``update`` re-uploads every file (overwriting in place) with the same inline /
   side-channel split and skip rules as ``init``. By default it only adds and
   overwrites; ``--prune`` also deletes workspace files that no longer exist under
   the directory, so the workspace mirrors it exactly. Read-only pinned workspaces
   (``--workspace-dir``) refuse the update — edit their files on disk instead. The
   MCP tool ``update_workspace`` does the same for an LLM/agent client (see
   :ref:`architecture`), refreshing a whole project in one call rather than looping
   per-file writes. In the browser, dragging a project folder onto the **Config →
   Files** tab performs the same add/overwrite sync.

.. tip::

   To skip the upload entirely and always have a project available — even across
   restarts — pin its directory at launch (local backend only):

   .. code-block:: bash

      vast serve --workspace-dir configs/examples/ros2_basic

   The directory is used **in place** as a **read-only** workspace: it appears in
   the dropdown the moment the service starts, with a path-stable id so its UI
   link keeps working after a restart. Edit the files on disk to change it —
   writes through the service/UI/MCP are refused (campaign outputs still land in
   the shared results store, never under the pinned dir). One directory may be
   pinned, named after itself; it may hold any number of ``.vast`` files, chosen per
   campaign, so pin the collection rather than each project. Hidden files and
   ``results/`` are skipped, exactly like ``workspace init``.

   **It lands wherever the UI is — usually with no flag at all.** A workspace
   lives in the store of whichever service you talk to, and ``vast workspace``
   **follows whatever service is already running**: it probes the local service
   port, so if a local ``vast serve`` or a ``vast serve --attach`` (a held-open
   tunnel to the cluster) is up, the command rides that same endpoint — the exact
   one the browser is on:

   .. code-block:: bash

      # (a) vast serve --attach running in another terminal → this follows it:
      vast workspace init configs/examples/growth_sim
      #> Target: service (http://127.0.0.1:8800) [detected — following a running vast serve]

      # (b) nothing running → this machine, in-process:
      vast workspace init configs/examples/growth_sim
      #> Target: this machine, in-process (store: …)

      # (c) reach the cluster with NO service running → open your own ephemeral tunnel:
      vast workspace init configs/examples/growth_sim --cluster
      #> Target: in-cluster service (http://127.0.0.1:…)

   So you rarely type ``--cluster``: keep a ``vast serve --attach`` open and every
   ``vast workspace`` command follows it automatically. Use ``--cluster`` only to
   reach the cluster when no service is up (it opens an ephemeral
   ``kubectl port-forward`` for the call; add ``-x <context>`` / ``-n
   <namespace>`` to pick the cluster). Every command prints the ``Target:`` it
   resolved — including ``[detected]`` — so the choice is never silent. To reach a
   service that is neither local nor in this cluster (a remote VM), bring up your
   own tunnel to the conventional port ``127.0.0.1:8800``
   (``ssh -N -L 8800:127.0.0.1:8800 <vm>``) — the same auto-detect then follows it,
   no flag needed.

The ``.vast`` JSON Schema
(from the service) drives completion and inline validation as you type, and the
service validates the whole project (schema + scenario references + plugin refs)
after each edit — problems appear in the panel below the editor. **Generate** expands
the config and lists the resolved configurations with their parameters, without
running anything. Variation plugins declared in the ``.vast`` ``plugins:`` list are
installed server-side automatically, so validation and preview resolve them.

Starting it
-----------

**The service serves the UI itself**, so the web frontend comes up together with
``robovast-service`` — there is no separate web server to run or port to expose.
Build the UI once, then start the service:

.. code-block:: bash

   cd ui && npm install && npm run build     # emits ui/dist (served by the service)
   vast serve                                # serves the UI + REST API on one port

Open the service URL in a browser and you get the UI; the REST API is served
same-origin under the same URL (OpenAPI at ``/docs``). The in-cluster service
ships the same build in its image, so mode 3 needs no extra step.

Accessing it — ``vast ui``
--------------------------

``vast ui`` is a thin shortcut: it opens a browser at the service on the
conventional local port and does nothing else. Something must already be serving
there — you make the service reachable with ``vast serve``, and ``vast ui`` opens
it:

.. code-block:: bash

   vast serve            # this machine: local service on :8800
   vast serve --attach   # the in-cluster service: holds a tunnel on :8800
   vast ui               # open a browser at whatever is serving on :8800

* **This machine** — run ``vast serve`` (local backend, serves the UI itself),
  then ``vast ui`` to open it. If nothing is answering on the port, ``vast ui``
  says so and exits rather than starting anything — ``vast serve`` is the one
  command that owns the service lifecycle.
* **Cluster** — deploy with ``vast exec cluster setup``, then ``vast serve
  --attach`` (``-x/--context``, ``-n/--namespace`` pick which cluster) holds a
  ``kubectl port-forward`` on :8800; ``vast ui`` then opens it. Needs ``kubectl``
  + a kubeconfig; the attach process runs in the foreground and Ctrl-C closes the
  tunnel.
* **Remote VM** — the service binds ``127.0.0.1`` there, so reach it with your
  own SSH tunnel (``ssh -N -L 8800:127.0.0.1:8800 <vm>``) and open
  ``http://127.0.0.1:8800``. Because that is the conventional port, ``vast ui``
  and every other command auto-detect the tunnel — nothing to export.

Because the service serves the **web UI and the REST API on the same port**,
whatever ``vast ui`` opens is all a browser, the ``vast`` CLI, and the MCP server
need. Other service-touching commands (``vast workspace …``) reach the same place
with the same ``--cluster`` switch, so nothing needs to be exported.

A connection indicator in the top bar turns green once the service answers, and
shows the backend's live **resource usage** — used vs. total CPU cores and memory
(``cpu 6/16 · mem 22/62 GiB``). The numbers are backend-appropriate: the host
machine's utilisation for a local ``vast serve``, and the cluster's node
capacity vs. the summed requests of the pods scheduled onto those nodes for an
in-cluster service (runs still queued for a node show up in the jobs meter as
pending, not as CPU in use). Hovering the chip
reveals the RoboVAST version, the backend, and whether runs execute in parallel
(cluster) or one at a time (local). The reading is sampled at most once every few
seconds and shared across browser tabs, so it never loads the backend.

.. note::

   The service is **unauthenticated in v1** and must stay behind the
   localhost / SSH-tunnel / ``kubectl port-forward`` boundary — do not expose it
   directly. Public access (Ingress + token/TLS) is a deferred, whole-surface
   decision (see :ref:`deployment`).

Results viewer
--------------

The **Results** tab explores a finished campaign's data through three sub-views: an
**Explorer** (a campaign → config → run tree with per-node notebook reports), a
**Run view** (a time-driven, panel-based replay of one run — see below), and a
**Data browser** (ad-hoc SQL + charts).

**Explorer.** The tree shows each campaign's configs and runs with a pass/fail status
dot; selecting a node opens its details on the right. When a campaign declares
:ref:`evaluation notebooks <evaluation-notebooks>` under
``evaluation.visualization``, each workload appears as a **tab**, and the notebook for
the selected node's level (campaign / config / run) is executed server-side and shown
as a rendered HTML page — the web equivalent of ``vast eval gui``. The notebook's
``DATA_DIR`` is set to the selected node's directory (the same contract the desktop
tool uses), so the *same* notebooks work in both. Output is cached, so re-selecting a
node is instant. There is also an **Open in Data browser** button that jumps to the
Data browser scoped to the selected node.

**Data browser.** The left panel lists the tables in the campaign's
``_execution/data.db`` — one per metric CSV, plus the ``runs`` **dimension table**
(per-run ``status``/``duration_s`` and each scenario parameter as a ``param_*``
column), with ``campaign.db`` attached as schema ``campaign``. Write **read-only SQL**
in the editor and **Run** it; the result shows as a table and, via the chart builder,
as a chart — pick *x* / *y* / *color* columns and a mark. Join ``runs`` to any metric
table on ``(config_name, run_id)`` to answer "how does *<param>* affect *<metric>*".

**The first query on a cluster campaign may pause.** Its databases live in the object
store, and the service copies them into a local cache inside that first request; every
query afterwards reads the cache. Rather than an unexplained spinner, the Explorer's
loading row and the Data browser's toolbar say what is happening and how much is moving
("First query — fetching campaign data (39.9 MiB) from the object store over a
port-forward…"). Nothing is shown for a local service, which transfers nothing, or for a
campaign already cached — so the message appears exactly when there is something to
explain. It comes from ``GET /campaigns/{id}/data-status``, which is cheap enough to ask
alongside the query itself.

.. note::

   A campaign only becomes queryable once **analysis postprocessing** has run — it
   builds ``data.db`` from the raw rosbags. Launching with **Postprocess when done**
   (the default) runs it automatically on both backends; otherwise the Results tab
   offers a **Run postprocessing** button, and the CLI equivalent is
   ``vast results postprocess --campaign <id>``. To *change* the postprocessing
   parameters and re-run, use **Retrigger postprocessing** in the Monitor view's
   campaign actions menu (see above). The rosbag→CSV step always runs in
   the campaign's own execution image (locally in a container, in a cluster as a Job),
   because rosbags only deserialize where the system-under-test's ROS2 message types
   are defined.

**Declared plots.** A campaign can carry its own saved plots, authored in the
``.vast`` under ``evaluation.plots`` (analogous to referencing analysis notebooks).
Each plot is a SQL query plus a `Vega-Lite <https://vega.github.io/vega-lite/>`_
encoding; the viewer runs the query and binds the result rows into the spec as
``data.values`` — so the query's column aliases are the Vega-Lite ``field`` names and
no ``data`` block is written:

.. code-block:: yaml

   evaluation:
     plots:
       - title: "Landing error vs wind speed"
         query: >
           SELECT r.param_wind_speed AS wind_speed,
                  m.value            AS landing_error,
                  r.config_name      AS config
           FROM runs r
           JOIN landing_error m ON (m.config_name = r.config_name AND m.run_id = r.run_id)
         vega_lite:
           mark: point
           encoding:
             x:     { field: wind_speed,    type: quantitative }
             y:     { field: landing_error, type: quantitative }
             color: { field: config,        type: nominal }

Declared plots render automatically in the Results tab for that campaign, and are
schema-validated with the rest of the ``.vast`` in the Config editor.

The same SQL surface is available to an LLM through the MCP ``describe_campaign_data``
/ ``query_campaign_data_sql`` tools, which resolve locally or delegate to a configured
service — so CLI, MCP, and the web UI read results the same way.

.. _run-view:

Run view
--------

The **Run view** replays a *single run* of a postprocessed campaign over its **rosbag
timeline**. You pick a campaign and a run; the view then lays out a set of **panels**
that a shared **playback clock** drives — dragging the timeline moves every panel to
the same instant. All panels read only the run's recorded results — its postprocessed
``data.db``, plus per-run artifact files such as the 3D scene descriptor (there is no
live connection to the system-under-test).

Which panels appear, where they sit, and where each gets its data are declared in the
``.vast`` under a top-level ``visualization.panels`` list — the campaign author defines
the view once and every run of the campaign replays through it:

.. code-block:: yaml

   visualization:
     panels:
       - playback:                            # transport bar; defaults to full-width bottom
       - costmap:
           title: Nav2 costmaps
           position: { anchor: top-right, width: 440, height: 440 }
           minimizable: true
           layers:
             map:    { topic: /map }
             global: { topic: /global_costmap/costmap }
             local:  { topic: /local_costmap/costmap }
             poses:  { table: poses }         # TF source for placing/moving layers
       - scenario_tree:
           position: { anchor: left, width: 320 }
           source: { table: behaviors }

Each panel entry is a single-key mapping — the key is the panel **type** (selecting the
panel plugin) and its value holds that panel's fields: an optional ``title``, a
``position`` (an ``anchor`` — ``bottom``/``top``/``left``/``right``, a corner, ``center``,
or ``fill`` for a full-view background — plus ``width``/``height`` in pixels or a ``"40%"``
string), the toggles ``minimizable``/``minimized``/``hidden``, and panel-specific **data
bindings** (which ``data.db`` table or recorded topic each piece of data comes from). Any
field you omit falls back to the panel type's built-in default, so a bare ``playback:`` on
its own is a complete panel. In this first version the layout is exactly as the ``.vast``
declares it (minimize/toggle work; drag-resize is not yet persisted).

The built-in panels:

**Playback** (``playback``) — a transport bar spanning the bottom: a click-to-seek
progress bar, an icon play/pause, a **2×** fast-forward toggle, and a ``current / total``
time label. It owns the clock; every other panel follows it. The timeline range comes from the run
capture's own time base when a ``scene3d`` panel declares one (the run's ground truth, and available
before any postprocessing), else from an explicit ``visualization.timeline``, else from the union of the
postprocessed ``poses`` / ``behaviors`` / ``scenario_timestamps`` timestamps.

**Costmaps** (``costmap``) — an rviz-style top-down view of what nav2 saw: the static
map, the global and local costmaps, the **actual path the robot drove**, and the robot
marker, all at the current time (scroll to zoom, drag to pan). Each ``layers`` entry
binds a name to a costmap **topic**; ``poses`` (the TF table) both places the layers into
the map frame and provides the driven-path trail + robot pose. It requires the
:ref:`costmap postprocessing step <costmap-delivery>` — if the ``costmaps`` data is
missing the panel says so rather than drawing nothing. *This panel ships with the*
``robovast_nav`` *package* (not the core UI) as a package-provided panel — see below — so
it is available whenever ``robovast_nav`` is installed; the ``.vast`` still references it
as plain ``- costmap:``.

**Scenario tree** (``scenario_tree``) — an rviz-scenario-execution-style behaviour tree
that colours each node by its status (running / success / failure) at the current time.
It reads the ``behaviors`` table, written by ``scenario_execution`` on every run (no ROS
required) unless ``execution.bt_log`` turns it off. Where the data supports it, each node
also shows its kind (sequence / selector / parallel / decorator), its feedback message at
the current time, and — on hover — its class and the ``.osc`` file and line it came from;
when a tree ends in failure the panel names the action responsible, via ``tip_id``.

The panel renders *any* table in the ``behaviors`` schema — point it at another with
``source: { table: <name> }``. Columns a table does not have are simply not shown, so an
older or differently-produced table still renders.

**nav2's behavior tree** uses exactly that: the ``nav2_behaviors`` table produced by the
:ref:`nav2 BT postprocessing <configuration>` (``rosbags_nav2bt_to_csv`` +
``nav2_bt_tree``) — node status over time from nav2's ``/behavior_tree_log``, tree
structure from the BT XML nav2 ran — displayed by this same panel with
``- scenario_tree:`` and ``source: { table: nav2_behaviors }``. The ``robovast_nav``
package ships no tree panel of its own. See :repo_link:`configs/examples/basic_nav` for a
complete campaign.

**3D scene** (``scene3d``) — the 3D world view, typically the run view's full-bleed **base layer**
(``position: { anchor: fill }``): the simulated world's actual geometry rendered in the browser
(orbit/zoom with the mouse), with **everything that moved** replayed — including *articulation*, so an
arm bends rather than swinging as one rigid piece.

It needs no bindings at all — ``- scene3d:`` on its own is a complete panel — because the run's
**capture** names the world it used and the service builds the matching **geometry** on demand. Both
artifacts are specified in :ref:`run-capture`.

*Geometry is compiled when somebody looks, not when a campaign runs.* A descriptor is 13–31 MB and takes
5–9 s to compile, for an artifact whose only consumer is this panel — so a campaign no longer ships one.
On the first view the service compiles it **inside that campaign's own pinned image** (the world is
generally installed there from a wheel, and a host that merely happens to have the tooling could be a
different version, which renders plausible but wrong geometry) and caches it keyed by *world identity*:
image digest + world reference + ``world_overrides``. Every later view is a read from disk — and so is
the first view of **every other run, and every other campaign that used the same world**. A 25-run sweep
compiles once, not 25 times.

Because a build is seconds (≈8 s on a warm cluster node) and can be a couple of minutes when the node
must first pull the image, the panel **names what it is waiting for** rather than spinning: *Fetching the
simulation image onto the node*, *Compiling the world geometry*, *Copying the scene back from the
container*. The rest of the run view stays usable meanwhile — the capture, the timeline and the
table-fed panels need no geometry, so playback and the costmap keep working — and a failure stops polling
and shows its reason.

Nothing is listed under the panel. A capture's tracks name the joints and bodies they drive exactly as
the descriptor spells them, so what gets animated is discovered from the artifact pair; a track matching
nothing is reported, with the capture's own ``world`` and ``producer``, rather than leaving a silently
static world.

.. note::

   Two earlier shapes are gone. The panel used to animate from the postprocessed ``poses`` table
   (``rosbags_tf_to_csv``): that needed a rosbag before anything moved, imposed a naming contract on the
   simulator plus a ``bind`` list for its exceptions, and could only place bodies parented to the world —
   so an articulated robot replayed rigid. ``scene.scope``/``capture.scope`` go with it: once geometry is
   resolved by content key there is nothing to declare, and nothing to declare *wrongly* (a
   campaign-scope descriptor aimed at a world that varied per configuration rendered confidently wrong
   geometry, and no validation could catch it). The ``poses`` table is unaffected and still serves the
   costmap panel and ``timeseries``.

   ``execution.generate`` remains supported for a campaign that wants its descriptor *frozen into its
   results* — an archive that must replay even without the image — but it is no longer how the run view
   obtains geometry.

**2D scene** (``scene``) — a top-down/side 2D plot of "where the thing is right now": one
column against another (e.g. a quadrotor's ``x`` vs altitude ``z``) from any table with a
time column (``source``, ``x``, ``y``; ``trail: false`` disables the driven path).

**Time series** (``timeseries``) — a chart of one or more numeric columns over the run's
timeline with a cursor at the current time (``source`` + a ``series`` list of
``{ column, label }``).

**State** (``state``) — the current numeric values of selected columns as labelled
read-outs (``source`` + ``fields`` of ``{ column, label, unit }``).

Custom and package-provided panels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The panel types above are the ones bundled into the core UI, but the run view is not
limited to them. A panel can also be loaded **at runtime** from outside the core UI as a
`Module-Federation <https://module-federation.io/>`_ remote — a small pre-built JavaScript
bundle. This is how the ``costmap`` panel ships from ``robovast_nav`` rather than the core
UI, and it is also how *you* can add a bespoke visualization to a run view. Either way, in
the ``.vast`` a panel is always referenced by a **type name** the same way — where the
code lives is invisible to the config:

* **Package-provided** — an installed plugin package registers a panel in the
  ``robovast.panel_types`` entry-point group and ships its built bundle as package data.
  Reference it by its registered type name (``- costmap:``); it is available to every
  campaign once the package is installed.
* **User-authored** (``custom``) — you build your own panel bundle and drop it next to the
  ``.vast``, referencing it by path:

  .. code-block:: yaml

     visualization:
       panels:
         - custom:
             remote: panels/my_panel        # dir (or remoteEntry.js) relative to the .vast
             module: ./myPanel              # the exposed module (default ./panel)
             title: My view
             position: { anchor: right, width: 420 }
             # any further keys are panel-specific data bindings (as for built-ins)

**Writing a panel.** A panel is a React component implementing the same contract the
built-ins use — ``({ spec, clock, data }) => JSX`` — so it is time-synced and reads the
run's ``data.db`` exactly like a built-in: ``clock.t`` / ``clock.subscribe(...)`` for the
current playback time, ``data.series(table)`` / ``data.nearest(table, t)`` for run rows,
``spec.config`` for the panel's ``.vast`` bindings. A panel needing a specialized endpoint
(as the costmap panel needs nav2 grids) reaches it through the generic run-scoped
``data.fetchRun(endpoint, params)`` — the run view core stays free of panel-specific
knowledge. Build the component as a Module-Federation remote exposing that module, with
``react``/``react-dom`` marked as **shared singletons** pinned to the host's version
(``^18``); a broken or missing bundle shows an inline error, never a silent blank. See
:repo_link:`src/robovast_nav/web` for the reference build (the costmap panel) to copy from,
and the developer guide for the internals.

**Several panels in one package.** A package that ships more than one panel builds them into
a **single Module-Federation container** exposing one module per panel, and each panel's type
class sets a shared ``REMOTE_NAME`` (the container name) so the service points every type at
the one bundle. Adding a panel is then one more ``exposes`` entry plus one panel class — no
new build, and React/vendor chunks stay shared.

**Before shipping a panel at all**, check whether a built-in one already renders your data.
A package that merely produces a *table in an existing schema* needs no panel: it points a
built-in one at its table with ``source: { table }``. That is what ``robovast_nav`` does for
``nav2_behaviors`` — it ships the postprocessing that builds the table and no tree panel.
A remote is for a panel the host genuinely cannot serve, like ``costmap`` with its own
binary-grid endpoint.

**Serving a panel's data.** A panel reads the run's postprocessed ``data.db`` through ``data``
(``fetchRun`` for anything beyond plain table rows). When that data comes from a table your own
**postprocessing** step produced and needs custom serving (untruncated blobs, nearest-frame
selection, …), a package can also ship the endpoint: a small class registered in the
``robovast.service_endpoints`` entry-point group, serving ``GET /campaigns/{id}/<name>`` from the
campaign's data — no core change, and it works the same on local ``vast serve`` and the in-cluster
service. So an analysis package can own the whole chain end-to-end — *postprocessing step →
service endpoint → panel* — with nothing in core. The costmap panel is exactly this: its
``rosbags_costmap_to_csv`` step, its ``costmap`` endpoint, and its panel all ship in
``robovast_nav``. (Large binary artifacts don't even need an endpoint — serve them as ordinary files
via ``data.runFileUrl(path)`` for one run's, or ``data.campaignFileUrl(path)`` for one the whole
campaign shares, as the ``scene3d`` panel does.) See the developer guide for the endpoint contract.

.. _scene-descriptor-delivery:

**3D scene data delivery.** The ``scene3d`` panel renders a **scene descriptor** — ``scene.json`` +
``scene.bin`` + one PNG per texture, a compact browser-renderable export of the simulated world, defined
in :ref:`run-capture` and produced for rst by ``rst/export_web.py``. It is a *directory*, not a file: the
loader fetches ``scene.bin`` and the textures as **relative siblings** of ``scene.json``.

A campaign does not deliver it. The service resolves it per view:

.. code-block:: text

   GET  /campaigns/{id}/scene?config_name=&run_id=   status only; never starts a build
   POST /campaigns/{id}/scene/run                    the explicit trigger
   GET  /campaigns/{id}/scene_assets/{key}/{file}    the bytes, from the shared cache

The split matters: a ``GET`` that started a build would fire on a browser prefetch or a React
strict-mode double render, and each of those would launch an image pull. Status is modelled on
``data-status`` (*say why you are about to wait, before you wait*), starting work is a ``POST`` returning
``ActionResult`` as ``postprocessing/run`` is, and the bytes are served like a panel bundle because they
live in the service's cache rather than in the campaign's results. The cache key is in the asset path so
one URL prefix addresses the whole entry, which is what makes the loader's sibling fetches resolve.

The cache is **shared across campaigns** and durable (``~/.robovast/cache/scenes``, overridable with
``ROBOVAST_SCENE_CACHE``; size-capped by ``ROBOVAST_SCENE_CACHE_BYTES``, evicted whole-entry
least-recently-used). Two consequences worth knowing:

* A campaign whose image is gone — garbage-collected, or a mutable tag rebuilt under the same name —
  cannot have its geometry rebuilt, and the panel says so rather than showing an empty world. A campaign
  that records only a mutable tag refuses to cache at all, because an entry keyed on a tag may silently
  describe different bytes later.
* A downloaded or shared campaign has no descriptor in it. If a self-contained archive matters more than
  laziness, keep an ``execution.generate`` entry for that campaign.

.. _costmap-delivery:

**Costmap data delivery.** Occupancy grids cannot be flattened into ``data.db`` columns
usefully (a grid becomes thousands of per-cell columns). Instead the
``rosbags_costmap_to_csv`` postprocessing step stores each grid **losslessly and
compactly** — its int8 cells zlib-compressed — into a ``costmaps`` table, together with
the geometry (resolution in m/cell, width/height in cells, so the map spans
``width×resolution`` by ``height×resolution`` **meters**, and the origin pose). Record the
costmap topics in the scenario and add the step to postprocessing:

.. code-block:: yaml

   results_processing:
     postprocessing:
       - rosbags_tf_to_csv: { frames: [base_link, odom] }   # base_link=robot/path, odom=local costmap
       - rosbags_costmap_to_csv:
           topics: [/map, /global_costmap/costmap, /local_costmap/costmap]

The run-view costmap panel fetches the frame nearest the current time from the campaign
``costmap`` endpoint (delivered untruncated) and inflates it in the browser. The same
geometry is visible to an LLM via MCP ``describe_campaign_data`` (the ``costmaps`` table
description carries the map's size in meters, resolution, layers, and delivery), so it can
reason about the run without decoding grids.

Development
-----------

For UI development with hot reload, run the Vite dev server against a running
service:

.. code-block:: bash

   vast serve                # in one terminal (the service to talk to)
   cd ui && npm run dev      # in another (Vite on :5173)

The dev server proxies the API path prefixes to the service so the browser stays
same-origin (no CORS). Point it at a different service with
``ROBOVAST_SERVICE_URL``. See :ref:`web-ui-internals` in the developer guide for
the app's structure and how to extend it.
