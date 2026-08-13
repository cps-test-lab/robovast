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

* **Run** — author a project (``.vast``), check it, then start, monitor and stop
  campaigns on the local Docker backend or on a Kubernetes cluster.
* **Analyze** — inspect campaigns, configurations, runs, logs, and tabular run
  data (read-only).

That order is deliberate, and the server's MCP ``instructions`` say the same thing.
Introducing itself as an archive — the earlier text was "provides access to the results
created by RoboVAST" — is why assistants ran experiments by hand on the host and came
here only to read files afterwards. A hand-started simulator has no pinned image, no
recorded provenance and no repetitions, so its output cannot be compared with a
campaign's; the instructions say so, and so does ``start_campaign``. Two MCP prompts
cover the halves: ``run_experiments`` and ``analyze_campaigns``.

A campaign runs a **workspace's** ``.vast``: ``workspace_id`` is the only project
binding the service accepts, and ``config_path`` selects among several
``.vast`` files in that workspace. There is no server-side "current project" —
``.robovast_project`` / ``vast init`` bind the *CLI's* project (for
``vast exec local run``, ``vast results``, ``vast eval``) and never select what
the service runs. Get a ``workspace_id`` either by pinning a directory in place
with ``vast serve --workspace-dir <dir>`` (no upload; edits on disk are live —
only for a service running on that host) or by uploading one with
``create_workspace`` + ``update_workspace`` (required for a remote service
and in-pod services, where the directory does not exist locally).

The one exception is a **retrigger**: ``start_campaign(from_campaign=<campaign-id>)``
runs a *previous campaign's* frozen configuration and the image its runs actually used,
with no workspace involved at all. Campaigns are workspace-independent, and the workspace
one came from may be gone — its own ``_config/`` is the durable source of truth. It
produces a new campaign and leaves the source untouched, so it works whatever state that
campaign ended in, and it replays the recorded launch, so re-running a one-config pilot
stays a one-config pilot. It takes no other argument (passing one is an error rather than
being ignored), and it is refused when the campaign recorded no usable image: a campaign's
build context is not archived in its results, so launch it from its workspace instead.

A ``.vast`` file defines a **project**; a **campaign** is one execution of it; a
**config** is one scenario parameter set within a campaign.

``vast serve`` already exposes these tools at ``/mcp`` on its own port
(``127.0.0.1:8800`` by default) — see :ref:`deployment`. That is the right
choice whenever a ``robovast-service`` is running anyway, since it means one
port (and, remotely, one SSH tunnel) reaches the web UI, the REST API, and MCP
together. Pass ``--no-mcp`` to ``vast serve`` to turn that off.

``vast mcp serve`` below starts MCP as its **own** separate process, on its
own port. Reach for it only when that separation is actually wanted: a
``stdio`` transport for a locally-spawned client such as Claude Desktop (which
never goes over a port at all), or a deliberately standalone MCP endpoint
decoupled from ``vast serve``'s lifecycle.

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

Tools are grouped by **lifecycle phase** — where in a campaign's life you reach for them —
because that is the axis a caller actually navigates: you are authoring, or running, or
reading results. Each phase is one plugin, so the generated table below is also the map.

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Phase
     - What it covers
   * - ``files``
     - Reading and writing any file, in every phase. Not a phase but
       :ref:`one address space <mcp-files>`.
   * - ``authoring``
     - Before anything runs: create a workspace, put a ``.vast`` in it, check it, see what
       configurations it expands to, and ask the simulator :ref:`what its world offers an
       override <mcp-describe-world>`.
   * - ``execution``
     - Starting a campaign and watching it: status, logs, per-job view, capacity, stop.
       Building the experiment image lives here too — a build is part of a campaign's
       driven work, not a stage of its own. So does
       :ref:`testing a container <mcp-container-exec>`, which produces no campaign data.
   * - ``results``
     - Reading what a campaign did: :ref:`read-only SQL <mcp-analysis>` plus the campaign
       listing and one aggregate.
   * - ``results_lifecycle``
     - Acting *on* finished results: re-deriving them (postprocessing), publishing them,
       downloading, cleaning up, deleting.
   * - ``reference`` / ``docs`` / ``examples`` / ``plugin_metadata``
     - Reference material about RoboVAST itself: the config schema, the CLI, the
       documentation, worked examples, and what plugins are installed.

The grouping used to be a mix: some modules by scope (one per campaign / configuration /
run) and some by capability, with a 20-tool module holding everything else — so "which
module owns this?" had no answer, and the biggest one owned build, postprocessing, share,
deletion and download, none of which are execution control.

Names read ``<verb>_<resource>``: ``get`` retrieves, ``list`` enumerates, ``search``
filters, ``describe``/``query`` are the SQL pair, and ``validate`` / ``preview`` /
``start`` / ``stop`` / ``run`` / ``build`` / ``delete`` do what they say.

