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
  cluster — with its status; expanding a running one streams that **job's own live log**:
  every container it runs, merged into one stream, each line tagged ``[<container>]``
  and colored per container when the job has more than one. That matters in the ROS
  shape, where the simulator and the system under test have their own containers and a
  failure is only legible when their output is read against the scenario's. The campaign **live log** panel below is the
  campaign's unified *infrastructure* log — the variation (config generation), run
  (controller) and postprocessing phases assembled into one stream with
  ``===== PHASE =====`` dividers — streamed live and shown in full once it finishes.
  **Stop** cooperatively ends the campaign *and* terminates its in-flight jobs, so
  running work halts promptly (not only after the current batch). A finished cluster
  campaign also shows a **Download** button that streams its postprocessed
  ``tar.gz`` straight from the object store (offered only for a cluster service — a
  local service's results are already on its filesystem). A finished campaign's
  actions menu offers **Retrigger campaign**, which starts a **new** campaign from
  what this one recorded — its frozen ``_config/`` and the image its runs actually
  used — rather than from the workspace it was launched from, which may be gone or
  may have moved on. The source campaign is untouched, so this works whatever state
  it ended in, and the new campaign appears at the top of the list with a description
  naming the one it came from. It replays the recorded launch
  (``_execution/launch.yaml``), so re-running a one-config pilot stays a one-config
  pilot. A campaign that never recorded a usable image is refused rather than rebuilt
  from a guess: a campaign's build context is not archived in its results, so the
  refusal names the container and points back at the workspace.
  The same menu offers **Retrigger postprocessing**, which opens a dialog to *adapt
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
**Data browser** (ad-hoc SQL + charts). Each carries a small icon, used in the sidebar
and again on the campaign cards.

**The campaign is part of the URL**: ``#/results/<view>/<campaign_id>``. So a Results
view is linkable and survives a reload, switching sub-view keeps the campaign, and each
campaign card in **Campaigns** carries shortcut buttons — left of its gear — that jump
straight into the Explorer or the Run view *for that campaign*. A card only offers what
it can deliver: the Explorer button once the campaign is finished **and** postprocessed
(the same gate the Results tab itself applies), and the Run view button only if the
campaign also recorded runs to replay. Changing the campaign inside a view updates the
URL without adding a browser-history step, so **Back** always returns to where you came
from in one press.

**Explorer.** The tree shows each campaign's configs and runs with a pass/fail status
dot; selecting a node opens its details on the right. When a campaign declares
:ref:`evaluation notebooks <evaluation-notebooks>` under
``evaluation.visualization``, each workload appears as a **tab**, and the notebook for
the selected node's level (campaign / config / run) is executed server-side and shown
as a rendered HTML page — the web equivalent of ``vast eval gui``. The notebook's
``DATA_DIR`` is set to the selected node's directory (the same contract the desktop
tool uses), so the *same* notebooks work in both. Output is cached, so re-selecting a
node is instant. Selecting a campaign here also selects it for the other two sub-views
(it is the shared, URL-carried selection); the config or run picked below it is the
Explorer's own.

After the declared workloads comes a built-in **Log** tab, which needs nothing in the ``.vast``.
It shows the same merged log as the run-view panel — same filters, same colours — over whatever
the selected node scopes to, and it is where the *cross-run* question lives, because a run view
can only ever show one run:

* a **run** node — that run's lines;
* a **config** node — every run of it, with a ``run_id`` column;
* the **campaign** — a search across every run at once, reporting hits per run joined to each
  run's verdict, so "which runs logged this, and did they fail?" is one query. Click a row to
  read that run's log, and the trail back to the search stays.

There is no playback clock here, so the view drops the greying and the jump button rather than
implying a position it does not have.

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

.. _declared-plots:

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

These are **campaign-scoped**: one query across every run, rendered in the Data browser. For the
same Vega-Lite authoring against a **single run**, on the replay timeline and with a playback cursor,
see the :ref:`vega run-view panel <vega-panel>`.

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

The run picker lists only campaigns that actually **recorded runs** (``num_runs > 0``,
tallied from ``campaign.db``). A campaign that never started, or that ended before its
store was written, has nothing to replay, so it is not offered here at all — rather
than being selectable and then answering with an empty view.

