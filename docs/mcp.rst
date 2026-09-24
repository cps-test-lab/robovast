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
  campaigns on the Kubernetes cluster the service is deployed into.
* **Analyze** — inspect campaigns, configurations, runs, logs, and tabular run
  data (read-only).

That order is deliberate, and the server's MCP ``instructions`` say the same thing. A
server that introduces itself as an archive is used as one: assistants run experiments by
hand on the host and come here only to read files. A hand-started simulator has no pinned
image, no recorded provenance and no repetitions, so its output cannot be compared with a
campaign's; the instructions say so, and so does ``start_campaign``. Two MCP prompts
cover the halves: ``run_experiments`` and ``analyze_campaigns``.

.. _mcp-instructions-limit:

The instructions are the only text a client puts in front of the model before any tool
is chosen, and a client shows only so much of them: Claude Code cuts them at 2048
characters, without a mark the model can act on. They carry the loop and the rules it
cannot do without, and each tool's own description carries the rest. An installed MCP
plugin may add one paragraph routing a question to its tools; the server refuses to start
when core text and plugin paragraphs together exceed ``INSTRUCTIONS_LIMIT``.

A campaign runs a **workspace's** ``.vast``: ``workspace_id`` is the only project
binding the service accepts, and ``config_path`` selects among several
``.vast`` files in that workspace. There is no "current project" anywhere — not
server-side, and not CLI-side either: every command names its own input, and
``vast workspace run`` takes the same workspace-and-path pair this tool does.
Get a ``workspace_id`` by uploading one from the machine that holds the project:
``vast workspace init <dir>``. That is the route because this interface can reach
the service but not your filesystem — ``create_workspace`` + ``write_file`` covers
``.vast``/``.osc``, and ``create_upload`` covers a single file of any other kind.

The one exception is a **retrigger**: ``start_campaign(from_campaign=<campaign-id>)``
runs a *previous campaign's* frozen configuration and the image its runs actually used,
with no workspace involved at all. Campaigns are workspace-independent, and the workspace
one came from may be gone — its own ``_config/`` is the durable source of truth. It
produces a new campaign and leaves the source untouched, so it works whatever state that
campaign ended in, and it replays the recorded launch, so re-running a one-config pilot
stays a one-config pilot. It takes no other argument but ``force`` (passing one is an error
rather than being ignored), and the service refuses it when the pre-flight blocks — a campaign
that recorded no usable image, whose build context is not archived either, has to be launched
from its workspace instead. ``get_campaign_summary``'s ``retrigger`` key reports the same
verdict without launching, and ``force`` re-runs despite it
(:ref:`results-retrigger-preflight`).

A ``.vast`` file defines a **project**; a **campaign** is one execution of it; a
**config** is one scenario parameter set within a campaign.

The tools live at ``/mcp`` on the service's own port — ``vast serve`` mounts them
there by default, so one URL and one token reach the web UI, the REST API and the
MCP tools together. There is no separate server process to start.

Register a client against that URL. ``vast serve``, ``vast login`` and ``vast service
token`` each print the invocation, so the port, the path and the header set
never have to be assembled by hand:

.. code-block:: bash

   claude mcp add --transport http robovast http://127.0.0.1:8800/mcp \
     --header 'Authorization: Bearer <token>'

Pass ``--no-mcp`` to ``vast serve`` to serve the API without the tools.

.. note::

   **The control tools launch and kill real compute**, so the mount is behind the same
   shared token as every other route — there is no unauthenticated mode. A local
   ``vast serve`` binds ``127.0.0.1``; a deployed one is published over its Ingress with
   TLS. See :ref:`deployment`.


Claude Code plugin
------------------

The repository ships a small Claude Code plugin (``.claude-plugin/``) with one job:
**never end a turn silently in the middle of a campaign.** ``start_campaign`` returns as
soon as the campaign is *named*, so an agent that reads one status and stops has told the
user a campaign finished when it had barely begun.

Its hook blocks the first attempt to end a turn on a campaign nobody is waiting for, once,
and then allows. Three things settle a campaign: backgrounding ``vast campaign wait``, saying
plainly that you are not waiting and that ntfy announces the end, or ``stop_campaign``.
Blocking until done would hold a three-day sweep's session hostage, which is a worse
failure than the one being fixed.

It cannot live in the service: only the agent harness can see a turn ending. Hooks are a
Claude Code feature, so other harnesses get the advisory path — the ``next_step`` the tool
hands back, the server instructions, and ntfy.

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
       downloading, taking one in, cleaning up, deleting.
   * - ``reference`` / ``docs`` / ``examples`` / ``plugin_metadata``
     - Reference material about RoboVAST itself: the config schema, the CLI, the
       documentation, worked examples, and what plugins are installed.

Every module is a capability, none a scope. A mix — some by scope (one per campaign /
configuration / run), some by capability, with one large module holding everything else —
leaves "which module owns this?" without an answer, and puts build, postprocessing, share,
deletion and download inside a module named for execution control.

Names read ``<verb>_<resource>``: ``get`` retrieves, ``list`` enumerates, ``search``
filters, ``describe``/``query`` are the SQL pair, and ``validate`` / ``preview`` /
``start`` / ``stop`` / ``run`` / ``build`` / ``delete`` do what they say.

Two whole classes of question are deliberately *not* one tool per scope: files are
:ref:`one address space <mcp-files>`, and reading what a campaign did is
:ref:`read-only SQL <mcp-analysis>`.


.. _mcp-check-tiers:

What each check can and cannot settle
-------------------------------------

Three authoring/execution tools check a ``.vast``, and they are often mistaken for
increasingly-thorough versions of one check. They are not: they differ in **two independent
things**, and neither is "how carefully it looks".

.. list-table::
   :header-rows: 1
   :widths: 26 14 18 18 24

   * - Tool
     - Schema + refs
     - World loads/compiles
     - Installs ``plugins:``
     - Backend container context
   * - ``validate_project``
     - yes
     - yes
     - no
     - no
   * - ``preview_configurations``
     - yes
     - no
     - yes
     - no
   * - ``start_campaign``
     - yes
     - yes
     - yes
     - yes

Note what the table does *not* say. ``validate_project`` composes too (it has to, to report
``total_trials``), so "composes" is not the axis. And the container context is the *execution
backend's* — the campaign's aux pod — which is why ``start_campaign`` is the boundary rather
than "the cluster".

The world column is where ``validate_project`` runs a container, and it is a
**different** container from the backend context in the last column: a held, read-only query
container from the exec runner's pool (``ExecRequest.query``, ``service/world_query.py``), not a
variation's auxiliary one. That is why it can be the cheap tier and still settle the world —
the container is reused across calls, so a repeat validation costs an exec rather than a
start. ``check_world=False`` opts out and the world is then simply not checked.

It checks every world a run would load, once each: the campaign's ``simulation`` block
when some configuration runs it as authored, and each distinct block a configuration's own
``sim:`` resolves to. A campaign whose every configuration overrides the block -- a world
naming its mesh per configuration and none by default -- is checked on those configurations'
worlds only, because no run opens the default as authored. A search counts its
``search.parameters`` as one such configuration, the template every draw is composed from; a
``sim:`` value there taken from the search space as ``$name`` exists only per draw, so that
world is reported ``unchecked``.

Each world that loads is also checked for **keys its image does not know**. A campaign runs a
pinned image, and a world can be newer than it: a key a later plugin reads is, to the image's
older plugin, a key nobody reads, and most plugins do not refuse one — the run starts and the key
does nothing. So every component the world *document* declares has its top-level config keys
compared with what that image's own plugin publishes (``get_roqsim_plugin_details``: its
``parameters``, and ``schema`` where it declares one), and a key missing from it comes back as
``severity: "advice"``:

