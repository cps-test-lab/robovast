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
  **live log** panel. The log is the campaign's unified *infrastructure* log — the
  variation (config generation), run (controller) and postprocessing phases assembled
  into one stream with ``===== PHASE =====`` dividers — streamed live from the service
  and shown in full once it finishes. The browser equivalent of
  ``vast exec cluster monitor``.
* **Launcher** — starts a campaign from a workspace (which ``.vast``, config filter,
  runs per configuration, postprocess toggle) and watches its live status. The
  browser equivalent of ``vast exec cluster run``.
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
   port, so if ``vast ui`` (local) or ``vast ui --cluster`` (a held-open tunnel)
   is up, the command rides that same endpoint — the exact one the browser is on:

   .. code-block:: bash

      # (a) vast ui --cluster running in another terminal → this follows it:
      vast workspace init configs/examples/growth_sim
      #> Target: service (http://127.0.0.1:8800) [detected — following a running vast serve/ui]

      # (b) nothing running → this machine, in-process:
      vast workspace init configs/examples/growth_sim
      #> Target: this machine, in-process (store: …)

      # (c) reach the cluster with NO vast ui open → open your own ephemeral tunnel:
      vast workspace init configs/examples/growth_sim --cluster
      #> Target: in-cluster service (http://127.0.0.1:…)

   So you rarely type ``--cluster``: keep a ``vast ui --cluster`` open and every
   ``vast workspace`` command follows it automatically. Use ``--cluster`` only to
   reach the cluster when no ``vast ui`` tunnel is up (it opens an ephemeral
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

``vast ui`` is the one command for "give me a working UI", and the ``--cluster``
switch is the only thing that changes between local and cluster:

.. code-block:: bash

   vast ui                    # this machine: starts the service if none is up, opens the browser
   vast ui --cluster          # the in-cluster service: tunnels in, opens the browser
   vast ui --cluster -x prod -n robovast   # ...a specific context / namespace

* **This machine** (no ``--cluster``) — if a ``vast serve`` is already running
  here, ``vast ui`` just opens the browser at it; otherwise it **starts the
  service in-process** (local backend) and serves the UI itself. So you never
  have to run ``vast serve`` by hand for local use — that command stays as the
  headless primitive for a VM, a script, or the in-cluster pod.
* **Cluster** (``--cluster``) — wraps ``kubectl port-forward
  svc/robovast-service`` and opens the browser at it; ``-x/--context`` and
  ``-n/--namespace`` pick which cluster. Needs ``kubectl`` + a kubeconfig, runs
  in the foreground, and Ctrl-C closes the tunnel.
* **Remote VM** — the service binds ``127.0.0.1`` there, so reach it with your
  own SSH tunnel (``ssh -N -L 8800:127.0.0.1:8800 <vm>``) and open
  ``http://127.0.0.1:8800``. Because that is the conventional port, ``vast ui``
  and every other command auto-detect the tunnel — nothing to export.

Because the service serves the **web UI and the REST API on the same port**,
whatever ``vast ui`` opens is all a browser, the ``vast`` CLI, and the MCP server
need. Other service-touching commands (``vast workspace …``) reach the same place
with the same ``--cluster`` switch, so nothing needs to be exported.

A connection indicator in the top bar turns green and shows the service version
once the handshake succeeds.

.. note::

   The service is **unauthenticated in v1** and must stay behind the
   localhost / SSH-tunnel / ``kubectl port-forward`` boundary — do not expose it
   directly. Public access (Ingress + token/TLS) is a deferred, whole-surface
   decision (see :ref:`deployment`).

Results viewer
--------------

The **Results** tab explores a finished campaign's data. Pick a campaign, and the
left panel lists the tables in its ``_execution/data.db`` — one per metric CSV, plus
the ``runs`` **dimension table** (per-run ``status``/``duration_s`` and each scenario
parameter as a ``param_*`` column), with ``campaign.db`` attached as schema
``campaign``. Write **read-only SQL** in the editor and **Run** it; the result shows
as a table and, via the chart builder, as a chart — pick *x* / *y* / *color* columns
and a mark. Join ``runs`` to any metric table on ``(config_name, run_id)`` to answer
"how does *<param>* affect *<metric>*".

Unlike ``vast eval gui``, the web viewer uses **SQL + charts** rather than executing
Jupyter notebooks (the kernel would have to run server-side; that integration is
future work — the desktop GUI remains for notebook analysis).

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