.. _shutdown-toggle:

**The run ends at its scenario's verdict.** The gear at the right of the header opens the run
view's settings, and its **Include shutdown phase** entry decides whether "this run" means the
trial or the whole recording. It is **off by default** — everything after the verdict is teardown:
nodes being killed, lifecycle transitions failing because their peer is already gone, TF errors
from a publisher that has stopped. It is minutes of wall time, it colours the log red, and it
describes nothing that happened during the run.

It governs the view rather than a panel, which is why it sits in the header and neither the
playback bar nor the log panel carries a toggle of its own: with the shutdown hidden, the
timeline stops at the verdict (so the duration readout is the trial's) and the log stops there
too. Tick the entry and the full recording returns, with a divider on the playback bar marking
where the trial ended.

A menu rather than a bare icon because it is a *view-wide* setting, the same shape the campaign
row's gear uses: the header is otherwise a row of labelled controls, and each further such
setting would add another icon to decode. One entry today, named in words, with room to grow.

The moment itself is read from ``scenario_timestamps``, written once by postprocessing —
the same row ``search_run_logs`` cuts on, so the web UI and the MCP tools cannot disagree
about where a run ended. A run that reached no verdict, and a campaign postprocessed before
the verdict was recorded, leave the control disabled with that reason on hover: nothing is
trimmed, rather than trimmed to a guess.

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
``position`` (an ``anchor`` — ``bottom``/``top``/``left``/``right``, a corner,
``bottom-center``, ``center``, or ``fill`` for a full-view background — plus
``width``/``height`` in pixels or a ``"40%"`` string), the toggles ``minimizable``/``minimized``/``hidden``/``fixed``, and panel-specific
**data bindings** (which ``data.db`` table or recorded topic each piece of data comes from).
Any field you omit falls back to the panel type's built-in default, so a bare ``playback:``
on its own is a complete panel.

``bottom-center`` differs from ``bottom`` in one way that matters: ``bottom`` **docks** at the
very edge and reserves its height, which is what the playback bar does, so a second ``bottom``
panel would cover it. ``bottom-center`` *floats* above that reserved band, centred, and needs a
declared ``width`` (a full-width one is just ``bottom``). Because its bottom edge is the pinned
one, ``minimized`` collapses it down to its header at the edge and expands it back upward.

**Some panels are contributed by the simulator backend and need no entry at all.** A backend
whose runs always record the capture the ``scene3d`` panel replays supplies that panel the same
way it supplies the environment that produces the capture — there is nothing to decide, so
there is nothing to declare. Declaring it yourself still wins, which is how you place it
somewhere other than the full-bleed base layer.

The declared layout is where the panels **start**, not where they are stuck:

* **Move** — press the mouse on a panel's **title bar**, drag, and release to drop it there.
  A moved panel is raised above the others, and always keeps an edge inside the view so it
  can be grabbed back.
* **Resize** — drag a panel's **free edge or corner**: the one the anchor does not pin, which
  is the edge facing the middle of the view (a ``left`` column is dragged by its right edge,
  a ``bottom-right`` panel by its top-left corner). The anchored side stays where it is, so
  resizing never fights the layout. A ``left``/``right`` column with no declared ``height``
  spans the view; dragging its bottom edge is what gives it one.

Panels that show no title bar cannot be moved — the docked ``playback`` transport and a
``fill`` background such as ``scene3d`` stay put. Everything else is movable and resizable
unless the ``.vast`` says otherwise: ``fixed: true`` locks a panel's geometry outright, and
``resizable: false`` is the narrower opt-out for one that may be moved but not resized.

These are **view-local** adjustments: they last as long as the view is open, and reloading it
(or switching runs) restores the layout the ``.vast`` declares. To change the layout for good,
edit it under **Edit visualization**.

The built-in panels:

**Playback** (``playback``) — a transport bar spanning the bottom: a click-to-seek
progress bar, an icon play/pause, a **2×** fast-forward toggle, and a ``current / total``
time label. It owns the clock; every other panel follows it. The timeline range comes from the run
capture's own time base when a ``scene3d`` panel declares one (the run's ground truth, and available
before any postprocessing), else from an explicit ``visualization.timeline``, else from the union of the
postprocessed ``poses`` / ``behaviors`` / ``scenario_timestamps`` timestamps.