.. code-block:: text

   advice world: <image>'s energy_monitor (components.robot.energy) does not publish
   'resistive_w_per_nm2' as a config key; unless it reads it without documenting it, that
   image will ignore it.

Advice, because a plugin's published list is its documented ``Config::`` block unless it declares
a schema, and a documented block can leave out a key the plugin does read; a plugin whose schema
says ``strict_keys`` has a complete list by its own declaration, and the advice then says the
image will refuse the key. Not compared: the keys roqsim lets any component carry (a manifest's
``prefix``, a transport scope, a fault block — read from the image, which is what applies them),
components a model's manifest adds (they ship with the plugin that reads them), a plugin that
publishes no keys at all, and a plugin the world loads by path. The catalog is asked once per
image and cached with the ``list_roqsim_plugins`` tools' own; a catalog that could not be read is
itself an advice problem saying the keys were not checked.

``check_scenario`` is the second such check, on the same pool but in the **scenario** container
(``service/scenario_query.py``): does the scenario parse there, imports resolved? Only that
image can answer — ``import osc.<library>`` resolves against the
``scenario_execution.osc_libraries`` installed where the scenario runs — so it is the one check
that sees a library the image lacks, which otherwise kills every trial at its first line.

**A check that did not run is not a pass.** ``valid`` covers every check the reply reports on,
so a world nobody could look at makes it ``false``; ``world_checked`` says which of the three
happened (it ran, it could not, it was not asked for); and the problem carries
``severity: "unchecked"``. A caller branching on the boolean — which is what a boolean is for
— is therefore never told a campaign is good to run because the most expensive thing about it
was skipped, and it can still tell "could not check" from "is wrong" without matching on
English. ``severity: "advice"`` is the other side of that line: a checked fact worth saying,
and ``valid`` stays true. An *advisory* check — one the caller did not ask for, such as the
world-key check — reports as advice whether it ran or not: it was never part of the verdict, so
the only wrong answer it could give is silence.

The consequence is that each tier has something it structurally cannot settle, and the honest
place to say so is **the problem it reports**, not a tool description the reader has to
remember and map onto their situation:

* A world that only the campaign's own **built** image could describe cannot be checked
  before that image exists. ``validate_project`` then reports that it was **not** checked and
  names ``build_experiment_image``, rather than letting a silent reply read as a clean world.
* A ``plugins:`` spec not yet installed for the project cannot be resolved by
  ``validate_project`` at all — declared specs are installed during config *generation*. A
  package already staged in ``.robovast_plugins/`` *is* resolved, by reading entry-point
  **names** out of that directory: metadata, not an import, because
  ``config_plugins._prepend_sys_path`` is only safe in the isolated compose subprocess and
  this process is long-lived.
* A variation declaring an auxiliary container is exercised by **both**, because both
  compose: composing is what asks a variation to produce what it varies, and a variation may
  need a helper image to do it. Each arranges a runner for one first — see
  ``ServiceBase.validate_project`` and ``preview_configurations``, which enter the same
  held aux-runner span. What separates them is what they *report*: preview names the cells
  the sweep resolves to and the images it ran in ``aux_containers``, where validation reports
  only the counts. The composition is cached either way, so a following ``start_campaign``
  reuses the work.
* Where the runner for that helper image comes from is the *caller's* business, arranged per
  span by ``ServiceBase._aux_runner_context``: a campaign gets one for its run, a preview
  gets one held by the container-exec manager, idle only once every holder has released it
  and reaped after that. When neither applies — composing in a process with no backend —
  the refusal is
  :class:`~robovast.common.errors.AuxContainerUnavailable`, naming the variation and the
  container, rather than a ``docker run`` that dies with a bare ``FileNotFoundError``. It says
  where it ran, since that and not the ``.vast`` is what was missing.
* The exec runner's **query slot** is not that runner and never was: it runs a read-only question
  in a campaign's own image with nothing written back. A held **aux** slot in the same manager
  is, which is why the two live side by side under one reaper rather than one pretending to be
  the other.

Each carries a ``next_step`` stating what closing the gap **costs** — seconds for a preview, one
real trial and cluster time for a campaign — so a caller who only needed the sweep's shape can
weigh it rather than reading the hint as an instruction.


.. _mcp-one-tool-per-question:

What the documentation corpus covers
------------------------------------

``search_docs`` serves RoboVAST's own pages, roqsim's and OpenSCENARIO DSL's, as one corpus.
A campaign is authored against more than RoboVAST -- the world format, the plugin reference,
the scene catalog -- and that is documented in the repository that owns it. Searching only
this one returns zero for a world's ``components:`` list, which reads as "no such thing"
rather than "not indexed here".

**The image answers for its own pages.** The simulator image carries both upstream source
trees -- roqsim's at ``/opt/roqsim``, scenario-execution's in the ROS workspace -- so their
``docs/`` are read out of the image, over the same query pool and per-image cache the
catalogs use. Deriving the same pages from a source ref instead would put a second pin
beside the one the image was built from, free to name a different commit, and the service
would then document a simulator nobody runs.

No address is needed: the deployment's own simulator image answers, which is what a question
asked before there is a project to name is about. An ``address`` narrows it to the image that
``.vast`` resolves to, for a project pinning a different simulator than the service does.

The pages are served under the prefix of the corpus they came from, so both repositories keep
an ``architecture`` page and neither shadows the other, and every listing row carries a
``source``. The image says which corpus each page belongs to; a reader deriving it from the
path would be a second answer to that question.

The corpus is fetched once per resolved image, however many callers ask at once: a fetch costs
a container and a whole corpus over the wire, so a caller arriving while one is running waits
for it rather than starting a second. Warming begins when the server is constructed, which is
what makes the fetch already done by the time most questions arrive -- a question asked inside
that window waits for it, and pays what it would have paid anyway. The resolution runs on every
search -- it costs no container -- so a redeployed image is picked up by the next one.

**When the upstream half is missing** -- the service unreachable, or an image that carries no
pages -- RoboVAST's own pages still answer and the reply carries an ``incomplete`` field
saying so. A search that quietly dropped the world format would answer "no match" to a
question the missing half documents, and a smaller corpus looks like no corpus at all.

``ROBOVAST_DOCS_EXTRA`` takes ``label=/path`` pairs separated by the path separator, for a
checkout with no image behind it.

Only RoboVAST's own pages have their Sphinx directives expanded. Another repository's
extensions are its own, so its pages are served as written rather than half-rendered.

Using it: search an identifier, not a word
------------------------------------------

The search is a case-insensitive substring match over lines, and results are grouped by page
in **name order, not relevance order** — so the first page returned is not the best one. That
makes the query the thing that decides whether a reply is useful. Measured against the corpus
as it stands, 50 pages:

.. list-table::
   :header-rows: 1
   :widths: 34 14 52

   * - ``query``
     - pages
     - 
   * - ``world``
     - 31
     - a prose word matches most of the corpus
   * - ``osc``
     - 25
     - so does a short token that occurs inside other words
   * - ``spawn_robot``
     - 8
     - an identifier narrows to the pages that define and use it
   * - ``sensor_coverage_probe``
     - 2
     - the more exact the spelling, the closer to one answer
   * - ``Config::``
     - 1
     - punctuation included, and it lands on the page that defines it

**Search for the thing as a file spells it.** A plugin name, a YAML key with its colon, a
declaration's header — those are what the pages contain verbatim, and they are what an author
is looking for anyway. A word like ``world`` or ``scenario`` is in every page's prose.

The three calls, in the order they are usually wanted::

   search_docs()                          # the page list: name, title, source
   search_docs(query="spawn_robot")       # matching excerpts, grouped by page
   search_docs(page="roqsim-interfaces")  # that page in full, once you know which

``limit`` caps excerpts **per page**, not the number of pages, so raising it on a broad query
makes the reply bigger without making it narrower. Narrow the term instead.

