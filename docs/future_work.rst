.. _future-work:

===========
Future Work
===========

Directions that are designed but not yet implemented. Recorded here so the
intent and the reusable building blocks are not lost.


.. _future-raster-egress:

Visual artifact egress over MCP (video and rasters)
---------------------------------------------------

**Motivation.** Analysis over MCP is increasingly driven by an LLM. Quantitative
questions are well served: per-run metrics are consolidated into
``<campaign>/_execution/data.db`` and queried with read-only SQL via the
``run_data`` tools (``describe_campaign_data`` / ``query_campaign_data_sql``), and
a campaign's author-declared charts are exposed as Vega-Lite specs by
``list_campaign_plots`` (from ``evaluation.plots`` in the snapshot ``.vast``).

What is **not** reachable is any raster or video output. ``get_run_output_file``
deliberately refuses binary files, so ``rosbags_to_webm`` recordings and rendered
PNG plots cannot travel to the model. ``list_campaign_plots`` + SQL covers
*charts* (declarative specs the client renders), not *pixels*: a trajectory
overlay, a costmap, or a landing-scatter image cannot be seen. For a
robotics-simulation tool this is the main remaining analysis gap — 5000 rows of
poses is not how a human notices a robot driving into a wall.

**Open questions** (to decide, not yet decided):

* Should the model receive images as MCP image content, and for which artifacts?
* Is video better delivered as a short-lived artifact/link than inline, given
  size?
* The ``nav`` plugin (``robovast_nav``) already returns images via
  ``draw_map`` / ``display_simulation_screenshot`` — is that the pattern to
  generalize, or is a campaign-level artifact route the right home?


.. _future-llm-analysis:

Server-rendered figures (optional)
----------------------------------

A machine-readable figure layer **now exists**: a campaign's ``evaluation.plots``
declare ``{title, query, vega_lite}`` entries, surfaced over MCP by
``list_campaign_plots`` and over the service by ``GET
/campaigns/{id}/plots``. The web UI can render those Vega-Lite specs directly, and
an LLM can read ``data.db`` through the ``run_data`` SQL tools to *propose* new
ones.

What is *not* built is a server-side ``render_analysis(campaign, spec) ->
figure_json`` that resolves a declarative spec to a figure on the server. It is an
open question whether this is worth building at all: an LLM that can write SQL and
emit a Vega-Lite spec itself may not need the server to render one. Retained here
as a possibility, not a commitment. The ``evaluation.visualization`` Jupyter
notebook path (rendered to a static ``overview_*.html`` via ``nbconvert``) is
unaffected and stays for rich, code-driven analysis.

Refocusing the MCP interface for a cluster-first service
--------------------------------------------------------

A running programme of work, part landed and part not. The shared theme in every
item below is a **report that does not match reality** — a status that says
``running`` for a doomed run, a listing that omits campaigns that exist, a client
timeout for work that succeeded. Each was found by using the interface rather than
reading it.

Landed already: ``workspace_id`` is the service's only project binding (the
``.robovast_project`` fallback silently ignored ``config_path`` and ran the wrong
project); one pinned workspace directory, allowed on the off-cluster cluster lane;
``get_campaign_log`` routed through the service so it works on the cluster;
campaign discovery without a project file; and one shared path-confinement check
(``robovast.common.safe_path.safe_join``).

Not yet done, roughly in dependency order.

**1. Cluster campaign discovery (design settled, not implemented).**
``ClusterService`` inherits ``LocalTransport.list_campaigns``, which scans a local
directory — so a finished cluster campaign, whose home is the object store, is
invisible. Do **not** enumerate buckets: ``StorageClient`` has no bucket listing,
with a per-campaign-bucket store each campaign *is* a bucket named
``campaign_id.lower().replace("_","-")`` (a lossy transform to invert), and buckets
should stay internal. Instead publish a small per-campaign **index object** and read
it back, under two constraints:

* *Identity only, never status.* ``_execution/outcome.json`` is already the
  canonical terminal record and already carries ``postprocessing_error`` /
  ``share_error``; ``_status_from_disk``'s precedence exists so per-campaign status
  "can never disagree with the list view". A status field in the index would
  immediately be that second source of truth.
* *Write it at the earliest store write, not at* ``finalize_campaign``. Finalize is
  the once-after-completion publish, so indexing there hides exactly the campaigns
  worth inspecting — one whose upload itself failed, leaving partial data and no
  entry. Writing early makes postprocessing-failed, share-failed, stopped and
  crashed-mid-run campaigns discoverable, because the entry predates the failure.

**2. The image build must not block campaign creation.** ``create_campaign`` is
specified fire-and-forget, but ``_ensure_build_image`` awaits the build inline. A
cluster start for a project with a ``build:`` section dies on the HTTP client's 30 s
read timeout **while the server keeps going and the campaign succeeds** — a reported
failure for work that worked. No timeout value fixes this honestly. The async
machinery already exists and is discarded: ``_start_cluster_build`` returns a
``build_id``, and ``get_image_build_status`` / ``get_image_build_log`` are already
tools. Return immediately in a ``building`` phase and let the driver await it. This
applies to the local docker build too: building is part of the campaign's driven
work, not a precondition of its existence. It changes an error path deliberately —
a failed build becomes an inspectable failed campaign rather than no campaign.

