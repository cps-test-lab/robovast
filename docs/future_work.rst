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
``<campaign>/_execution/data.db`` and queried with read-only SQL
(``describe_campaign_data`` / ``query_campaign_data_sql``), and
a campaign's author-declared charts are exposed as Vega-Lite specs by
``list_campaign_plots`` (from ``evaluation.plots`` in the snapshot ``.vast``).

What is **not** reachable is any raster or video output. ``read_file`` deliberately
refuses binary content (it would be mangled, not read), so ``rosbags_to_webm``
recordings and rendered PNG plots cannot travel to the model — the bytes *are*
addressable over HTTP and with ``vast files get``, but nothing turns them into MCP
image content. ``list_campaign_plots`` + SQL covers
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


A freshness contract for locally-derived campaign inputs
--------------------------------------------------------

A ``build:`` section can only reference packages under the ``.vast``'s own directory (that directory
*is* the build context, and it is what gets uploaded when the service runs elsewhere). So a campaign
whose code or assets live outside it stages them in first — wheels built from a working tree, a
scene descriptor compiled from a world — by running a script by hand.

Those derived inputs have **no freshness contract**. A *missing* one fails loudly: the image build
cannot find the wheel. A *stale* one succeeds silently — the campaign runs old code, or its 3D view
shows a world the simulation did not run, and every status the service reports is green. Nothing in
RoboVAST triggers or checks the staging step: ``build:`` consumes the artifacts, ``pre_command``
runs in-container per run, and there is no host-side hook between "start" and "build the image".

The building block is small: let a ``.vast`` declare its derived inputs and the sources they come
from, then compare the newest source mtime against the artifacts at ``start`` and refuse with the
command to run. What needs deciding is how much further it should go — whether RoboVAST should
*invoke* the generator (which means running caller-supplied code on the host, and the same question
the campaign-level artifact route raises above), or only ever verify and refuse. Verifying is the
cheap 90%: the failure being fixed is forgetting, not being unable.


.. _future-llm-analysis:

Server-rendered figures (optional)
----------------------------------

A machine-readable figure layer **now exists**: a campaign's ``evaluation.plots``
declare ``{title, query, vega_lite}`` entries, surfaced over MCP by
``list_campaign_plots`` and over the service by ``GET
/campaigns/{id}/plots``. The web UI can render those Vega-Lite specs directly, and
an LLM can read ``data.db`` through the SQL tools to *propose* new ones.

What is *not* built is a server-side ``render_analysis(campaign, spec) ->
figure_json`` that resolves a declarative spec to a figure on the server. It is an
open question whether this is worth building at all: an LLM that can write SQL and
emit a Vega-Lite spec itself may not need the server to render one. Retained here
as a possibility, not a commitment. The ``evaluation.visualization`` Jupyter
notebook path (rendered to a static ``overview_*.html`` via ``nbconvert``) is
unaffected and stays for rich, code-driven analysis.

Refocusing the MCP interface for a cluster-first service
--------------------------------------------------------

What is left of a running programme of work. The shared theme in every item below is a
**report that does not match reality** — a listing that omits campaigns that exist, a client
timeout for work that succeeded. Each was found by using the interface rather than reading
it.

Finished items are not kept here: they are described where they are implemented — the file
address space in :ref:`file-address-space`, the SQL results surface in :ref:`mcp-analysis`
and :ref:`database-or-address-space`, the HTTP route table in :ref:`http-api`, and telling a
wedged run from a slow one in :ref:`mcp-liveness`. The numbering below is historical and
deliberately not compacted, so a note elsewhere referring to "item 9" still means item 9.

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
tools.

Worse than the timeout: while that build runs, **the work is unobservable**.
The campaign is created only *after* the build, so ``list_running_campaigns``
reports nothing, and the only handles on a build are
``/image-builds/{build_id}/status`` and ``.../log`` — there is no build listing, and
the timed-out call never returned the id. So a caller is left with a failed request,
no campaign, and live work it cannot see.

