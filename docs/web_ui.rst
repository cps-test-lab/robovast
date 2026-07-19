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
  **live log** panel. The per-batch run bar also distinguishes **finished** runs (the
  solid fill) from those **currently running** (a lighter segment on top), with the
  exact ``running N · pending M`` counts beside it. While a fixed-size campaign runs,
  an **ETA** (``~12m left``) appears next to that count once at least one run has
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
  local service's results are already on its filesystem). The browser equivalent of
  ``vast exec cluster monitor``.
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

.. tip::

   To skip the upload entirely and always have a project available — even across
   restarts — pin its directory at launch (local backend only):

   .. code-block:: bash

      vast serve --workspace-dir configs/examples/ros2_basic

   The directory is used **in place** as a **read-only** workspace: it appears in
   the dropdown the moment the service starts, with a path-stable id so its UI
   link keeps working after a restart. Edit the files on disk to change it —
   writes through the service/UI/MCP are refused (campaign outputs still land in
   the shared results store, never under the pinned dir). Repeat ``--workspace-dir``
   to pin several; each is named after its directory. Hidden files and ``results/``
   are skipped, exactly like ``workspace init``.

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
capacity vs. summed pod requests for an in-cluster service. Hovering the chip
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

.. note::

   A campaign only becomes queryable once **analysis postprocessing** has run — it
   builds ``data.db`` from the raw rosbags. Launching with **Postprocess when done**
   (the default) runs it automatically on both backends; otherwise the Results tab
   offers a **Run postprocessing** button, and the CLI equivalent is
   ``vast results postprocess --campaign <id>``. The rosbag→CSV step always runs in
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
the same instant. All panels read only the run's postprocessed ``data.db`` (there is no
live connection to the system-under-test).

Which panels appear, where they sit, and where each gets its data are declared in the
``.vast`` under a top-level ``visualization.panels`` list — the campaign author defines
the view once and every run of the campaign replays through it:

.. code-block:: yaml

   visualization:
     panels:
       - type: playback                       # transport bar; defaults to full-width bottom
       - type: costmap
         title: Nav2 costmaps
         position: { anchor: top-right, width: 440, height: 440 }
         minimizable: true
         layers:
           map:    { topic: /map }
           global: { topic: /global_costmap/costmap }
           local:  { topic: /local_costmap/costmap }
           poses:  { table: poses }           # TF source for placing/moving layers
       - type: scenario_tree
         position: { anchor: left, width: 320 }
         source: { table: behaviors }

Each panel entry has a ``type`` (selecting the panel plugin), an optional ``title``, a
``position`` (an ``anchor`` — ``bottom``/``top``/``left``/``right``, a corner, ``center``,
or ``fill`` for a full-view background — plus ``width``/``height`` in pixels or a ``"40%"``
string), the toggles ``minimizable``/``minimized``/``hidden``, and panel-specific **data
bindings** (which ``data.db`` table or recorded topic each piece of data comes from). Any
field you omit falls back to the panel type's built-in default, so ``- type: playback`` on
its own is a complete panel. In this first version the layout is exactly as the ``.vast``
declares it (minimize/toggle work; drag-resize is not yet persisted).

The built-in panels:

**Playback** (``playback``) — a transport bar spanning the bottom: a click-to-seek
progress bar, an icon play/pause, a **2×** fast-forward toggle, and a ``current / total``
time label. It owns the clock; every other panel follows it. The timeline range is taken
from the run's recorded timestamps (``poses`` / ``behaviors`` / ``scenario_timestamps``).

**Costmaps** (``costmap``) — an rviz-style top-down view of what nav2 saw: the static
map, the global and local costmaps, the **actual path the robot drove**, and the robot
marker, all at the current time (scroll to zoom, drag to pan). Each ``layers`` entry
binds a name to a costmap **topic**; ``poses`` (the TF table) both places the layers into
the map frame and provides the driven-path trail + robot pose. It requires the
:ref:`costmap postprocessing step <costmap-delivery>` — if the ``costmaps`` data is
missing the panel says so rather than drawing nothing.

**Scenario tree** (``scenario_tree``) — an rviz-scenario-execution-style behaviour tree
that colours each node by its status (running / success / failure) at the current time.
It reads the ``behaviors`` table, which comes from ``rosbags_bt_to_csv`` on the
``/scenario_execution/snapshots`` topic. If that topic was not recorded in the scenario's
``bag_record(...)`` action, the panel shows exactly that, with the fix.

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
       - rosbags_bt_to_csv                                   # scenario tree
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
