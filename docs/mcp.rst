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
     - Before anything runs: create a workspace, put a ``.vast`` in it, check it, and see
       what configurations it expands to.
   * - ``execution``
     - Starting a campaign and watching it: status, logs, per-job view, capacity, stop.
       Building the experiment image lives here too — a build is part of a campaign's
       driven work, not a stage of its own.
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
cleanup, delete and download, none of which are execution control.

Names still read ``<verb>_<resource>``: ``get`` retrieves one object, ``list`` enumerates,
``search`` filters, ``describe``/``query`` are the SQL pair, and ``validate`` /
``preview`` / ``start`` / ``stop`` / ``run`` do what they say.

Two whole classes of question are deliberately *not* one tool per scope: files are
:ref:`one address space <mcp-files>`, and reading what a campaign did is
:ref:`read-only SQL <mcp-analysis>`.


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
       configuration's ``params_json`` and ``objective``, and the host record
       (``sysinfo_json``). Available as soon as runs are recorded.
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
* On demand, from ``campaign_data_status`` — a cheap pre-flight (two metadata lookups)
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
                         _execution/  outcome.json, execution.yaml, controller.log,
                                      postprocessing.log, data.db (query it with SQL)
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

That backstop is not wasted — it is simply a different job. Only the cluster lane
*enforces* a per-run limit (as a Job ``activeDeadlineSeconds``, falling back to one
hour so a run cannot hang forever); the local lane enforces nothing at all. Killing
late still beats never, whereas *reporting* late is worse than reporting nothing, so
the two figures are deliberately separate (``per_run_deadline_seconds`` versus
``declared_per_run_seconds``). A stalled local run therefore stays alive to be
inspected — end it with ``stop_campaign``.

**What is it doing?** That is a log question, and the log tools answer it. All three
(``get_campaign_log``, ``get_job_log``, ``get_image_build_log``) take the same four
controls, applied in this order:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Control
     - Effect
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

When an experiment needs new code or system packages **baked into its container
image** (a new ``sim_suite`` package, an apt dependency), the assistant declares a
:ref:`build section <config-build-section>` in the ``.vast`` and sets
``execution.image: build:<tag>``. The ``execution`` plugin then exposes:

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