``source`` on each listing row says which corpus a page came from, which is also how to tell
a simulator page from ours when both have one by the same name.


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
   * - ``get_cli_help()`` / ``("workspace run")`` / ``(search=…)``
     - the command groups / one command's ``--help`` / a keyword search of the tree
   * - ``search_docs()`` / ``(query=…)`` / ``(page=…)``
     - the page list / matching excerpts / one page in full
   * - ``get_example()`` / ``get_example("basic_nav")``
     - the catalog / one project's files
   * - ``list_workspaces()`` / ``list_workspaces("ws-ab12")``
     - all workspaces / one
   * - ``list_plugins()`` / ``(group=…)`` / ``(query=…)``
     - the group catalog / a group's plugins / a name search
   * - ``list_campaigns()`` / ``(running_only=True)`` / ``(sort="size")``
     - every campaign, live first then newest first / the live ones / largest results
       first (``order="asc"`` reverses either order; the live ones still lead)

The same reasoning fixes the vocabulary. One concept has one argument name across the
surface — ``campaign_id``, ``config_name``, ``run_id``, ``address``, ``limit``,
``offset``, ``backend`` — and one name has one meaning. A short spelling beside the long
one, or ``config_path`` meaning a workspace-relative path on one tool and an absolute
filesystem path on another, is a bug waiting for the caller that reads both. ``tail``
(last N lines)
and ``top`` (top N patterns) stay distinct from ``limit`` because they are different
operations.

:mod:`tests.mcp_server.test_plugin_registry_sync` enforces all of this — the vocabulary,
as an allowlist, so a new argument name fails the build until someone adds it on purpose,
the single ``{"error": …}`` convention, that no retired name survives in text an LLM
reads, and a ceiling on the surface's total token cost.


.. _mcp-analysis:

Reading results: SQL, not a tool per scope
------------------------------------------

There is no tool that summarizes one configuration, none that returns a single run's
outcome, none that returns a run's host information. A reader per scope would fix the shape
of the question to whoever wrote the tool: "the mean error per parameter value, for the runs
that passed" would not be expressible at all, while "the status of 200 runs" would cost 200
calls. And a reader of a file postprocessing writes would answer "run postprocessing first"
about campaigns whose outcomes are already recorded in ``campaign.db``.

So the per-run and per-configuration views are the same read-only SQL the metric tables
answer to, over the campaign directory itself (:ref:`database-or-address-space`): the engine
is DuckDB, in-process, and a table is built from the campaign's records the first time a
query names it.

* ``describe_campaign_data`` — the schema, and **where the canonical query for each
  question is written down**. Read its ``note`` first. It lists every table the campaign's
  records can give, each with its ``kind`` (``view``, ``table``, ``record``) and, for a
  per-run table, ``built`` of ``runs`` — for how many runs it is built already. Describing
  builds nothing; a table's ``columns`` are empty until it is built for some run.
* ``query_campaign_data_sql`` — one ``SELECT`` in DuckDB's dialect, **confined to the
  campaign it names**: the query sees only that campaign's files, so ``WHERE campaign_id =
  ...`` is never needed to keep another campaign's rows out. Before it runs, the tables it
  names are built for the runs in scope that lack them — narrowed to the runs its top-level
  ``WHERE`` restricts them to by ``config_name``/``run_id`` equality or ``IN``, so a first
  look at a large table is cheap when it names one run. What could not be built is reported
  in the reply's ``note``, by table and run. Spanning campaigns is deliberate rather than
  default: the interface's ``campaigns`` argument (on the HTTP query route) names the
  further campaigns, and every row then carries ``campaign_id``.
* ``build_campaign_tables(campaign_id, tables=None)`` / ``clear_campaign_tables(campaign_id)``
  — build a finished campaign's tables for every run ahead of a long analysis, in the
  background with progress in the campaign log's ``TABLES`` section; or remove them to free
  storage. Neither is needed for an answer: a query builds what it names, and a cleared
  table is built again on use.

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

Two more views carry a derivation that is easy to get wrong by hand. ``run_validity_view``
says whether a run was a clean observation or was capped at its CPU limit.
``pose_track_view`` summarizes every recorded track (length, duration, speeds, start and end
pose) over every pose, on the measurement clock (:ref:`pose-contract`). Both are listed with
their columns by ``describe_campaign_data``.