**3. A caller cannot tell a hanging run from a healthy one.** ``get_campaign_status``
returned ``status: running, progress: 0`` for a run whose TF was being rejected
wholesale, so it could never reach its goal. The log tools *do* take a
case-insensitive ``grep``, but filtering is not enough when the flood **is** the
signal: the filtered response came back ``lines_total: 18226, dropped: 773,
truncated: true``, and the returned lines looked like ordinary noise. Needed, in
value order: a **count/summarize mode** that normalises lines and returns distinct
patterns with counts (``TF_OLD_DATA … x18226`` is one line at ~20 tokens instead of
thousands); **health in the status** (error/warn counts, top repeated message);
a **documented default severity pattern** so callers stop inventing one; and
**stall detection** (``progress`` unchanged for N minutes is itself a signal — note
the local lane does not enforce ``execution.timeout`` at all, so a stalled run there
hangs indefinitely). Interim guidance lives in the ``campaign-execution`` skill,
but a check that must be remembered will be forgotten; the status should carry it.

**4. One address space for files, read-only where it must be.** Roughly a dozen MCP
tools are the same operation — list or read a text file under a campaign-relative
prefix — reached through per-scope tools, all bottoming out in the same three helpers
in ``plugin_common``. Collapse them into ``read_file`` / ``list_files`` over the
service's own URL space, so the string a caller passes *is* the string it can GET:
``/results/<campaign>/<path>`` for outputs (**read-only**; on the cluster the local
tree is a cache of immutable objects, so a write there would silently vanish) and
``/sources/<workspace>/<path>`` for inputs (writable). The prefix becomes the
permission, dispatched once instead of enforced per tool, and each confines against
its own root via ``safe_join`` — never the other's. Both are separate top-level
namespaces because ``/campaigns/{id}/`` and ``/workspaces/{id}/`` are control
namespaces whose literal routes (``status``, ``logs``, ``query``, ``validate`` …)
would shadow user-chosen config and file names. Two details worth keeping: the path
after the namespace must be the **real** on-disk relative path (today's
``run-files/{config}/{run}/{path}`` is a synthetic segment matching no directory,
and "run files" separately means the ``.vast`` ``run_files:`` inputs — two opposite
meanings), and ``list_files`` must be **non-recursive by default** with ``total``
reported, or a campaign with thousands of runs returns an unusable listing.
On the cluster, back ``read_file`` with a **single-object** read rather than
``fetch_campaign``, which pulls the whole campaign prefix — otherwise a 2 KB
``metadata.yaml`` drags the entire campaign, in the primary deployment.

**5. Tell the caller how it can reach files.** Extend ``get_service_info`` with the
address templates plus a filesystem root for the results and sources trees, non-null
**only** when the service is local-filesystem *and* on loopback — then a caller on
the same machine can read files with its own tools instead of relaying content
through the interface. Never advertise a path the caller cannot open: in particular
the off-cluster fetch cache (``/tmp/robovast-campaigns``) is on the caller's host and
looks eligible, but it is ephemeral and holds only already-fetched campaigns.

**6. Retire "project" from the service, and re-cut the tool taxonomy.**
``ProjectConfig`` is a vestigial adapter inside the service — synthesized per call
with a constant ``results_dir``, while every consumer reads only ``config_path``.
The real unit is *(workspace, .vast)*; keep ``ProjectConfig`` for the CLI, where
``.robovast_project`` genuinely binds a config and a results dir. The word then
survives only there, and ``validate_project`` becomes ``validate_config``.
Separately, the tool modules mix scope-based and capability-based grouping with a
21-tool catch-all; re-cut by lifecycle phase (files / authoring / execution+build /
results+data / results-lifecycle / reference).

**7. Collapse the metadata tools onto SQL.** Nine tools are views over
``metadata.yaml`` plus the ``.vast``, each with its own response schema. ``data.db``
already holds this: ``campaign.config_json`` is the entire ``.vast`` (queryable with
``json_extract``), and — the constraint that made this look impossible — the
``campaign`` schema is written **live during execution**, so ``campaign.run`` answers
pass/fail counts with one ``GROUP BY`` before postprocessing exists. Only ``main.*``
needs postprocessing. Classify each of the nine against that, delete the ones SQL
answers (documenting the query), keep ``get_campaign_summary`` as the single
convenience implemented *over the same SQL*, and ensure every table an argument
relies on has a ``_TABLE_DESCRIPTIONS`` entry — "use SQL instead" must not mean
"guess the columns".

**8. Docs that do not exist yet.** There is no page describing the HTTP interface —
only the runtime OpenAPI at ``/docs``, which nobody reading the documentation sees.
That matters more once the path *is* the ``read_file`` argument. Add
``docs/http_api.rst``: narrative for the address space and conventions, plus an
**autogenerated** route table via a directive modelled on the existing
``.. mcp-tools::`` (``docs/_ext/mcp_tools.py``) — a hand-maintained endpoint list is
how ``run-files`` came to look documented while matching no directory.