Two whole classes of question are deliberately *not* one tool per scope: files are
:ref:`one address space <mcp-files>`, and reading what a campaign did is
:ref:`read-only SQL <mcp-analysis>`.


.. _mcp-one-tool-per-question:

One tool per question, not per shape of answer
----------------------------------------------

Every tool description and JSON Schema is injected into the model's context on **every**
request, so the surface is a cost paid per turn rather than once. A read/list pair over
the same object charges twice for it, and additionally costs a round trip: the caller
must call the lister to learn the name the getter needs. So an **empty argument means
"all of them"**, and the pair is one tool:

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Call
     - Answers
   * - ``get_cli_help()`` / ``get_cli_help("exec cluster run")``
     - the command tree / one command's ``--help``
   * - ``search_docs()`` / ``(query=…)`` / ``(page=…)``
     - the page list / matching excerpts / one page in full
   * - ``get_example()`` / ``get_example("basic_nav")``
     - the catalogue / one project's files
   * - ``list_workspaces()`` / ``list_workspaces("ws-ab12")``
     - all workspaces / one
   * - ``list_plugins()`` / ``(group=…)`` / ``(query=…)``
     - the group catalogue / a group's plugins / a name search
   * - ``list_campaigns()`` / ``(running_only=True)``
     - every campaign / the live ones
   * - ``describe_campaign_data(id)`` / ``(preflight_only=True)``
     - the schema / just the fetch verdict, without reading it
   * - ``delete_campaign(id)`` / ``(id, data_only=True)``
     - remove the campaign / free only its object-store data
   * - ``nav_get_trajectory(…)`` / ``(…, stats_only=True)``
     - the points / distance, duration, speeds, bounding box
   * - ``nav_get_map_info(…)`` / ``(…, occupancy=True)``
     - map metadata / metadata plus cell counts

The same reasoning fixes the vocabulary. One concept has one argument name across the
surface — ``campaign_id``, ``config_name``, ``run_id``, ``address``, ``limit``,
``offset``, ``backend`` — and one name has one meaning. Two divergences were real bugs
waiting: the nav tools took ``campaign``/``config``/``run`` while every other tool took
the long forms, and ``config_path`` meant a *workspace-relative* path in the execution
tools but an *absolute filesystem* path in the authoring ones. ``tail`` (last N lines)
and ``top`` (top N patterns) stay distinct from ``limit`` because they are different
operations.

:mod:`tests.mcp_server.test_plugin_registry_sync` enforces all of this — the vocabulary,
the single ``{"error": …}`` convention, that no retired name survives in text an LLM
reads, and a ceiling on the surface's total token cost.


.. _mcp-analysis:

Reading results: SQL, not a tool per scope
------------------------------------------

There is no tool that summarises one configuration, none that returns a single run's
outcome, none that returns a run's host information. There were nine such tools, each a
hand-written reader of the campaign's ``metadata.yaml`` with its own response schema. Two
things were wrong with that. The file is written **only by postprocessing**, so every one
of them answered "run postprocessing first" about campaigns whose outcomes were already
recorded in ``campaign.db``. And the shape of the question was fixed by whoever wrote the
tool: "the mean error per parameter value, for the runs that passed" was not expressible at
all, while "the status of 200 runs" cost 200 calls.

So the per-run and per-configuration views collapsed onto the same read-only SQL the
metric tables already used:

* ``describe_campaign_data`` — the schema, and **where the canonical query for each
  question is written down**. Read its ``note`` first.
* ``query_campaign_data_sql`` — one ``SELECT``, optionally spanning several campaigns.

The entry points are two flat views, queried unqualified:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - View
     - Answers
   * - ``run_view``
     - One row per run: ``config_name``, ``run_id``, ``status``, ``duration_s``, the
       configuration's ``params_json`` and ``objective``, the search round that proposed it
       (``batch``), and the host record (``sysinfo_json``). Available as soon as runs are
       recorded. ``batch`` is 0 throughout a batch-mode campaign and only means something
       when ``campaign.campaign.mode`` is ``search``, where
       ``SELECT batch, COUNT(*), AVG(objective) FROM run_view GROUP BY 1 ORDER BY 1`` is the
       search's history over time.
   * - ``config_view``
     - The campaign's ``.vast`` as one row per key (``fullkey``, ``value``) — for reading
       the configuration without pulling one oversized cell.

They carry the joins on purpose. ``run_id`` is unique only *within* a configuration, so a
query that filters on ``run_id`` alone silently returns rows from every configuration and
averages across them — it does not fail. Making the join part of the schema removes that
failure mode instead of documenting it.