Creating the campaign **first**, in a ``building`` phase, fixes all of it at once and
makes ``list_running_campaigns`` correct with no change to it: a building campaign is
a campaign. The status then carries the phase and the ``build_id``, so the build log
stays reachable. Do **not** instead teach ``list_running_campaigns`` to enumerate
builds — that needs a listing endpoint that does not exist, returns entries that are
not campaigns, and creates a second in-flight registry to keep consistent with the
first.

Two details the ordering change must respect, because builds are **shared**:
``build_hash`` is content-addressed over the spec and context, so two campaigns needing
the same image both wait on one build.

* The phase means *waiting for its image*, not *performing the build* — otherwise two
  campaigns each appear to be building the same image.
* Stopping a building campaign must **detach** it, not cancel the build: another campaign
  may be waiting on it, and the image is a cache entry rather than that campaign's
  property. Return immediately in a ``building`` phase and let the driver await it.

This applies to the local docker build too: building is part of the campaign's driven
work, not a precondition of its existence. It changes an error path deliberately —
a failed build becomes an inspectable failed campaign rather than no campaign.

**3b. Log patterns are computed on read, never joinable.** Telling a hanging run from a
healthy one landed (:ref:`mcp-liveness`): the status carries ``progress_age_s`` and a
``stalled`` verdict against the declared per-run budget, and the log tools take
``min_severity`` and ``summarize`` so a flood of one message costs one line instead of
thousands. Deliberately, **nothing derived from a log is stored** — the counts are
recomputed per call, which is what keeps them out of competition with the tables in
:ref:`database-or-address-space`.

That is the right split for the *liveness* question and the wrong one for the
*analysis* question. Normalized pattern plus count is an aggregate, so by that same
rule it should be a table, joinable to ``run_view`` — "which failed runs share a
warning pattern?" is a query nobody can currently write, and it is the question that
turns a flaky sweep into a diagnosis. The raw lines stay files regardless; one TEXT
column is not queryable data.

The open part is the ingest path, and it is genuinely open. It must work on a **live**
campaign, where ``data.db`` does not exist yet and ``campaign.db``'s writer does not
tail container logs. Note the asymmetry that makes this harder than it looks: on the
local lane the run's own output is folded into ``controller.log``, which the controller
already writes and could count as lines pass through it; on the cluster that output
exists only in pod logs, which no in-campaign writer sees. A single ingest point that
works on both lanes is the thing to design, and until it exists an aggregate written on
only one lane would be worse than none — it would silently mean different things per
backend. ``rosout`` (already a DB table, from the rosbag) is the post-hoc half of this
and may be the model to extend rather than a second mechanism to add.

**5b. The file address space's remaining substrate costs.** The address space landed
(see :ref:`file-address-space`); these are the object-store paths it did **not**
optimize, each measured rather than guessed.

* *A cluster text read transfers the whole object.* ``read_object`` returns bytes, so
  paging 200 lines of a 1 GB ``controller.log`` moves 1 GB and peaks around 3.4× that in
  the service pod — an OOM in a memory-limited deployment, and repeated per page. Both
  SDKs stream (botocore's ``StreamingBody.iter_lines`` plus ``close()`` to abort the
  response; GCS ``blob.open("rt")``), which needs one new ``StorageClient`` method and
  costs the exact ``total_lines`` on a truncated read.
* *A recursive cluster listing enumerates every key.* ``list_entries(delimited=False)``
  runs the paginator to exhaustion — ~20 round trips for a 1000-run campaign to show
  100 names. Both APIs support pushdown (``MaxItems``/``StartingToken``,
  ``max_results``/``page_token``), which trades the exact ``total`` for an opaque
  ``next_token``. The non-recursive case is already delimited and does not have this.
* *A storage client is built per file request.* On S3 that is ~9 ms of CPU; on GCS the
  fresh credentials mean a **JWT grant exchange per read** (100–250 ms). Memoizing one
  client per ``(config, endpoint)`` on the service fixes both, but must not weaken
  ``_resilient``'s reconnect-on-stalled-port-forward path.