What survives as a tool is the one aggregate asked constantly —
``get_campaign_summary`` (pass/fail counts plus the campaign's provenance), itself
implemented over the same SQL — and ``list_campaigns``, which spans campaigns rather than
querying one.

**"What did this run cost?" is SQL too, and is not** ``get_resource_usage``. That tool
reports the cluster's free capacity now, which is a pre-flight question. What an executed run
consumed is a table — :ref:`per-run resource usage <per-run-resource-usage>`, CPU and memory
per container over the run, joinable to ``runs.available_cpus`` for the saturation ceiling
and to ``poses`` for what the robot was doing at the time. The two read alike and answer
different questions, so ``describe_campaign_data`` names the table and says which is which.

**Where a robot went is answered the same way, for any robot type.** A track's length,
duration and speeds are ``pose_track_view``, over every table that follows the
:ref:`pose contract <pose-contract>`; an action's feedback is its table. Both are SQL. What is
not a table is served by a core tool: ``get_track_deviation`` measures a track against its
configuration's planned path, and ``draw_config`` draws the configuration with a run's track.
A domain package contributes to these rather than adding read tools of its own (see
:ref:`the developer guide <add-mcp-plugin>`).

What a configuration's files hold is read from the files themselves.
``get_config_contribution`` names each one by role and campaign-relative path, so a map's
resolution, origin, thresholds and size come from ``read_file`` on the map YAML it names --
the same file the picture is drawn from, rather than a second reader of one format on the
tool surface.

Maps, videos and the resolved scenario parameters stay file-sourced, because no table
holds them; they are reached through the ``/results/<campaign_id>/…`` address space, which
is what makes them work on the cluster too. What a configuration's variations placed — a
planned path, goals, obstacles — is in no table either: ``get_config_contribution`` derives it
from the campaign's frozen ``configurations.yaml`` and ``.vast``, the same markers its config
view draws, for any robot type. ``get_track_deviation`` measures a recorded track against one
of those ``path`` markers, over every pose.

**Looking at a run: two tools, and the difference between them is the point.**

``get_camera_frame`` reads a camera that was *recorded during the run* — the perspective is
fixed by wherever it was mounted, it re-renders nothing, and it works on any backend that
registered a video (see :ref:`the videos table <videos-table>`). Cheap.

``get_simulation_screenshot`` renders the world **again**, from a viewpoint the caller picks
(``lookat`` / ``distance`` / ``azimuth`` / ``elevation``, or ``focus`` on a named entity, or a
camera the world defines). That needs a simulator that can re-render — roqsim can, Gazebo
cannot — and a run that recorded its state, and it runs a container in the campaign's own
simulation image: seconds if that image is on the node, minutes if it must be pulled.

Both return an image, so both **raise** rather than returning ``{"error": …}``: an image
response has no dict to carry one. And for a *human* who wants to watch a run, neither is the
answer — ``read_file`` on the ``.webm`` returns a URL, and a video is not something to move
through this interface one frame at a time.

The dialect is DuckDB's: ``CAST(x AS DOUBLE)``, ``x::JSON`` with ``->`` / ``->>``,
``unnest``, ``median``, ``quantile_cont``, ``regexp_matches``, and ``sqrt`` for an aggregate
over a distance. Two macros keep SQL written for other engines meaning what it meant:
``PERCENTILE(x, p)`` with ``p`` in 0..100, and ``REGEXP(pattern, x)`` as a search.

Two limits worth knowing, both stated in ``describe_campaign_data``'s output:

* **To list a campaign's configurations, list its directories**
  (``list_files("/results/<campaign>/")``). SQL knows only configurations that produced
  runs, so on a stopped or partially-run campaign it omits exactly the ones worth
  inspecting.
* **Do not** ``SELECT config_json``. It is the whole ``.vast`` in one cell, exceeds the
  per-cell limit, and returns truncated. Use ``config_view``, the JSON operators for a
  known path (``config_json::JSON -> 'execution' -> 'containers' -> 'scenario' ->>
  'image'``), or ``read_file`` on ``/results/<campaign>/_config/*.vast`` for the file as
  authored — that last one being the only way to see what the author *wrote* rather than
  the validated config with defaults filled in.

A query costs the rows it touches
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A query is answered by the service, next to the campaign's directory, and nothing has to be
prepared before a question can be asked — so ``describe_campaign_data`` takes a campaign id
and returns the schema, and a query works while the campaign runs.

The first query to name a table for a run pays for building it from that run's records; every
later one reads the parquet file it left in ``.cache/``. After that, cost tracks the rows a
query touches, not the rosbags beside them — the same rule ``read_file`` follows for
``/results``. Which is why a first look names one run.


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
                                      controller.log, postprocessing.log
                         _transient/  configurations.yaml, entrypoint.sh,
                                      postprocessing.yaml
                         _jobs/job-N/ sysinfo.yaml, logs/system.log
                         <config_name>/<run>/  test.xml, out.csv, rosbag2/, scene/

``<config_name>`` is the directory name, which is **not** the ``config_identifier``
that the configuration tools accept — list the campaign root to see the real names.

A trailing slash lists a directory; without one you read a file. Listings are
non-recursive by default (a campaign has one directory per configuration and one per
run) and report ``total`` when truncated, so you know to page. ``read_file`` returns
text and refuses binary — fetch those over HTTP or with ``vast files get``.

Writes are restricted to ``/sources``: campaign results are immutable — they are the
record a figure is drawn over, and a rewritten one is a figure nobody can check. Inline
writes accept only ``.vast``/``.osc``; everything else goes through ``create_upload``,
so its bytes never enter the token stream.

``get_service_info`` reports the two address templates, and — when the service runs on
your own machine — ``results_root`` / ``sources_root``, so you can read files with your
own tools rather than through the interface.

.. _mcp-origin:

Where a link comes from
-----------------------

An address or a route is origin-less, and everything that hands one back as a *URL* — the
binary/oversized ``read_file``, ``csv_url`` on a truncated query, the campaign log,
``get_campaign_download`` — needs an origin to put in front of it. **The service declares
that origin**, and reports it as ``web_base``.

It has to be the service's own fact. A transport's base URL is where *it* dials, which is
the same string only by accident, and the MCP mounted inside the service has no transport
at all — the client there *is* the implementation. Read off a transport, this raised an
``AttributeError`` on ``get_campaign_download`` and silently dropped every other link, on
exactly the deployments that publish the service. Both are fixed by asking the service.

A published deployment is told its origin when it is set up, because an in-pod service is
given no RBAC to read its own Ingress; a local ``vast serve`` uses the address it bound.
Where neither names one — unpublished, or bound to a wildcard, where which address a
caller used is genuinely unknowable — there is **no** origin, the URL field is absent
rather than empty, and the route or address is still the answer. A caller that dialled the
service itself keeps using the address that worked, which stays right through a tunnel or
a port-forward where the service's own view of itself would not be.


.. _mcp-control:

Campaign control
----------------

The ``execution`` plugin lets an assistant drive campaigns. It is a
**strict client of a running** ``robovast-service`` — the deployment recorded by ``vast
login``, or the one answering on the conventional port. The service is the single
execution authority and owns run-state tracking; there is **no local subprocess path**.
When no service is reachable every tool fails loudly (``{"error": "no robovast-service
reachable — …"}``) rather than silently running or reading something else. There is no
serviceless run at all: which service answers is the only thing that differs between a
deployment on this machine and one across the room.

``start_campaign`` validates and launches through the service and returns
immediately — the campaign has barely started. Wait for it with
``vast campaign wait <campaign-id>`` (exit 0 finished, 1 failed/stopped, 2 ``--timeout``
elapsed), which returns only once the campaign is genuinely over, past
postprocessing. Deliberately a **command and not an MCP tool**: a campaign can
run for days, and a blocking tool call would occupy its caller for the whole of
it, where a command can be backgrounded and waited on. ``get_campaign_status``
is the single-read version for a campaign you are not waiting on.

``start_campaign``'s ``priority`` says which campaign the cluster queue admits first
when several are waiting, so an assistant told to start something out of the way of a
running campaign can do it at launch. **Changing it afterwards, and pausing, are
``vast campaign`` verbs rather than tools** — ``priority``, ``pause`` and ``resume``.
Which experiment deserves the cluster right now is a decision about the operator's
plans rather than about the campaign in front of the assistant, and every tool's schema
is injected into the model's context on every request, so the surface is spent on what
an assistant is actually the right one to decide.

**Image builds wait the same way**, through ``vast image wait <build-id>…``.
That was once the exception — a blocking ``wait_for_image_build`` tool, on the
argument that a build is minutes rather than days — and the exception did not
hold. The tool could block for at most 600s, so a ROS build doing apt + pip +
colcon returned unfinished and had to be called again, blocking again, in
exactly the case where blocking cost most. A cap on how long a tool may block
does not make a long wait tool-shaped; it moves the overrun to the caller. The
rule that survives: **if a wait can outlive a turn, it is not a tool.**

``next_step`` is how those commands reach the caller: a literal command with the
ids already filled in, in band with the answer, because a reply carrying only an
id leaves "and now wait for it" to be remembered — and it was not. **An error may
carry one too.** A refusal is where the next move is least obvious: an agent told
"the image is not built" moments after building it has nowhere to go, whereas the
same refusal with ``vast image wait <build-id>`` attached is a next action. Where
the field is *absent*, that is an answer as well: there is nothing obvious to do
next. It is deliberately not on every reply, since a field that always appears is
one that stops being read.

Results live
wherever the service keeps them — its results root (retrieve via the web UI or
``get_campaign_download``, which hands back the route, the ``vast campaign download``
command with the id filled in, and a URL when this deployment declares an origin to build
one from — see :ref:`mcp-origin`). It says nothing about the share: whether a campaign has
a copy there is not a fact the service records, so claiming one would be advertising what
the caller may not have.

``export_campaign`` is the other way out, and the one to reach for when the point is an
**analysis away from the service** -- a notebook on a laptop, a hand-off to someone without
an account: it has the service build an export (:ref:`results-export`) -- the campaign's
tables as one parquet or CSV file each, its records beside them, its recordings if asked --
and returns a handle: the ``export_id`` to poll with ``get_export_status``, the route, a
URL where an origin is declared, and the ``vast campaign export`` command with every option
filled in. The download is link-only for the same reason the archive's is. Download the
archive instead when the point is the **campaign itself** -- to import it into another
service, to keep it whole, or when the tables are not what is wanted: the archive ships no
table, and an export's tables are built for the request, from the same records, by the
same decoder a query uses.

The opposite direction is ``import_campaign``: it takes in a campaign archive
somebody else produced and registers it, so it lists, displays and can be re-run like
one that ran here. It has two sources and **neither carries bytes, for the same reason
the download is link-only** — a campaign archive is routinely gigabytes.
``archive_path`` is a path on the *service host*; an archive on your own machine goes
through ``vast campaign import`` or the web UI's campaign view, both of which upload it
over a side channel (:doc:`http_api`) and then call this same operation.
``share_archive`` names one on the configured share, which the **service** downloads
itself — so a campaign moving between two servers never travels through anybody's
laptop, and needs no share credentials of yours.

The tool returns a ``campaign_id`` rather than a report, because the import is a
tracked operation: watch it with ``vast campaign wait <campaign_id>``, and read the per-stage
verdicts from the campaign's ``_execution/import.json``. Per stage because an archive
carries three version surfaces of its own and each can independently be older, newer,
absent or corrupt; a *degraded* import is usable-but-incomplete rather than a failure,
so read it before discarding a campaign you just recovered.

``delete_campaign`` takes one campaign id or a list of them, and answers the same way for
both: one ``{campaign_id, outcome, ok, message}`` per id, where ``outcome`` is ``deleted``,
``not_found``, ``partial``, ``running`` or ``invalid``. It is one tool rather than a second one
for several, because every tool's description is sent on every request and deleting one campaign
is deleting a list of one. Each id stands alone: a running campaign among them is refused and the
rest are deleted, so read every entry rather than stopping at the first. ``ok`` means nothing of
that campaign is left on the service, which is also true of one that was already gone.

There is no MCP tool for listing or downloading from the share. That is deliberate and
it is the same rule as the wait tools: a share listing is a CLI call
(``vast share list``), and a transfer that can outlive a turn is a shell command
(``vast share download``, ``vast share import``), which costs this surface nothing.

``vast share`` is the one command group that does **not** go through the service: it
speaks to Nextcloud, GCS or Zenodo directly, with the caller's own credentials, which is
why it ships with the full ``robovast`` distribution rather than with ``robovast-client``.
An agent holding only the client can therefore have its campaigns *uploaded* to a share
(that path runs in the service, as a launch flag) but cannot list or fetch from one. Say
so rather than suggesting the command, if that is the install you are on.

Pass a ``description`` (≤ 200 characters) saying what the run is *for*. It is
recorded on the campaign row in its ``campaign.db``, so it travels with the
results and is shown by ``list_campaigns`` and on the campaign card in the web
UI — where it is the only thing telling two same-day ``campaign-<timestamp>``
ids apart. The launcher in the web UI has the same field.

.. note::

   ``stop_campaign`` is a cooperative stop through the service, which owns the
   teardown (the in-flight scenario Jobs). It lands on whatever is *running*, and the reply says which: the
   **runs** (the batches that finished are still postprocessed, and the campaign
   stays queryable), **postprocessing** (results kept, derived data not
   computed — re-run it), or the **share upload** (cancelled, partial archive removed).
   A campaign that is already over is refused rather than silently accepted.
   ``list_campaigns(running_only=True)`` reports the campaigns the service considers
   live.

   ``stop_job`` is the narrow one beside it: it kills a **single running** job and lets
   the rest of the campaign finish. Reach for it only when ``list_campaign_jobs`` shows a
   job that is running and will not end on its own, and you still want the other runs —
   a merely slow run finishes by itself, and the Job's deadline kills a genuinely hung
   one without help, so check ``get_campaign_status``'s ``stalled`` before deciding. It
   refuses anything that is not ``running``, naming the phase. The kill is permanent and
   recorded: that run reports ``status='killed'`` with the reason in ``failure_message``
   for the life of the campaign, and counts as neither a pass nor a failure — so exclude
   it from pass rates (``WHERE status <> 'killed'``). See :ref:`stopping-one-job`.

.. note::

   ``list_campaign_jobs`` and ``get_job_log`` give an assistant the same
   **per-job** view the web UI Monitor shows: the current batch's jobs with their
   status (running / pending / completed / failed) and aggregate counts, and the
   log of a single job. A **finished** job is served as readily as a running one: a pod
   that has gone is read from the campaign's own files instead.

   Each job also carries ``node`` — where its pod was placed, ``None`` on a job the
   scheduler has not placed yet — and ``started_at`` (epoch seconds — the *job's* start, so a job that
   has not begun executing has one too) and, while it runs on a cluster, ``usage``: what it
   is consuming against **both** figures it was given. Measured against the request says
   whether the reservation was the right size; against the limit, whether the job is near
   being throttled or OOM-killed. Reading it answers those without an ``exec_in_job``, and
   without costing the run anything.

   An absent ``usage``, or an absent field inside it, means **not measured** — never zero.
   The job is not running, the job sets no container limits, or a container left a limit
   open (which means the whole node, so no ceiling is true). When the cause is worth acting
   on, the response carries ``metrics_unavailable`` saying so. Do not read a listing with no
   usage anywhere as an idle cluster: check that field first.

.. note::

   ``get_resource_usage`` reports the cluster's CPU/memory capacity and current
   usage — plus, where the service can report them, its ``disk`` and ``results``
   filesystems — and a ``parallel_runs`` flag. Use it to size a ``.vast`` run
   against free capacity: with ``free_cpu = cpu_capacity - cpu_used`` (and the same
   for memory), a run's concurrency is ``1`` when ``parallel_runs`` is false,
   otherwise ``min(⌊free_cpu / run_cpu⌋, ⌊free_mem / run_mem⌋)`` from the per-run
   reservations in the ``.vast`` — and the wall time is roughly
   ``⌈num_runs / concurrency⌉ × per_run_time``.

   **Reserved and measured are separate fields, and the sizing above wants the reserved
   one.** ``cpu_reserved`` / ``memory_reserved_bytes`` are what the scheduler has
   committed — the number that decides whether the next run fits — while ``cpu_measured``
   / ``memory_measured_bytes`` are what is actually being consumed, which answers whether
   the last campaign needed what it asked for. ``cpu_used`` aliases whichever the service
   leads with (the request sum on a cluster), so it stays the right field when the
   distinction does not matter. Either pair is ``null`` where the service has no such reading
   — a cluster without metrics-server cannot measure, saying which in
   ``metrics_unavailable`` — and ``null`` never means zero.

   ``storage_refusal`` is non-null while ``disk`` or ``results`` has less free space than the
   reserve the service keeps (``ROBOVAST_DISK_RESERVE_GB``, see :ref:`deployment`). While
   it is set, ``start_campaign``, re-runs, image builds, imports and postprocessing are
   refused with the same sentence; campaigns already running continue, but on a cluster start
   no new Jobs below the reserve and say so in ``get_campaign_status``'s ``stage``. Check
   it before a sweep rather than learning it from the refusal. When clearing the service's
   caches would help, the refusal's ``next_step`` is ``vast service cache --clear``.


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
       (the declared ``execution.timeout``); ``false``
       inside it; ``null`` when no verdict is possible — the ``.vast`` declares no
       timeout, ``status`` is not ``running`` (see below), or every job of the current
       batch is queued for cluster capacity, so no run is running and none can complete.
       That last case is the second one's argument applied inside ``running``: the budget
       is per-run, and a queue the campaign does not control is not a stalled run.
       ``stall_verdict`` then says which.
   * - ``stall_reason``
     - Present only when ``stalled`` is ``true``. Names the comparison *and the next
       call*, so the follow-up is not something to remember.
   * - ``health_findings``
     - What a running job's own **simulator** reported wrong about itself, ``error``-level
       only and absent when there is none. Independent of every field above: it needs no
       declared timeout and is true within a minute of the fault. See
       :ref:`mcp-health-findings`.

.. important::

   ``stalled`` is three-valued for a reason worth stating plainly. A two-valued flag has
   to answer ``false`` when there is no budget to check against, and ``false`` reads as
   *verified healthy* — a clean bill of health for a run that may already be dead. The
   tempting fix, substituting the enforcement backstop, is worse: it is one hour, so a
   two-minute pilot that wedged immediately would report ``stalled: false`` for
   fifty-nine minutes. **Declare** ``execution.timeout`` and the verdict becomes real.

   The other ``null`` is the phase. The budget is per-*run*, and ``progress_age_s``
   measures the age of the last **run** completion — so only ``status: "running"`` can be
   judged by them. ``postprocessing``, ``sharing``, ``finishing`` and the pre-run phases
   restart that clock when they begin and then have nothing that can advance it, so
   passing the budget there says only that the phase outlasted a single run. Converting a
   large campaign's rosbags always does, and asserting a stall over it reported a healthy
   campaign as wedged — pointing the reader at a job that had already finished, and ending
   ``vast campaign wait`` at exit 4. Read ``progress_age_s`` as the age of the phase, and
   ``get_campaign_log`` for what the phase is doing.

That backstop is not wasted — it is simply a different job. The cluster *enforces* a per-job
limit from ``execution.timeout`` (a Job ``activeDeadlineSeconds``), and falls back to an
hour when none is declared. Killing late still beats never, whereas *reporting* late is worse than reporting nothing, so the two
figures are deliberately separate (``job_deadline_seconds``, which falls back, versus
``declared_job_seconds``, which does not). A wedged local run with no declared timeout
therefore stays alive to be inspected — end it with ``stop_campaign``.

.. _mcp-health-findings:

What a running campaign says is wrong
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``stalled`` needs a declared budget and one run's worth of patience. A simulator can say
"sim time is not advancing" within a minute of the fault and needs neither — so it is asked,
and what it answers rides on ``get_campaign_status`` as ``health_findings`` and on
``get_job_state`` in full.

**How it is asked.** The service *pulls*: it runs a **fixed** command of its own choosing in
the running container (:meth:`~robovast.common.simulators.SimulatorBackend.health_command`),
reads the JSON, and keeps it for the length of one poll interval. So nothing runs in a run
container between reads, nothing is emitted into any log, nothing is written into the results,
and a campaign nobody is watching is never asked at all. N watchers cost one check per
interval, and the read happens off the request thread — a wedged container cannot slow a
status read even by its own timeout. Every running job that carries a run is asked, a
node-calibration probe included: the read runs inside the simulator's container and counts
against its memory, and a probe that was not asked would size the simulator without it.

**The contract, and all of it.** A simulator's reply carries ``findings``, each

.. code-block:: json

   {"level": "error", "check": "sim-time-rate", "detail": "sim advanced 3.1s in 60s of wall time"}

RoboVAST interprets **one word**: ``level``. ``error`` ends a ``vast campaign wait`` (exit 5);
``warn`` never does, and surfaces on ``get_job_state`` and the campaign's own exit.
``check`` is a stable slug the simulator owns — carried through untouched, so it is matched
and reported, never interpreted — and ``detail`` is its observation in its own words. There
is therefore no per-check knowledge anywhere in RoboVAST, and any simulator shipping a
command with this contract is understood without a line of code here. Look a slug up in the
simulator's documentation, not in this one.

**One finding is RoboVAST's own**, and it is here because the fault cannot be self-reported: a
container that was OOM-killed is not there to answer a health command, and the Job carrying its
evidence is deleted moments later. Under ``sizing: calibrated``, a run killed at memory the
campaign *measured* is recorded by the runner in the campaign's ledger and reported as
``calibrated-memory-oom`` — one finding per fault however many runs it takes, with the count in
its ``detail``, because every run is sized from the same probe's peak and so meets the same
figure. The campaign keeps running; ``vast campaign wait`` ends once on the finding, which is
how an agent watching learns of it without reading a log. What to do about it — state
``calibration.min.memory``, raise ``resources.memory``, or accept the loss — is the ``.vast``
author's, and :ref:`configuration <config-sizing>` says how.

This paragraph is the specification, deliberately: the two sides of it cannot import each
other, and an agreed format with no written home drifts the first time either side is
edited. The precedent is :mod:`robovast_decode.scenario_markers`, for the same reason.

.. important::

   **No findings is not a clean bill of health.** It means nothing was reported — which is
   also what a simulator that cannot report on itself, a run that is not recording, and a
   read that failed all produce. ``get_job_state`` is where the difference is stated: every
   section it cannot fill names *which* and *why* in ``unavailable``, rather than rendering
   as an empty world that reads as "nothing is happening".

   A check the simulator says it **did not run** is the same trap one level down, and it is
   reported as its own thing rather than as a finding: ``health_checks_not_run`` on
   ``get_campaign_status`` and a ``check did not run`` line from ``vast campaign wait``, each carrying
   the simulator's own reason. Never turned into a ``warn`` -- a finding has a ``level`` its
   simulator chose, and manufacturing one for a check that reached no verdict would put
   RoboVAST's word in the simulator's mouth. roqsim's ``robot-motion`` is the case to know:
   it resolves which bodies are robots from the run's own entity roster, and a run without
   one is a run where nothing looked at whether the robot moved.

Where the scenario has got to
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The simulator's half says whether the world is stepping and where its bodies are. It cannot say
which **action** the scenario is stuck in, which is usually the sentence that identifies the
fault -- so ``get_job_state`` folds that from the run's ``behaviors`` and ``behaviors_meta``
tables, the rows the campaign's data engine reads out of the ``behaviors.jsonl`` the run writes,
and returns it under ``scenario`` in the shape scenario-execution's own ``tree_state`` reader
gives. Nothing runs in the job for it: the log grows in the campaign directory as the file
agent delivers it, and the tree shown is the one a query of the run sees.

Two properties, both deliberate:

* **The two reads are independent.** The scenario runs in every campaign whatever the simulator
  is, so it is read unconditionally: a campaign whose simulator cannot report on itself still
  gets the more useful half.
* **It is the expensive half, and therefore on demand.** The behaviour-tree log holds one line
  per status change, so the current tree is a fold over every row rather than a tail read.
  That is why it lives on ``get_job_state``, asked for when someone wants it, and never in the
  cheap reply the service polls. Every run records it; there is no way to turn it off. A log
  without its metadata record is refused, naming the file: without it the tree is of an
  unknown run.

Alongside both, ``resources`` carries the newest sample the run's own monitor wrote, per
container and per process. It answers what neither of the others can: a run stuck at 0% CPU is
deadlocked, one at 100% is spinning, and a log and a tree that both say RUNNING cannot tell
them apart.

.. note::

   **The three reads look in three places, and while a run is live those are not the same
   place.** Worth knowing when a section comes back empty, because the failure looks identical
   to "nothing is happening":

   ===========================  ==========================  ============================
   what                         while the run is live        after results collection
   ===========================  ==========================  ============================
   the behaviour-tree log       ``<config>/<run>/``          same
   the monitor's CSVs           ``_jobs/<batch>/job-N/``     same (a JOB artifact)
   the simulator's records      ``_jobs/<batch>/job-N/``     ``<config>/<run>/``
   ===========================  ==========================  ============================

   So the tree is read from the run dir, the samples from the job's own ``OUTPUT_DIR`` (which
   the backend stamps on the pod, so it is read back rather than derived), and the simulator is
   asked about the job dir first and the run dir after it. Pointing all three at one directory
   is the bug this table exists to prevent: whichever read matched the path worked, and the
   others reported that the run had written nothing.

**What is it doing?** That is a log question, and the log tools answer it. All three
(``get_campaign_log``, ``get_job_log``, ``get_image_build_log``) take the same controls,
applied in this order (``search_run_logs`` below shares all but ``tail``):

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

       ``get_campaign_log`` and ``get_job_log`` read a live log, so they find the
       verdict in the text (:mod:`robovast_decode.scenario_markers`); a stream that
       concatenates runs resumes at the next ``Executing scenario``. ``search_run_logs``
       reads it from :ref:`scenario_timestamps <scenario-verdict>` instead, the table the
       decoder builds from the same verdict line — one answer to "when did the trial end", shared with
       the web UI. Not offered by ``get_image_build_log`` or ``exec_in_container``:
       neither has a scenario.
   * - ``grep``
     - Keep lines matching a case-insensitive regex. Free text — your pattern.
   * - ``min_severity``
     - Keep lines rated at least ``"warn"`` / ``"error"`` by RoboVAST's **own**
       classifier: a line's ``[WARN]``/``[ERROR]`` marker when it has one, else the
       published keyword pattern
       (:data:`~robovast_decode.log_summary.DEFAULT_SEVERITY_PATTERN`). Use this
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
``summarize``), defaults included — though ``hide_shutdown`` is the one it implements
differently, as a SQL term over ``scenario_timestamps`` rather than a scan of the text, because
its default shape (``group_by_run``) never renders lines at all. Since nothing is dropped in
Python there is no ``shutdown_dropped`` to report, so every response instead *says* in its
``note`` that only the trial was searched. What it adds is *scope*: ``config_filter``, ``run_id``,
``container``, ``node`` and a sim-time window (``t0``/``t1``). Set ``campaign_regex`` to make
``campaign_id`` a pattern over campaign ids. ``tail``, ``offset``, ``source`` and ``in_window``
are not parameters: the ``run_log`` table has those columns, and ``query_campaign_data_sql``
reaches them directly for the question that needs them.

Three shapes, one per question:

* ``group_by_run=True`` (the default) — hits per run, joined to ``passed``/``status`` and the
  first sim time it appeared at. This is the "which runs, and did they fail?" answer.
* ``group_by_run=False`` — the matching lines themselves, up to ``limit``.
* ``summarize=True`` — patterns and counts, so "what flooded this sweep" costs one call. The
  summary scans far more rows than it returns, because it returns counts.

Two costs it reports rather than hides. Every response carries ``campaigns`` and
``campaigns_skipped``: each campaign's ``run_log`` is read where it lies, so a campaign costs
no transfer, but ``grep`` is still a regex over every log line of every campaign it spans, and
a campaign whose ``run_log`` is not built yet builds it first — hence
``max_campaigns`` defaults to 5, and what it leaves out is named rather than silently trimmed.

Each run also reports its ``clock_map_source``; ``none`` means that run's lines have no
``sim_time`` at all — readable, but not on the timeline (see :ref:`clock-map`).

``get_campaign_log`` takes one more, because its log is several phases read as rows
(import → build → plugin install → variation → run, then postprocessing, share and
``TABLES`` — a table build asked for ahead — in the order they ran, each as often as it
ran): ``phase`` reads one of them — or ``"all"``. Every read reports ``phases`` as
``[{name, included, rows}, …]``, so what a read left out is stated rather than absent. On
this tool ``phase``, ``grep`` and ``min_severity`` are applied by the service **as it
reads** the phase files (:ref:`_execution/ <results-execution-dir>`), so a read of one
phase never transfers the others; each row it returns is rendered the way
``vast campaign log`` prints it, ``[PHASE] <time> <LEVEL> <logger>: <message>`` with a
continuation indented, and ``tail``, ``summarize`` and the page window apply to those lines.

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

* ``build_experiment_image`` — build (or reuse) the derived images the project's
  containers declare: one per entry in ``execution.containers`` that adds
  ``system_packages`` or ``python_packages``, tagged by container name. Returns
  ``{build_id, tag, cached, cached_builds, builds, next_step}``. It is **not built when
  this returns**: ``next_step`` is the ``vast image wait`` command to background, naming
  exactly the builds that are not cache hits (or, when every one is, the run to go
  straight to). As for a campaign, the wait is a shell command rather than a tool — see
  :ref:`mcp-control`.

  ``cached`` is the **conjunction**: true only when *every* image was a cache hit, with
  ``cached_builds`` giving the per-container verdict. That distinction is load-bearing:
  taking it from whichever value the primary container happens to have tells a project
  whose scenario image is cached and whose ``sut`` image is still building "cache hit,
  nothing to wait for" — and the caller goes on to exec in a ``sut`` image that does not
  exist yet, where the refusal reads as though nothing had been built at all.

  It is therefore also the cheap way to *ask* "is this image built?": idempotent, one
  registry manifest probe (or one ``docker image inspect``) when nothing changed, and
  ``cached_builds`` is the answer per container. Nothing else answers that without a
  ``build_id`` already in hand.
* ``vast image wait <build-id>…`` — block until every build is done (exit 0 built,
  1 failed, 2 stopped waiting: ``--timeout``, or the service stopped answering). Takes
  several ids because a project builds one image per container that adds packages, and
  waiting for the first says nothing about the rest.
* ``get_image_build_status`` — poll a build: ``phase`` / ``done`` plus a **structured**
  ``error_detail`` (``phase`` = apt / pip / source-build / base-pull / push / resource /
  builder-pod, the offending ``build:`` ``entry``, and ``fixable_by`` = ``agent`` or
  ``infra``). Carries a ``next_step`` for the phase it reports — this is the tool that is
  polled while deciding what to do next, and a build still running, one that cannot start,
  one that failed, and one that finished want four different actions.

.. _mcp-build-blocked:

**A build whose pod cannot start fails; it is not waited out.** Kubernetes leaves such a
Job ``active`` indefinitely — an unpullable image keeps the pod ``Pending``, and with
``backoffLimit: 0`` and no ``activeDeadlineSeconds`` neither the ``succeeded`` nor the
``failed`` counter ever moves. Read only as "still building", that is a wait that never
returns and an agent that is never told anything: the reported shape of this bug was a
backgrounded ``vast image wait`` that simply never exited.

So the status read asks *why the pod is not running*, and reports ``phase="blocked"`` with
the reason in ``error_detail`` from the first poll — then ``failed`` if the pod has been
blocked for a minute, which is long enough for a registry blip to clear and short enough
not to be a hang. ``fixable_by`` is ``infra`` and ``error_detail.phase`` is
``builder-pod``: nothing about the project's ``build:`` section is involved, and the
message names which image could not be pulled (the ``robovast-sidecar`` init container, or
the BuildKit builder) or which resource no node could satisfy. **Neither waiting nor
rebuilding helps** — the two things a caller would otherwise try.
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
``stop_campaign`` on a building campaign detaches it rather than canceling a build a
sibling may also be waiting on.

The workflow is three steps and stays entirely in the ``.vast`` the assistant
already edits:

#. add the package to the container's ``python_packages`` / ``system_packages``;
#. ``build_experiment_image`` — **idempotent**, so it is safe to always call (a
   no-op cache hit when nothing changed);
#. ``start_campaign`` — the image is wired in automatically.

The assistant may even skip step 2: ``start_campaign`` on a ``build:<tag>`` project
(re)builds the image as its first step. The build runs **where the backend runs**
(local ``docker buildx`` for a local ``vast serve``, an in-cluster BuildKit Job on
the cluster), and the assistant **never handles a registry reference or
credentials** — the symbolic ``build:<tag>`` is all it ever sees. Requires a
reachable ``robovast-service`` (a ``vast serve`` or a tunnel); on the cluster it
also requires a registry configured at ``vast cluster setup`` (see
:doc:`cluster_execution`).

.. _mcp-describe-world:

Asking what a world offers an override
--------------------------------------

The ``sim`` channel is writable long before it is *discoverable*. A campaign writes
``components.floorplna.size``, composes cleanly, ships, pulls the image, schedules the pod — and
only there is it refused, because resolving a world's ``extends`` chain needs the simulator.
``describe_world`` (and ``vast workspace world``) asks the simulator instead, up front:

.. code-block:: console

   vast workspace world tiago_pick --targets 'gripper_right*'

Two halves, at the two costs they actually have. ``components`` names each component under the
address an override names it by, with the dotted paths that already exist — cheap, no model
built. (It was ``plugins`` until the world document's own key was renamed to ``components``;
the payload lagged that, and a ``.vast``'s unrelated top-level ``plugins:`` made the old name a
collision as well as a mismatch.)
``overridable`` says which **model values a run may change while it is running** (the
``model_override`` plugin: friction, contact masks, actuator force limits, mass); its ``fields``
half is a property of the simulator rather than of the world and so is always there, while
``targets`` needs a model built and waits for a glob. A path the world leaves at its default is
legitimately absent, so an unlisted one is unverifiable rather than wrong; a *component*
address matching nothing is unambiguous, and is what the campaign pre-check refuses before any
compute.

``describe_world`` runs on the exec runner's held query pool, the same one ``validate_project``'s world check uses — so the two share a warm container
rather than each paying a start.

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

``exec_in_container`` runs one command in an experiment image, and **which** image depends
on the config source it is given — the two answer different questions. A ``workspace_id``
runs what that project would build *now*: a container declaring ``system_packages`` /
``python_packages`` must already have its image in the deployment's registry, because this never builds implicitly — a
seconds-long check must not silently become a multi-minute build. A ``campaign_id`` runs the
image that campaign *recorded*, so it is what you exec against to ask "what did that run
actually see?", and it stays correct after the workspace has moved on.

A refusal over a missing image says which of four states it is in — nothing started, a build
running, a build failed, or a build that succeeded and whose image has since been pruned —
and carries the ``next_step`` for that state, because the four need four different actions.
"Not built" alone was a dead end for the caller who had just built it.

It is for **testing a container and its setup**, and it **produces no campaign data**: no
campaign directory,
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

* omitted — the bare image: ``python3 -c 'import roqsim'``, ``ros2 pkg list | grep <pkg>``,
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

The service stages the project's ``/config`` and, when ``workspace_id`` is given, mounts that workspace read-only
at ``/sources/<workspace_id>`` — the same address, so a path returned by ``write_file`` is
usable verbatim in the command either way. In-cluster the staging is a tar stream from the
service's data plane into the pod, exactly as a campaign job's inputs are, so anything a
campaign can stage this can stage too — there is no separate size ceiling to run into.

**A running campaign is never a target of this tool.** There is no way from here to a
job's container or pod, and the argument for that has not changed: a campaign in flight is
provenance-recorded, reproducible compute, and attaching to it perturbs the thing it exists
to produce.

What changed is that the perturbation is now *recordable* rather than forbidden. Three tools
reach a live job, and the difference between them is who chooses the command and for how
long it runs:

* ``get_job_state`` runs only **fixed** commands the service chose — the simulator's own health
  read and a tail of the run's own resource samples, each in the container that runs it — and
  folds the scenario's tree from the run's tables without entering the job at all. Nothing
  arbitrary can ride in, nothing is perturbed, and nothing is recorded. That property holds
  *because* the commands are ours: they read files the run is already writing.
* ``exec_in_job`` runs **yours**, which cannot be bounded, so it is written into the
  campaign instead: every run the job covers is recorded as probed in the campaign's
  ``_execution/interventions.json``, which is the ``runs.probed`` column a query reads.
* ``tap_job`` runs the simulator's own **following** command
  (:meth:`~robovast.common.simulators.SimulatorBackend.tap_command`) -- ``ros2 topic echo``
  of the selected topics in the ROS shape, the topic list for none -- and collects what it
  prints for a few seconds. The command is the service's, but a process it started runs in
  the simulator's container for as long as the tap lasts, so it is recorded exactly as
  ``exec_in_job`` is. A simulator whose recording is already the live view (roqsim) has no
  tap and says so by name; read its run's tables instead.

That makes this tool the right *first* move rather than the only one, because it answers the
same question against a copy at no cost to the campaign. A fault that does not reproduce here
is itself the finding — it is environmental, timing-dependent or draw-specific — and that is
when the live job earns its record.

To ask why a *campaign* is wedged: ``get_campaign_status`` (``stalled`` / ``stall_reason``),
then ``get_job_state``, then ``get_campaign_log`` / ``get_job_log``; and to *see* what a
finished run did, ``get_camera_frame`` or ``get_simulation_screenshot``.

**At most one container exists at a time.** That is what keeps this from growing session
ids, a listing tool, and a leak class. ``keep_alive=True`` holds it open; every result
reports ``container.reused``, and ``reused: false`` on a ``keep_alive`` call means a fresh
container — anything the previous one was running is gone. ``stop_container`` ends it, and
``get_resource_usage`` reports it while it lives, so a cluster with no room for a campaign
can be traced to your own held container instead of guessed at.

Asking for a different project while something is still running in the held container is
**refused**, naming ``stop_container``: replacing it would kill a scenario you deliberately
started, inferred from a changed argument rather than asked for. An idle container is
replaced freely.

**Time limits are derived, not passed.** A command gets a fixed cap; a scenario gets the
project's own ``execution.timeout``; a project that sets none gets the same fixed cap,
reported as ``limit_source: "default"`` — never a campaign's one-hour fallback,
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

What was called, and what it answered
-------------------------------------

Every tool call is recorded once, by one middleware
(:func:`robovast.mcp_server.server._install_tool_stats`), and no tool carries accounting code
of its own -- a tool added tomorrow is in the record without knowing the record exists. The web
UI's Admin page shows it under **MCP tools**: a ranking of which tools agents actually reach for,
with a green/red bar per tool comparing call counts and failure share, and the calls behind it.

The record is one row per call rather than a counter per tool, and that is the whole decision.
Counts, error rates and durations would fit in ~70 upserted rows; the **arguments and the
answer** would not, and those are what makes a failure debuggable once the process that served it
is gone. So the ranking is an aggregate over the log rather than a number kept beside it.

What it keeps, and does not:

* ``args`` and ``answer`` are truncated where they are recorded --
  :data:`robovast.mcp_server.tool_stats.MAX_LINES` lines and
  :data:`~robovast.mcp_server.tool_stats.MAX_CHARS` characters, marked when cut. A
  ``write_file`` body or a whole campaign summary never reaches the table. That module is the
  single place deciding this, which makes it also the place to read when asking what the trail
  could hold.
* Rows age out at :data:`~robovast.mcp_server.tool_stats.MAX_AGE_S` (30 days) or
  :data:`~robovast.mcp_server.tool_stats.MAX_ROWS`, whichever bites first. Age alone would not
  bound the table -- one agent loop emits thousands of calls in an hour -- so a burst shortens
  the retained window, and the panel says so rather than claiming a month it does not have.
* Rows go to ``mcp_calls.db``, a SQLite file on the workspaces volume beside the service's
  event log, buffered rather than written per call: a write in front of every tool call would
  cost more than some of the tools. They therefore survive a service restart, and last as
  long as that volume does.
* ``actor`` is the resolved principal -- the name the caller gave and the source it
  authenticated by -- and ``session`` is ``"<client>/<session>"``: over streamable HTTP that
  client's ``mcp-session-id``, the same for every call it makes. The principal is who the
  service authenticated; the session is what separates two agents sharing one token. Without
  the session the record can say a tool was called a thousand times but not whether that was a
  thousand agents once or one agent in a loop, which are opposite findings.

**A page of the record says how much of the record it is.** ``read_calls`` is one page; the total
it was cut from is ``count_calls``, and the routes report both, so a reader can tell a record
that ended from a page that did -- the ranking printed beside it summarizes the full retained
window. The panel's page ceiling bounds one JSON response the service holds in memory; the CSV
export streams and so is bounded only by what is retained.

**Recording never fails a tool call.** Every path in
:mod:`robovast.mcp_server.tool_stats` swallows its own failure -- an unwritable log file costs the
log, never the call. This is the same contract :class:`robovast.service.event_log.EventLog`
states for itself, for the same reason: what is recorded is a description of the work, not the
work.

.. _mcp-tools:

Available Tools
---------------

All tools are provided by plugins loaded at startup via the
``robovast.mcp_plugins`` entry-point group. The table below is generated from the
**registered** plugins, so it always reflects the tools the server actually exposes: an
installed distribution contributing one adds its tools to it.

Use MCP Inspector or a compatible client to explore the available tools and
their input/output schemas.

.. code-block:: bash

    npx @modelcontextprotocol/inspector

.. mcp-tools::