That range is the whole **recording**; where the trial ended is a separate figure, so the
:ref:`shutdown toggle <shutdown-toggle>` can shorten the timeline and restore it without
re-querying anything. While the shutdown phase is shown, the bar draws a full-height divider at
the verdict — with it hidden the verdict *is* the end of the bar, and a line there would mark
nothing.

**Costmaps** (``costmap``) — an rviz-style top-down view of what nav2 saw: the static
map, the global and local costmaps, the **actual path the robot drove**, and the robot
marker, all at the current time (scroll to zoom, drag to pan). Each ``layers`` entry
binds a name to a costmap **topic**; ``poses`` (the TF table) both places the layers into
the map frame and provides the driven-path trail + robot pose. It requires the
:ref:`costmap postprocessing step <costmap-delivery>` — if the ``costmaps`` data is
missing the panel says so rather than drawing nothing.

A layer is left out, and named in the top-left corner (*"local: nearest frame 4.3 s away"*),
when the recording genuinely has no frame near the cursor — before nav2 starts publishing,
after it stops, or across a gap mid-run — rather than showing the closest frame it could find
as though it were current. Each layer is judged against **its own** publish rate, so a static
map, published once and never again, is never affected.

While scrubbing, layers keep showing their last frame instead of blanking: a replacement is
already being fetched, and the layers differ enough in weight (a full-map global costmap is
~15 KB against a local costmap's sub-KB) that blanking during the catch-up would make the
heaviest one flicker. Note also that a costmap is placed at the time it was published, so at
speed the robot marker can sit slightly ahead of its window — that offset is the recording's
own resolution, not drift. *This panel ships with the*
``robovast_nav`` *package* (not the core UI) as a package-provided panel — see below — so
it is available whenever ``robovast_nav`` is installed; the ``.vast`` still references it
as plain ``- costmap:``.

.. _camera-panel:

**Camera** (``camera``) — a camera that was **recorded during the run**, played on the
playback clock. This is what a simulator with no 3D scene has instead of one: Gazebo writes
no run capture and has no scene exporter, so a :ref:`scene3d <scene3d-panel>` panel has
nothing to replay there, while a monitor camera spawned into the world gives that run view a
picture of the trial.

It needs no bindings — a bare ``- camera:`` is a complete panel whenever the run registered
exactly one video, the same promise ``scene3d`` makes. ``source: { topic: … }`` picks one when
a run recorded several; ``source: { path: …, t0: … }`` is the escape hatch for a video no
producer registered, and needs ``t0`` because a file with no entry in the ``videos`` table
carries nothing that says where it sits on the timeline.

The panel is a **reader** of the clock and never a writer, so it shows no controls of its own:
the :ref:`playback <run-view>` bar owns time and this follows it, including at 2×. Seeking
happens only when the element drifts more than about one frame from the cursor, so ordinary
playback is not a seek storm. Outside the recording — a camera that came up late, a trial that
ran past the last frame — it dims and says *"No frames at this time"* rather than showing
frame 0 as though it were the current moment.

Where the video comes from: a producer writes it into the run directory and registers it in
the ``videos`` table. ``rosbags_to_webm`` is the first such producer (see the
:ref:`worked example <videos-table>`), but the table is a contract any of them may write.

Two properties worth knowing. The encode is **constant-rate** — ``fps`` is derived so the
first and last frames land exactly on their recorded moments, so only mid-run jitter drifts,
which is sub-second at a monitor camera's 1 Hz. And **seeking is efficient on the local lane**:
the file is served with ``FileResponse``, so the browser ranges into it. A cluster campaign
fetches the one object behind the address first, then serves it the same way.

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

**Run log** (``log``) — everything the run said, following the playback cursor. One row per log
event from every container, joined with ``/rosout`` and placed on the run's clock (see
:ref:`merged-run-log`). Lines not yet logged at the cursor are greyed out with a divider marking
"now", so the log's whole shape stays visible while the position in it is unambiguous.