* *Byte reads are fully buffered.* ``vast files get`` on a 2 GB rosbag holds it in the
  service's RAM. ``FileResponse`` locally (sendfile, plus free ``Range``/``ETag``) and a
  streamed body on the cluster remove that; the HTTP client would need ``stream=True``
  to benefit.
* *``FileEntry.modified``/``executable`` are null on the object store* even though S3
  returns ``LastModified`` beside ``Size`` and this codebase already writes the
  executable bit as object metadata. Widening ``list_entries`` to a small record would
  fill them in.

**5c. One dispatch point for the file namespaces.** ``ClusterService`` overrides three
methods that each re-open with the same ``if namespace != RESULTS: return super()``, and
``MultiBackendService`` mirrors each one. Forgetting that guard fails *silently and
expensively* — the inherited path still returns the right bytes, having fetched the
entire campaign to do it. A ``FileSpace`` protocol (``list`` / ``read_text`` /
``read_bytes`` / ``write`` / ``delete``) with a filesystem and an object-store
implementation would put the namespace comparison in exactly one place, and would also
close the seam where ``/sources`` writes go through ``WorkspaceStore`` while its reads
go around it.

**9. A smaller defect found alongside the above.**

The other one — ``RobovastClient("")`` silently returning an in-process
``LocalTransport``, so a tool passing an unresolved URL read local disk instead of
reporting that no service answered — is fixed. ``robovast.mcp_server.service_access`` is
now the only place that resolves a service URL: ``service_client()`` returns the client or
``None`` (the control tools then report ``NO_SERVICE``), and ``client_or_local()`` spells
the fallback out for the operations that *are* meaningful without a service.
``RobovastClient("")`` itself still behaves this way; the fix was to stop calling it that
way, which a grep for ``detected_service_url`` under ``mcp_server/`` now shows.

* A campaign that ran and passed reported ``runs: {completed: 0, total: 0}`` in its
  ``_execution/outcome.json`` while ``test.xml`` recorded ``errors=0 failures=0`` and
  every postprocessed artifact was present -- the campaign-level counters were never
  populated. **Traced** for the local lane: ``DockerBackend.count_run_artifacts``
  returned ``None`` ("results are already on disk"), which made
  ``_start_progress_poller`` return early, so nothing ever wrote the counters and a live
  local campaign also published a ``progress`` that could not move. It now counts the
  per-run ``test.xml`` files, and a backend that genuinely cannot count is logged rather
  than passed over. What is still unverified is whether the *search* lane's
  ``batch_history`` and the campaign row's aggregate agree with those counters over a
  multi-batch run -- that aggregate is where a sweep's flakiness rate would be read
  from, so it wants one deliberate check before it is trusted.

**10. The cloud instance-type commands are untested.**
``get_instance_type_command`` is now wired into the generated entrypoint, so a run records
the node's instance type in its ``sysinfo.yaml`` (and thence ``main.runs.instance_type``).
Only the bare-metal implementations have actually run: ``rke2`` and ``minikube`` return
``uname -m``, which is verifiable locally. The **GCP and Azure** commands query a cloud
metadata service —

.. code-block:: bash

   INSTANCE_TYPE=$(curl -s -H "Metadata-Flavor: Google" \
     http://metadata.google.internal/computeMetadata/v1/instance/machine-type | awk -F'/' '{print $NF}')
   INSTANCE_TYPE=$(curl -s -H "Metadata: true" \
     "http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01")

— and neither has been exercised on a real node. Both fail *quietly*: ``curl -s`` on a
wrong URL, a changed response shape, or a blocked metadata endpoint yields an empty string,
which records exactly like the old hardcoded empty. So a green campaign proves nothing;
verify by running one on each provider and checking ``SELECT DISTINCT instance_type FROM
runs`` is a machine type rather than ``NULL``. The API versions in particular age: Azure's
``api-version`` is pinned in the URL.