What survives as a tool is the one aggregate asked constantly —
``get_campaign_summary`` (pass/fail counts plus the campaign's provenance), itself
implemented over the same SQL — and ``list_campaigns``, which spans campaigns rather than
querying one.

**"What did this run cost?" is SQL too, and is not** ``get_resource_usage``. That tool
reports a *lane's* free capacity now, which is a pre-flight question. What an executed run
consumed is a table — :ref:`per-run resource usage <per-run-resource-usage>`, CPU and memory
per container over the run, joinable to ``runs.available_cpus`` for the saturation ceiling
and to ``poses`` for what the robot was doing at the time. The two read alike and answer
different questions, so ``describe_campaign_data`` names the table and says which is which.

**The nav analysis tools follow the same rule.** ``nav_get_trajectory``,
``nav_get_path_deviation`` and ``nav_get_action_feedback`` query ``data.db`` — the tables
postprocessing already ingested each CSV into, keyed on ``(config_name, run_id)``. They
used to re-parse ``poses.csv`` off local disk, which meant they answered "campaign not
found" for every cluster campaign, transferred a whole recording to compute eight numbers,
and read ``orientation.x/y/z/w`` from a file that records ``orientation.roll/pitch/yaw`` —
so every reported yaw was ``0.0``, a wrong answer with the shape of a right one.

Maps, videos and the resolved scenario parameters stay file-sourced, because no table
holds them; they are reached through the ``/results/<campaign_id>/…`` address space, which
is what makes them work on the cluster too. Where a fact genuinely is not in the database
— a nav variation's planned path is written to ``_transient/configurations.yaml`` and to
nothing else — the tool's docstring says so and names what was checked.

**Looking at a run: two tools, and the difference between them is the point.**

``get_camera_frame`` reads a camera that was *recorded during the run* — the perspective is
fixed by wherever it was mounted, it re-renders nothing, and it works on any backend that
registered a video (see :ref:`the videos table <videos-table>`). Cheap.

``get_simulation_screenshot`` renders the world **again**, from a viewpoint the caller picks
(``lookat`` / ``distance`` / ``azimuth`` / ``elevation``, or ``focus`` on a named entity, or a
camera the world defines). That needs a simulator that can re-render — robosito can, Gazebo
cannot — and a run that recorded its state, and it runs a container in the campaign's own
simulation image: seconds if that image is on the node, minutes if it must be pulled.

Both return an image, so both **raise** rather than returning ``{"error": …}``: an image
response has no dict to carry one. And for a *human* who wants to watch a run, neither is the
answer — ``read_file`` on the ``.webm`` returns a URL, and a video is not something to move
through this interface one frame at a time.

An aggregate over a distance needs a square root, and SQLite's own ``sqrt`` is a
compile-time option, so a query could work on the MCP host and fail in the service.
``SQRT(x)`` is therefore registered alongside ``STDDEV``/``MEDIAN``/``PERCENTILE`` and is
available to every SQL caller.

Two limits worth knowing, both stated in ``describe_campaign_data``'s output:

* **To list a campaign's configurations, list its directories**
  (``list_files("/results/<campaign>/")``). SQL knows only configurations that produced
  runs, so on a stopped or partially-run campaign it omits exactly the ones worth
  inspecting.
* **Do not** ``SELECT config_json``. It is the whole ``.vast`` in one cell, exceeds the
  per-cell limit, and returns truncated. Use ``config_view``, ``json_extract`` for a known
  path, or ``read_file`` on ``/results/<campaign>/_config/*.vast`` for the file as
  authored — that last one being the only way to see what the author *wrote* rather than
  the validated config with defaults filled in.

The first query on a campaign can be slow
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A cluster campaign's durable home is the object store, so the service materializes its two
query databases into a local cache the first time something asks for them — **inside** that
request. On a local ``vast serve`` this never happens; against a cluster reached through a
``kubectl port-forward`` it is the difference between a query answering immediately and one
taking a few seconds. Every query after it reads the cache.

That is reported rather than left to be guessed at:

* Before the wait, as a log notification naming the size and the transport, so a client that
  renders MCP notifications shows *why* it is waiting instead of appearing to hang.
* With the result, as a ``fetch`` field — ``{source, transfer, cold[, seconds, bytes]}`` —
  which is the measured cost of that call, not an estimate. It also reaches clients that
  ignore notifications, via the warning channel every tool result carries.
* On demand, from ``describe_campaign_data(preflight_only=True)`` — a cheap pre-flight (two metadata lookups)
  worth calling before a *batch* of queries against a campaign you have not touched yet.
  ``fetch_required: false`` means the question does not apply. ``transfer`` separates
  ``cluster-network`` (in-pod, fast) from ``port-forward`` (off-cluster driver, slow): the
  same object store reached two ways, differing by orders of magnitude.

A query fetches **only** those two databases (``_execution/data.db`` and ``campaign.db``),
never the campaign's run artifacts — the same rule ``read_file`` follows for ``/results``.
The cost therefore tracks the size of the metrics, not of the rosbags beside them.


.. _mcp-files:

Files
-----

Every file RoboVAST can reach has a single address, which is also the URL that serves
it:

.. list-table::
   :header-rows: 1
   :widths: 40 40 20

   * - Address
     - What
     - Writable
   * - ``/results/<campaign_id>/<path>``
     - a campaign's outputs
     - no
   * - ``/sources/<workspace_id>/<path>``
     - a workspace's authored inputs
     - yes

Five tools work over it — ``list_files``, ``read_file``, ``write_file``, ``edit_file``,
``delete_file`` — instead of a reader and a lister per scope. The path after the owner
is the **real on-disk path**, so what a listing shows is what you can read:

.. code-block:: text

   /results/<campaign>/  _config/     scenario.osc, <name>.vast, run files, notebooks
                         _execution/  launch.yaml, outcome.json, execution.yaml,
                                      controller.log, postprocessing.log,
                                      data.db (query it with SQL)
                         _transient/  configurations.yaml, entrypoint.sh
                         _jobs/job-N/ sysinfo.yaml, logs/system.log
                         <config_name>/<run>/  test.xml, out.csv, rosbag2/, scene/

``<config_name>`` is the directory name, which is **not** the ``config_identifier``
that the configuration tools accept — list the campaign root to see the real names.

A trailing slash lists a directory; without one you read a file. Listings are
non-recursive by default (a campaign has one directory per configuration and one per
run) and report ``total`` when truncated, so you know to page. ``read_file`` returns
text and refuses binary — fetch those over HTTP or with ``vast files get``.

Writes are restricted to ``/sources``: campaign results are immutable, and on the
cluster they are object-store objects that a local write could not change. Inline
writes accept only ``.vast``/``.osc``; everything else goes through ``create_upload``,
so its bytes never enter the token stream.

``get_service_info`` reports the two address templates, and — when the service runs on
your own machine — ``results_root`` / ``sources_root``, so you can read files with your
own tools rather than through the interface.


.. _mcp-control:

Campaign control
----------------

The ``execution`` plugin lets an assistant drive campaigns. It is a
**strict client of a running** ``robovast-service`` — a ``vast serve`` locally,
or the deployed one recorded by ``vast login``. The service is
the single execution authority and owns run-state tracking; there is **no local
subprocess path**. When no service is reachable the control tools fail loudly
(``{"error": "no robovast-service reachable — start a 'vast serve' …"}``) rather
than silently running a divergent local lane. (For a serviceless local run, use
the ``vast exec local run`` CLI directly.)

``start_campaign`` validates and launches through the service and returns
immediately — the campaign has barely started. Wait for it with ``vast exec
wait <campaign-id>`` (exit 0 finished, 1 failed/stopped, 2 ``--timeout``
elapsed), which returns only once the campaign is genuinely over, past
postprocessing. Deliberately a **command and not an MCP tool**: a campaign can
run for days, and a blocking tool call would occupy its caller for the whole of
it, where a command can be backgrounded and waited on. ``get_campaign_status``
is the single-read version for a campaign you are not waiting on. Results live
wherever the service keeps them — local disk for a local ``vast serve``, the
object store for a cluster service (retrieve via the web UI or
``get_campaign_download``).

Pass a ``description`` (≤ 200 characters) saying what the run is *for*. It is
recorded on the campaign row in its ``campaign.db``, so it travels with the
results and is shown by ``list_campaigns`` and on the campaign card in the web
UI — where it is the only thing telling two same-day ``campaign-<timestamp>``
ids apart. The launcher in the web UI has the same field.

**Choosing a lane on a dual-backend serve.** A ``vast serve --backend
local+cluster`` (a dev host with both Docker and kubeconfig) offers *both* lanes
in one service and routes per campaign. There, three tools take an optional
``backend`` argument:

* ``start_campaign(backend=…)`` — ``"local"`` pilots on the serve host's Docker;
  ``"cluster"`` dispatches Kubernetes Jobs. Empty uses the service's **default
  lane (cluster when available)**.
* ``build_experiment_image(backend=…)`` — build for the lane you will run on.
* ``get_resource_usage(backend=…)`` — size the lane you target.

Every other tool is scoped to an existing ``campaign_id`` (or ``build_id``), so
the service resolves the lane itself and no ``backend`` argument is needed.
Single-backend services (a plain local or in-cluster ``vast serve``, or
``--attach``) offer one lane and ignore ``backend``.

.. note::

   ``stop_campaign`` is a cooperative stop through the service, which owns the
   teardown (terminating a local Docker container, or the cluster's in-flight
   scenario Jobs). ``list_campaigns(running_only=True)`` reports the campaigns the
   service considers live (all lanes).

   ``stop_job`` is the narrow one beside it: it kills a **single running** job and lets
   the rest of the campaign finish. Reach for it only when ``list_campaign_jobs`` shows a
   job that is running and will not end on its own, and you still want the other runs —
   a merely slow run finishes by itself, and the lane's deadline kills a genuinely hung
   one without help, so check ``get_campaign_status``'s ``stalled`` before deciding. It
   refuses anything that is not ``running``, naming the phase. The kill is permanent and
   recorded: that run reports ``status='killed'`` with the reason in ``failure_message``
   for the life of the campaign, and counts as neither a pass nor a failure — so exclude
   it from pass rates (``WHERE status <> 'killed'``). See :ref:`stopping-one-job`.

.. note::

   ``list_campaign_jobs`` and ``get_job_log`` give an assistant the same **live
   per-job** view the web UI Monitor shows: the current batch's jobs with their
   status (running / pending / completed / failed) and aggregate counts, and the
   live log of a single **running** job (its scenario container's output — the
   running pod's log on the cluster, the live ``system.log`` file locally). A
   finished job whose pod has been garbage-collected has no live log.

.. note::

   ``get_resource_usage`` reports an execution lane's CPU/memory capacity and current
   usage, plus a ``parallel_runs`` flag. The fields mean the same thing on either
   lane, so an assistant reads them uniformly. Use it to size a ``.vast`` run
   against free capacity: with ``free_cpu = cpu_capacity - cpu_used`` (and the same
   for memory), a run's concurrency is ``1`` when ``parallel_runs`` is false,
   otherwise ``min(⌊free_cpu / run_cpu⌋, ⌊free_mem / run_mem⌋)`` from the per-run
   reservations in the ``.vast`` — and the wall time is roughly
   ``⌈num_runs / concurrency⌉ × per_run_time``.


.. _mcp-liveness:

Is it working, or is it wedged?
-------------------------------

``status: "running"`` is not evidence of health, and a caller must not have to know
which log to grep to find that out. Two separate questions, answered in two places.

**Is it progressing?** ``get_campaign_status`` answers this from facts the controller
owns, with no log reading at all:

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Field
     - Meaning
   * - ``phase_age_s``
     - How long the current phase has been held. Meaningful **before** the run loop
       (``initializing``, ``building``): those phases have no counter to watch, so a
       wedged project push looks exactly like a slow one without it.
   * - ``progress_age_s``
     - Seconds since a run last completed. This is the one that matters *during* a
       run: a campaign holds ``running`` for its whole life, so its phase age grows
       either way.
   * - ``stalled``
     - **Tri-state.** ``true`` once ``progress_age_s`` passes ``progress_deadline_s``
       (the declared ``execution.timeout`` scaled by ``runs_per_job``); ``false``
       inside it; ``null`` when the ``.vast`` declares no timeout, so no verdict is
       possible — ``stall_verdict`` then says so.
   * - ``stall_reason``
     - Present only when ``stalled`` is ``true``. Names the comparison *and the next
       call*, so the follow-up is not something to remember.

.. important::

   ``stalled`` is tri-state for a reason worth stating plainly. A two-valued flag has
   to answer ``false`` when there is no budget to check against, and ``false`` reads as
   *verified healthy* — a clean bill of health for a run that may already be dead. The
   tempting fix, substituting the enforcement backstop, is worse: it is one hour, so a
   two-minute pilot that wedged immediately would report ``stalled: false`` for
   fifty-nine minutes. **Declare** ``execution.timeout`` and the verdict becomes real.

That backstop is not wasted — it is simply a different job. Both lanes now *enforce* a
per-run limit from ``execution.timeout`` (a Job ``activeDeadlineSeconds`` on the cluster,
a ``timeout``-wrapped compose step locally), but only the cluster falls back to one hour
when none is declared — locally an undeclared timeout stays unbounded. Killing late still
beats never, whereas *reporting* late is worse than reporting nothing, so the two figures
are deliberately separate (``per_run_deadline_seconds`` versus
``declared_per_run_seconds``). A wedged local run with no declared timeout therefore stays
alive to be inspected — end it with ``stop_campaign``.

**What is it doing?** That is a log question, and the log tools answer it. All three
(``get_campaign_log``, ``get_job_log``, ``get_image_build_log``) — and
``search_run_logs`` below — take the same
controls, applied in this order — a claim this page made while ``get_campaign_log`` was
in fact the one tool without ``tail``, so it now has one:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Control
     - Effect
   * - ``hide_shutdown``
     - Stop at each run's scenario verdict — **on by default**, and normally what you
       want. Past the verdict a run is only tearing down: lifecycle transitions failing
       because their peer is already gone, TF errors from a publisher that has stopped,
       nodes being killed. Every one of those is a warning or an error by the classifier
       below, so they are the bulk of what a severity read returns and none of them
       describe the run. Applied first, so ``tail`` and the rest describe the *trial*.
       Turn it off (``hide_shutdown=False``) when the shutdown itself is the fault you
       are chasing. Never silent: the response carries ``shutdown_dropped`` on every
       call, ``0`` included, and names the way back when it cut something.

       ``get_campaign_log`` and ``get_job_log`` read a live stream, so they find the
       verdict in the text (:mod:`robovast.common.scenario_markers`); a stream that
       concatenates runs resumes at the next ``Executing scenario``. ``search_run_logs``
       reads it from :ref:`scenario_timestamps <scenario-verdict>` instead, where
       postprocessing recorded it — one answer to "when did the trial end", shared with
       the web UI. Not offered by ``get_image_build_log`` or ``exec_in_container``:
       neither has a scenario.
   * - ``grep``
     - Keep lines matching a case-insensitive regex. Free text — your pattern.
   * - ``min_severity``
     - Keep lines rated at least ``"warn"`` / ``"error"`` by RoboVAST's **own**
       classifier: a line's ``[WARN]``/``[ERROR]`` marker when it has one, else the
       published keyword pattern
       (:data:`~robovast.common.log_summary.DEFAULT_SEVERITY_PATTERN`). Use this
       instead of hand-writing a severity ``grep`` — it is the same definition
       everything else uses, and two patterns mean two answers to "is this healthy?".
       A marker outranks a keyword, so an ``[INFO]`` line reporting ``errors=0`` is
       not an error.
   * - ``tail``
     - Keep the last N of whatever survived the filters.
   * - ``summarize``
     - Return **distinct patterns with counts** instead of lines.

.. _search-run-logs:

**Which runs said it?** That is a different question, and no stream can answer it: it is a join
between a log and a run's verdict, and a stream has nothing to join to. ``search_run_logs``
searches the merged :ref:`run_log <merged-run-log>` table — every container's output joined with
``/rosout``, on each run's own playback clock — across runs and across campaigns.

Same reading vocabulary as the tools above (``hide_shutdown``, ``grep``, ``min_severity``,
``summarize``, ``tail``), defaults included — though ``hide_shutdown`` is the one it implements
differently, as a SQL term over ``scenario_timestamps`` rather than a scan of the text, because
its default shape (``group_by_run``) never renders lines at all. Since nothing is dropped in
Python there is no ``shutdown_dropped`` to report, so every response instead *says* in its
``note`` that only the trial was searched. What it adds is *scope*: ``config_filter``, ``run_id``,
``container``, ``node``, ``source``, a sim-time window (``t0``/``t1``), and ``in_window`` to
separate "during the trial" from "while the simulator was being reset around it". Set
``campaign_regex`` to make ``campaign_id`` a pattern over campaign ids, or name further campaigns
in ``extra_campaign_ids``.

Three shapes, one per question:

* ``group_by_run=True`` (the default) — hits per run, joined to ``passed``/``status`` and the
  first sim time it appeared at. This is the "which runs, and did they fail?" answer.
* ``group_by_run=False`` — the matching lines themselves, paged with ``limit``/``offset``.
* ``summarize=True`` — patterns and counts, so "what flooded this sweep" costs one call. The
  summary scans far more rows than it returns, because it returns counts.

Two costs it reports rather than hides. Every response carries ``campaigns`` (with what each
transfer cost) and ``campaigns_skipped``: on the cluster the first query of a campaign
materializes its ``data.db`` from the object store, so a cross-campaign search pays one transfer
per *cold* campaign — hence ``max_campaigns`` defaults to 5. And SQLite attaches at most 10 extra
databases, so a wider search is batched, never silently trimmed.

Each run also reports its ``clock_map_source``; ``none`` means that run's lines have no
``sim_time`` at all — readable, but not on the timeline (see :ref:`clock-map`).

``get_campaign_log`` takes one more, because its stream is several phases concatenated
under ``===== PHASE =====`` dividers (variation → run → postprocessing): ``phase`` reads
one of them — or ``"all"``. Every read reports ``phases`` as
``[{name, lines, included}, …]``, so what a read left out is stated rather than absent.

``summarize=True`` is the one to reach for on a stalled run, because **filtering
cannot diagnose a flood — the flood is the signal.** A campaign whose TF was being
rejected wholesale matched a severity ``grep`` 18226 times; the returned lines looked
like ordinary noise and the count that was the actual finding went unread. Summarized,
it is one line:

.. code-block:: text

   get_campaign_log(campaign_id, summarize=True)
   → patterns: [{pattern: "[tf_bridge] TF_OLD_DATA ignoring data from the past for
                            frame base_link at time <n> according to authority <…>",
                 count: 18226, severity: "warn", example: "<the first raw line>"}]
     patterns_total: 2, severity_counts: {other: 1, warn: 18226, error: 0}

Each line is normalized before grouping — timestamps, coordinates, ids and hashes
become ``<n>`` / ``<hex>`` / ``<uuid>`` — so the same message with different numbers
collapses, while the same text from two different nodes stays two findings.
``example`` keeps the group actionable, since the placeholders have eaten the
specifics. ``patterns_total`` is the true number of distinct patterns even when
``top`` capped the list, and ``severity_counts`` counts **lines**, not groups: "18226
warnings" is the finding, "1 distinct warning" is only how it is reported.

Summarizing replaces the text rather than shortening it: the response carries
``patterns`` and no ``content``/``text`` key, so a summary can never be mistaken for
a page of lines. ``dropped`` reports how many lines the filters excluded either way —
a filtered read is never silently passed off as a complete one.


Building experiment images
---------------------------

When a container needs new code or system packages **baked into its image**, the
assistant adds ``system_packages`` / ``python_packages`` to that
:ref:`container's block <config-containers>` in the ``.vast``. A campaign may build
several images -- one per container that adds packages -- so the ``execution`` plugin
exposes:

* ``build_experiment_image`` — build (or reuse) the image from the project's
  ``build:`` section. Returns ``{build_id, tag, cached}``.
* ``get_image_build_status`` — poll a build: ``phase`` / ``done`` plus, on failure,
  a **structured** ``error_detail`` (``phase`` = apt / pip / source-build /
  base-pull / push / resource, the offending ``build:`` ``entry``, and
  ``fixable_by`` = ``agent`` or ``infra``).
* ``get_image_build_log`` — the raw builder log for deep dives, while the build exists.

.. _mcp-build-phase:

**A campaign that needs an image is created first and waits for it afterwards.**
``start_campaign`` returns its id immediately even when the image has to be built; the
campaign is then in phase ``building`` with ``stage: "waiting for image <tag>"``, it appears
in ``list_campaigns(running_only=True)``, and ``phase_age_s`` separates a slow build from a
wedged one.
Two things follow from that, both worth knowing before reaching for a build tool:

* **The build's output is a** ``BUILD`` **section of the campaign's own log**, so
  ``get_campaign_log(campaign_id)`` reads it with the only id you were given — no
  ``build_id`` to look up, and it stays readable after the build itself is gone (a build Job
  is reaped an hour after it finishes, and with it ``get_image_build_log``). It **is** part
  of a default read: while the campaign is still building it is the only section there is,
  and a default that held it back answered "what is this campaign doing?" with nothing.
  Because it comes first and is routinely the largest section, narrow rather than page once
  the campaign has run — ``phase="run"`` for the campaign's own narrative, and for a build
  that is misbehaving ``phase="build", summarize=True``, which collapses a few hundred
  near-identical layer lines to a handful. ``phases`` lists every section with its line
  count and whether the read included it.
* **A failed build is a failed campaign, not a failed request.** ``start_campaign``
  succeeds; ``get_campaign_status`` then reports ``failed`` with the reason in ``error``.
  Do not read that as "the start did not go through" and retry — that creates a second
  campaign. Note also that BuildKit writes unmarked lines, which RoboVAST's classifier
  deliberately rates ``warn`` rather than ``error`` (a producer's own marker outranks a
  keyword, and inventing one would report errors a log never claimed), so
  ``min_severity="error"`` is not how you find a build failure — the status is.

Because ``build_hash`` is content-addressed, two campaigns needing the same image both wait
on **one** build: the phase means *waiting for* an image, not performing a build, and
``stop_campaign`` on a building campaign detaches it rather than cancelling a build a
sibling may also be waiting on.

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

.. _mcp-describe-world:

Asking what a world offers an override
--------------------------------------

The ``sim`` channel is writable long before it is *discoverable*. A campaign writes
``plugins.floorplna.size``, composes cleanly, ships, pulls the image, schedules the pod — and
only there is it refused, because resolving a world's ``extends`` chain needs the simulator.
``describe_world`` (and ``vast workspace world``) asks the simulator instead, up front:

.. code-block:: console

   vast workspace world tiago_pick --targets 'gripper_right*'

Two halves, at the two costs they actually have. ``plugins`` names each plugin under the key an
override addresses it by, with the dotted paths that already exist — cheap, no model built.
``overridable`` says which **model values a run may change while it is running** (the
``model_override`` plugin: friction, contact masks, actuator force limits, mass); its ``fields``
half is a property of the simulator rather than of the world and so is always there, while
``targets`` needs a model built and waits for a glob. A path the world leaves at its default is
legitimately absent, so an unlisted one is unverifiable rather than wrong; a *plugin* key
matching nothing is unambiguous, and is what the campaign pre-check refuses before any compute.

**It is asked in the image the campaign runs, and the reply says which.** Not a detail: which
world a ref resolves to depends on what is *installed*, so an experiment shipping its own world
package (``python_packages``) has worlds that exist in its built image and nowhere else. Asked
against a base image such a ref does not resolve — which is why a campaign whose image has not
been built yet is told to build it rather than handed a description of nothing.

**A partial reply is a reply.** The halves that need a model built can fail on their own — a
ROS world described where the simulator's colcon-packaged bridge is not on the path is the case
that made this rule — and then ``errors`` says why while the cheap half still answers. Read
``errors`` before concluding that a ``null`` ``entities`` means the world compiles none; and
read ``dropped_transport``, which names the transport plugins left out of the build (a describe
publishes nothing, so they contribute nothing but a way to fail).

.. _mcp-container-exec:

Testing a container and its setup
----------------------------------

``exec_in_container`` runs one command in an experiment image. It is for **testing a
container and its setup**, and it **produces no campaign data**: no campaign directory,
no ``/out`` mount, no provenance, no repetitions, and no entry in ``list_campaigns``.
This page argues throughout that a hand-started run "has no pinned image, no recorded
provenance and no repetitions, so its output cannot be compared with a campaign's" — this
tool is in that category *by design*, which is exactly why nothing it does is durable.
To run the experiment, use ``start_campaign``.

It exists because the alternative was worse. Answering "is the package installed?", "does
it import?", "is the launch file in ``share/``?" meant authoring a campaign and starting
it — a multi-minute cycle per question, repeated until the image was right. There was no
cheaper way to ask.

**Two questions, one knob.** ``config_name`` decides which:

* omitted — the bare image: ``python3 -c 'import rst'``, ``ros2 pkg list | grep <pkg>``,
  ``ls`` of an install tree. This is the loop that diagnoses the ament/``AMENT_PREFIX_PATH``
  pitfalls in :ref:`configuration <config-containers>` in one call each.
* named — that configuration staged exactly as a campaign stages it, so an empty
  ``command`` starts its scenario.

**Both sources are projects.** ``workspace_id`` + ``config_path`` names a workspace's
``.vast``; ``campaign_id`` uses an existing campaign's ``_config/``, which *is* a project.
Nothing about the campaign case is special — same staging, same environment. The image
resolves from that project's ``.vast`` like any run, so a rebuilt image is picked up
rather than a historical digest. A ``build:<tag>`` must already exist: this never builds
implicitly, because a quick check silently becoming a full image build is the cost it was
added to remove.

**The two lanes answer the same way.** On a service offering both, ``backend`` picks one
(``"local"`` for Docker on the serve host, ``"cluster"`` for Kubernetes); omitting it uses
the service's default lane, the same rule ``start_campaign`` follows. Ask the lane you
will *run* on: an image checked on one says nothing about the other. Both stage the
project's ``/config`` and, when ``workspace_id`` is given, mount that workspace read-only
at ``/sources/<workspace_id>`` — the same address, so a path returned by ``write_file`` is
usable verbatim in the command either way. In-cluster the staging goes through the object
store, exactly as a campaign job's does, so anything a campaign can stage this can stage
too — there is no separate size ceiling to run into.

**A running campaign is never a target.** There is no way from here to a job's container
or pod. A campaign in flight is provenance-recorded, reproducible compute, and attaching
to it would perturb the thing it exists to produce. To inspect a live stack, start that
stack here instead — and to ask why a *campaign* is wedged, use
``get_campaign_status`` (``stalled`` / ``stall_reason``), ``get_campaign_log``,
``get_job_log`` and — to *see* what a finished run did — ``get_camera_frame`` or
``get_simulation_screenshot``.

**At most one container exists at a time.** That is what keeps this from growing session
ids, a listing tool, and a leak class. ``keep_alive=True`` holds it open; every result
reports ``container.reused``, and ``reused: false`` on a ``keep_alive`` call means a fresh
container — anything the previous one was running is gone. ``stop_container`` ends it, and
``get_resource_usage`` reports it while it lives, so a lane with no room for a campaign
can be traced to your own held container instead of guessed at.

Asking for a different project while something is still running in the held container is
**refused**, naming ``stop_container``: replacing it would kill a scenario you deliberately
started, inferred from a changed argument rather than asked for. An idle container is
replaced freely.

**Time limits are derived, not passed.** A command gets a fixed cap; a scenario gets the
project's own ``execution.timeout``; a project that sets none gets the same fixed cap,
reported as ``limit_source: "default"`` — never the campaign lane's one-hour fallback,
which for a diagnostic container is a leak rather than a limit. Because the source is
reported, a ``timed_out`` result names its own remedy.

**Backgrounding, and where a scenario's output goes.** ``entrypoint.sh`` redirects its own
stdout when it is given no argv, so a *started scenario* writes to a file inside the
container and ``stdout`` comes back near-empty. The result carries ``log_path``; read it
with a follow-up ``command="tail -200 <log_path>"``. A command that backgrounds something
itself must detach it — ``setsid nohup … & disown`` — or it is torn down with the exec
that started it, which looks like "the stack died" rather than "I killed it":

.. code-block:: text

   exec_in_container(campaign_id=cid, config_name="platform-1", keep_alive=True)
   # -> the scenario, detached; note the returned log_path

   exec_in_container(campaign_id=cid, config_name="platform-1", keep_alive=True,
                     command="ros2 node list; ros2 topic list")
   # -> the live stack, reused: true

   stop_container()

There are no ``grep`` / ``min_severity`` parameters, unlike the three log tools: the
command *is* a shell, so ``| grep``, ``tail`` and ``sed`` are already available and
strictly more expressive. ``tail`` trims the captured output through the same
:func:`~robovast.mcp_server.log_view.view_log` filter those tools use.

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