Filtering is instant and client-side, over the whole loaded log: a text box (substring, or a
regular expression with the ``.*`` toggle), two severity chips that cycle
*off → highlight → only these*, and one dropdown listing every ``container``, ROS ``node`` and
``source`` **this run actually produced**, with counts. Clicking a line seeks playback to it, and
the ▲▼ buttons jump to the previous/next warning or error — which is what turns a filter into
navigation. Scrolling away stops the follow and raises a button showing the cursor's time; click
it (or press ``Escape``) to jump back and resume following.

The log also stops at the scenario's verdict. In the run view that is the header gear's
:ref:`Include shutdown phase <shutdown-toggle>` entry and this panel shows no control of its own
— one question, one place to answer it. The Explorer's **Log** tab has no run view around it, so it
carries a power icon for the same setting in its own filter bar. Either way the cut is on the
verdict's **wall** time and not its sim time: the clock map does not extrapolate, so lines after
``/clock`` stopped have no sim time at all, and a sim-time cut is blind to exactly the lines
the toggle exists to remove.

Wall time is also what the log is **ordered** by, which is why those lines sit at the end where
they were logged — and not sim time with the rows lacking one placed first, which would read a
missing sim time as "logged before the simulator's clock started". It does not mean that: the
clock map is silent at *both* ends of its range, so such an order lands a run's shutdown at the
very top of the log, next to the boot lines.

A line with no sim time is **dimmed** in the time column, and that is the only marking it gets:
the figure is the seconds since the run's first log line, measured on the wall clock, and hovering
it says so. Nothing is prefixed to it, so a column of monospace figures keeps its alignment.

A log covering more than one run (a config's, or the Explorer's whole campaign) is ordered by run
first, so it reads as one run after another instead of interleaving runs that each start at zero.
There is no cursor in that view: every run has its own moment ``12.5 s``, so a single position
cannot point into all of them, and the log is shown plain rather than divided at an arbitrary row.

The footer never stays silent about what is missing: no ``run_log`` table (postprocessing predates
it), a run with no clock map (``wall time only``), how many shutdown lines were hidden and how the
scenario ended, how many lines the filter hid, and whether the load hit its ceiling.

The playback bar itself gains tick marks for every warning and error — full height for errors,
half for warnings — so the log's shape is visible *before* you scrub into it.

**Nav2 behavior tree** (``nav2_behavior_tree``) — the same tree view for **nav2's own**
behavior tree, reading the ``nav2_behaviors`` table produced by the :ref:`nav2 BT
postprocessing <configuration>` (``rosbags_nav2bt_to_csv`` + ``nav2_bt_tree``): node status
over time from nav2's ``/behavior_tree_log``, tree structure from the BT XML nav2 ran.
Declare it as ``- nav2_behavior_tree:`` and it brings its own table, title and — when the
table is absent — the nav2 postprocessing steps to add, rather than the scenario's
``bt_log``.

*This type ships with the* ``robovast_nav`` *package*, so it is available whenever that
package is installed, but it is not a second implementation: it renders the built-in panel
above with different defaults, and so gains its behaviour automatically. Both trees can be
shown at once — the scenario's says what the trial did, nav2's says why the navigator
recovered. See :repo_link:`configs/examples/basic_nav` for a complete campaign.

.. _scene3d-panel:

**3D scene** (``scene3d``) — the 3D world view, typically the run view's full-bleed **base layer**
(``position: { anchor: fill }``): the simulated world's actual geometry rendered in the browser, with
**everything that moved** replayed — including *articulation*, so an arm bends rather than swinging as
one rigid piece.

The mouse bindings:

- **wheel** — fly toward or away from whatever is under the pointer. You steer by aiming: point at a
  far shelf and scroll, and you arrive at that shelf. One notch covers the same fraction of the view
  at any distance, and there is no distance at which it stops — you can fly *into* a building whose
  camera was framed from outside it.
- **left-drag** — turn the view about a pivot held a fixed distance ahead, so it reads as looking
  around rather than circling a point that recedes as you approach it.
- **right-drag** — pan sideways and vertically.

The wheel deliberately does not shrink an orbit radius toward a fixed centre, which is the usual
default and is what makes such a view freeze a short way from its centre.

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

.. _vega-panel:

**Vega chart** (``vega``) — any diagram, declared as a `Vega-Lite
<https://vega.github.io/vega-lite/>`_ spec over one of the run's ``data.db`` tables. Where
``timeseries`` plots columns that *already exist*, a spec's ``transform`` can **derive** what the
run never recorded, and any Vega-Lite mark is available. It binds a ``source`` (the same
``{ table, time_column, filter }`` as ``timeseries``, so the run scope and the frame filter happen
in SQL) plus a ``vega_lite`` spec, and optionally ``max_rows`` (default 5000).

*Which of the two to pick:* ``timeseries`` is a hand-rolled canvas chart — the cheap path for
numeric columns at high sample rates. ``vega`` costs a full Vega render but expresses everything
else.

The spec is bound to two **named datasets** and so declares no ``data`` block of its own:

* ``table`` — the run's rows for that table;
* ``cursor`` — a single row ``{t}`` at the current playback time.

The playback cursor is layered in **automatically**, into the top-level spec and into each child of a
``vconcat``/``hconcat``/``concat`` — but only where it means something: the spec must be layerable
(``mark`` or ``layer``) and must bind the time column to ``x`` or ``y``. A boxplot by frame therefore
gets no cursor, and ``facet``/``repeat`` specs are left alone. Reference the ``cursor`` dataset
yourself to place it anywhere else.

Two things to know when charting a ``poses`` table, because every such spec hits them:

* **Dotted column names.** ``rosbags_tf_to_csv`` writes ``position.x`` / ``orientation.yaw``, and a
  Vega-Lite ``field`` reads a dot as a nested path. Either escape it (``position\.x``) or — usually
  clearer — hoist it to a flat name in a ``calculate`` transform: ``datum['position.x']``.
* **Every ``data.db`` column is TEXT.** The panel coerces each column whose values all parse as
  finite numbers, so ``type: quantitative`` works without a ``format.parse`` block.

A worked example over a ``poses`` table — derived speed above the raw pose, sharing one time axis, so
both charts get a cursor:

.. code-block:: yaml

   - vega:
       title: base_link
       position: {anchor: bottom-right, width: 460, height: 380}
       source: {table: poses, filter: {frame: base_link}}
       vega_lite:
         resolve: {scale: {x: shared}}
         transform:
         - {calculate: "datum['position.x']", as: px}
         - {calculate: "datum['position.y']", as: py}
         - window:                                  # previous sample, to difference against
           - {op: lag, field: px, as: px0}
           - {op: lag, field: py, as: py0}
           - {op: lag, field: timestamp, as: t0}
           sort: [{field: timestamp}]
         - filter: "isValid(datum.t0)"              # the first sample has no predecessor
         - calculate: >
             sqrt(pow(datum.px - datum.px0, 2) + pow(datum.py - datum.py0, 2))
             / max(datum.timestamp - datum.t0, 1e-6)
           as: speed
         - window: [{op: mean, field: speed, as: speed_avg}]   # differencing TF is noisy
           frame: [-9, 0]
           sort: [{field: timestamp}]
         vconcat:
         - height: 150
           encoding:
             x: {field: timestamp, type: quantitative, axis: null}
             y: {field: speed_avg, type: quantitative, title: "speed [m/s]"}
           mark: {type: line, strokeWidth: 1.5}
         - height: 120
           transform: [{fold: [px, py], as: [series, value]}]
           mark: {type: line, strokeWidth: 1.5}
           encoding:
             x: {field: timestamp, type: quantitative, title: "t [s]"}
             y: {field: value, type: quantitative, title: pose}
             color: {field: series, type: nominal}

This is the same authoring language as the campaign-scoped :ref:`declared plots
<declared-plots>` above; the difference is scope and binding — ``evaluation.plots`` runs a SQL query
across the whole campaign and renders in the Data browser, while a ``vega`` panel binds one table of
one run and renders in the Run view.

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

**Before writing a renderer**, check whether a built-in panel already draws your data. A
package whose data is a *table in an existing schema* should still ship its own **type** — so a
``.vast`` names it and gets its table, title and empty-state guidance — but that type can
**derive** from a built-in panel instead of reimplementing one. The host passes its own panel
components to a remote as ``props.builtins``; ``robovast_nav``'s ``nav2_behavior_tree`` is a few
lines that render ``builtins.ScenarioTree`` with nav2's defaults, and inherits every later
improvement to it. Write a renderer only when the host cannot draw the data at all — ``costmap``,
whose binary grids need their own endpoint.

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
