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
``list_campaign_plots`` (from ``visualization.results.data_browser.plots`` in the snapshot ``.vast``).

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
  ``draw_map`` / ``get_simulation_screenshot`` — is that the pattern to
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

A machine-readable figure layer **now exists**: a campaign's ``visualization.results.data_browser.plots``
declare ``{title, query, vega_lite}`` entries, surfaced over MCP by
``list_campaign_plots`` and over the service by ``GET
/campaigns/{id}/plots``. The web UI can render those Vega-Lite specs directly, and
an LLM can read ``data.db`` through the SQL tools to *propose* new ones.

What is *not* built is a server-side ``render_analysis(campaign, spec) ->
figure_json`` that resolves a declarative spec to a figure on the server. It is an
open question whether this is worth building at all: an LLM that can write SQL and
emit a Vega-Lite spec itself may not need the server to render one. Retained here
as a possibility, not a commitment. The ``visualization.results.explorer.notebooks`` Jupyter
notebook path (rendered to a static ``overview_*.html`` via ``nbconvert``) is
unaffected and stays for rich, code-driven analysis.

Refocusing the MCP interface for a cluster-first service
--------------------------------------------------------

What is left of a running program of work. The shared theme in every item below is a
**report that does not match reality** — a listing that omits campaigns that exist, a client
timeout for work that succeeded. Each was found by using the interface rather than reading
it.

Finished items are not kept here: they are described where they are implemented — the file
address space in :ref:`file-address-space`, the SQL results surface in :ref:`mcp-analysis`
and :ref:`database-or-address-space`, the HTTP route table in :ref:`http-api`, telling a
wedged run from a slow one in :ref:`mcp-liveness`, and campaign discovery plus the
non-blocking image build in :ref:`campaign-discovery` and :ref:`campaign-building-phase`.
The numbering below is historical and deliberately not compacted, so a note elsewhere
referring to "item 9" still means item 9.

**2b. Two deviations worth remembering, from the items that landed as**
:ref:`campaign-discovery` **and** :ref:`campaign-building-phase`. Both were recorded here
as settled designs and both were implemented differently on purpose; the reasoning is
easier to lose than the code.

* *The index marker is written at driver start, not "at the earliest store write".* The
  stated reason for the latter was that the entry must predate the failure, so
  postprocessing-failed / share-failed / stopped / crashed campaigns stay discoverable.
  Driver start satisfies that strictly better, and it collapses what would have been two
  best-effort side-writes — in ``kubernetes_backend.run_batch_in_pod`` and in
  ``controller._record_controller_outcome`` — into one hook in the file that owns the
  lifecycle. The only new case is a campaign that dies before touching the store at all,
  which lists as ``unknown`` with no data: the honest record that it existed and produced
  nothing, and exactly the failed-build campaign the second item wanted inspectable.
* *The status does not carry a* ``build_id``. That clause existed to keep the build *log*
  reachable, and the log is now a ``BUILD`` section of the campaign's own log — reachable
  with the id the caller already has, and durable past the build Job's TTL, which
  ``/image-builds/{id}/log`` is not. A second handle on the status would have been a
  second way to ask the same question.

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
backend.

The post-hoc half of this **shipped** as ``run_log`` (see :ref:`merged-run-log`): every
container's output joined with ``/rosout``, on the run's own playback clock, as a table
joinable to ``runs``. So "which failed runs share a warning pattern?" is now one query, and
``search_run_logs`` asks it across runs and campaigns. What remains open is exactly the
*live* ingest above — ``run_log`` is written by postprocessing, so a running campaign still
has only its streams.

(The earlier text here claimed ``rosout`` was already a DB table. It never was: the CSV is
written to the **job** directory, which ``generate_data_db`` does not glob. That gap is what
``run_log`` closes.)

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
  than passed over. What is still unverified is whether the *search*
  lane's per-batch record and the campaign row's aggregate agree with those counters over
  a multi-batch run -- that aggregate is where a sweep's flakiness rate would be read
  from, so it wants one deliberate check before it is trusted. (The ``Status.batch_history``
  this used to name is gone; the per-batch record now lives in ``campaign.db``'s ``batch``
  and ``unit`` tables, read by ``read_batch_objectives``.)

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

.. _future-dev-loop:

A faster developer loop, and the tunnel machinery it would retire
=================================================================

The off-cluster driver (``vast serve --backend cluster -x <ctx>``) is worth keeping: a
local debugger against a real cluster is hard to replace. It is also the **sole** reason
roughly 340 lines of tunnel-resilience exist —
``ClusterService._minio_port_forward_endpoint`` and its prober thread, the generation
counters, ``in_pod_storage._resilient``, and the shutdown coupling that stops a stalled
forward being resurrected mid-teardown. None of it runs in-pod, and none of it runs on
GCP, where the object store has a real endpoint.

Running that same local driver **inside the cluster's network** would make the machinery
unnecessary rather than merely unused:

.. code-block:: bash

   mirrord exec -- vast serve --backend cluster

`mirrord <https://mirrord.dev>`_ needs no cluster-side install — it spawns a temporary
agent pod — while `telepresence <https://telepresence.io>`_ wants a traffic manager. Under
either, the local process resolves ``robovast:9000`` natively: same debugger, same
edit-restart loop, no tunnel at all. That is the rare change that makes the loop *faster*
and deletes code, so it is worth an afternoon's trial before deciding.

If it holds, the port-forward path in ``bucket_ops`` and the reconnect machinery above can
go with it.

.. _future-node-types:

Node types: per-pool quota, and how a campaign should express what a run needs
------------------------------------------------------------------------------

**Motivation.** A cluster is modeled as one homogeneous pool. ``apply_kueue_queues``
renders a single ``default-flavor`` and a single ``ClusterQueue`` whose ``nominalQuota``
comes from :func:`get_cluster_allocatable_resources` — the **sum** of every node's
allocatable CPU and memory. On nodes of one shape that is correct. On a mixed cluster it is
arithmetic fiction: two 48-core nodes plus three 8-core nodes report 120 cores, so a
campaign reserving 8 cores per run has 15 runs admitted, of which only 11 fit the large
nodes, while an 8-core request does not fit an 8-core node at all once ``kubelet`` and the
DaemonSets have taken their cut. The remainder sit ``Pending`` and read as a scheduler
fault. ``execution.kubernetes.jobs.node_labels`` is the only lever today, and it is
all-or-nothing: it confines the whole cluster's jobs to one pool.

**The part that is clear.** Declaring the pools in the config the operator names with
``vast cluster setup --vast <file>`` — one ``ResourceFlavor`` per node type, each with its
own ``nominalQuota``, listed in one ``ClusterQueue`` in preference order — is a
straightforward extension of what is already there, and Kueue then places by fit and copies
the assigned flavor's ``nodeLabels`` onto the admitted pod. Quota per type would default to
the allocatable sum *over the nodes carrying that type's labels*, so the derivation is the
one already implemented, applied per label set rather than cluster-wide. A type whose
labels match no node has to be a setup error: a flavor no job can land on is precisely the
failure :func:`verify_kueue_admission_ready` exists to catch. With no node types declared
the rendered YAML must stay byte-identical, so existing clusters need no re-``setup``.

**The part that is not decided: where a per-type reservation is written, and by whom.**
The reservation a run needs is a property of the *experiment* and belongs in the campaign's
``.vast`` (``execution.containers.<name>.resources``); which pools exist is a property of
the *cluster*. Per-node-type reservations sit exactly on that seam, and both placements
have a real cost:

* In the campaign ``.vast`` — the author gains full control, but every author, including an
  LLM authoring a sweep over MCP, now has to know the cluster's pool names to write a
  reservation. Performance and placement detail leaks into the file that should describe
  only the experiment. The existing per-cluster list form
  (``cpu: [{gke_…: 4}, {minikube: 8}]``, resolved by
  :func:`~robovast.execution.cluster_execution.cluster_context.resolve_resources`) is the
  precedent, and it is already the least agent-friendly corner of the schema.
* Cluster-side only — the campaign stays clean, but the operator is silently rewriting what
  an experiment reserved, which for a reproduction study changes the measurement, not just
  its cost.

**A ratio, and what would have to be true for it to work.** The idea worth exploring is that
the campaign declares **one** reservation, against a reference node type, and each node type
carries a scalar factor relative to it; the service scales the reservation for whichever pool
a job is placed on. Authors keep writing one number, operators keep describing their
hardware once, and no pool name appears in a campaign file. Whether a single scalar can carry
that is an open question, and a testable one:

* A pod's requests are fixed **before** Kueue assigns a flavor and are never rewritten
  afterwards, so a scaled request forces the service to choose the type and stamp the
  matching ``nodeSelector`` itself. Placement stops being purely Kueue's decision.
* One factor per node type assumes every container scales the same way. They do not: a
  MuJoCo step loop is largely bound by one core's speed, while a Nav2 planner spreads across
  several — so the honest factor may have to be per container, or per resource (core count
  versus per-core speed), which is where the simplicity the idea was bought for goes.
* Scaling a reservation **down** for a smaller pool can change the result, not only the
  runtime: a simulation given fewer cores can drop below realtime pacing. A factor is a
  statement about *throughput*, and using it as one about *fidelity* is the failure mode to
  guard.
* A scaled request that no node in its pool can satisfy has to fail loudly at launch rather
  than produce a workload that never admits.

**Which software to measure it with** — the open question that gates the rest. Three
candidates, in increasing fidelity to what we actually run:

* Synthetic CPU benchmarks (``sysbench``, ``stress-ng``, ``coremark``). Cheap, standing, and
  wrong where it matters: they measure the machine, not the simulator, so a factor derived
  from them predicts sweep throughput only by luck.
* A ``roqsim`` micro-benchmark — one fixed world, fixed seed, fixed duration, run headless
  with no stack, reporting realtime factor. Cheap enough to run on every pool at setup, and
  it measures the bottleneck that actually governs a campaign.
* The substrate's own example campaigns (``configs/examples/basic_nav``,
  ``growth_sim``, ``quadrotor_landing``) as reference workloads. Most faithful, and the only
  way to learn whether *one* factor generalizes across a sim-bound and a stack-bound
  campaign.

The experiment that would settle it: run one reference campaign on each pool, record wall
time and realtime factor per run, derive the factor from the micro-benchmark, then check
whether it predicts the other campaigns' throughput within a stated error band. If a single
scalar holds across a sim-bound and a stack-bound campaign, the ratio design is sound; if it
does not, the honest options are a per-container factor or dropping request scaling and
keeping per-pool quota alone. Either way the measured factor belongs in a run's provenance
beside ``instance_type``, since a campaign whose runs were placed by fit has runs on
different hardware and any comparison has to group by it.

**Adjacent, and worth stating because it now cuts the other way.** RoboVAST's own
infrastructure pods are pinned to one node (:ref:`cluster-node-local-storage`), which makes
the single summed quota *more* misleading rather than less: the node holding the service, the
registry and the build cache has materially less left for campaign work than the sum implies,
and the queue does not know that.


.. _future-gpu-usage:

Per-job GPU usage, and why device-wide sampling is not it
---------------------------------------------------------

**Motivation.** A campaign records what each run cost in CPU and memory — sampled at 1 Hz per
process per container and consolidated into the ``resource_usage`` table (see
:ref:`merged-run-log` for the sibling log path). Since simulation cameras render on the GPU
(:ref:`cluster-gpu`), the same question is now open for the device: how much of it did *this
job* use? That is what decides whether ``--gpu-replicas`` can be raised, and it is the one
figure a GPU campaign cannot currently produce.

**The requirement is job-wise, not node-wide.** This is the reason the obvious
implementation was rejected rather than shipped.

**What is cheaply available, and why it does not answer the question.**
``nvidia-smi --query-gpu=memory.used,utilization.gpu`` is one 26 ms call and would slot into
the existing sampler without difficulty. But both figures are whole-*device*: under
time-slicing a single card carries up to ``--gpu-replicas`` tenants plus whatever else the
node runs (a desktop session accounted for 337 MiB on ``node-02``). A row would be
attributed per job — the sampler runs in the job's container, so ``config_name``, ``run_id``
and ``container`` all come out right — while its *value* described the whole card. That
answers "was the GPU saturated while my run went", not "what my run cost", and a row read in
isolation a year later gives no hint which of the two it is.

**Why per-job attribution is hard, established by measurement rather than assumption:**

* ``nvidia-smi --query-compute-apps`` returns **nothing** for an offscreen GL renderer — it is
  compute-only, and MuJoCo's EGL path is a graphics client. Per-process memory in MiB appears
  only in the human-readable ``Processes`` table (``G`` rows), i.e. NVML's
  ``nvmlDeviceGetGraphicsRunningProcesses``.
* ``nvidia-smi pmon`` does list graphics processes with a type column, but its ``mem`` field is
  a *percentage* of bandwidth, not a footprint.
* NVML reports **host** PIDs. A container has its own PID namespace, so a process there cannot
  recognize itself in that list, and there is no in-container mapping back. This is the actual
  blocker, and it is not specific to us.

**The tension worth stating plainly:** per-job GPU attribution and time-slicing pull against
each other. Exclusive allocation (``--gpu-replicas 1``, or MIG on hardware that has it — an
RTX A2000 does not) makes a device figure exactly the job's figure and gives up the
concurrency the replica count exists for. Time-slicing buys the concurrency and makes device
*utilization* meaningless per job, since the card interleaves contexts. Memory is the more
tractable half: an allocation does belong to one context, so per-job GPU *memory* is
attributable in principle and blocked only by the PID mapping above.

**The path that would work.** A node-level agent — a privileged DaemonSet rather than
in-container sampling — reads NVML where host PIDs are meaningful and correlates each PID to
its pod through ``/proc/<pid>/cgroup``, which carries the pod UID and container ID. This is
what NVIDIA's own ``dcgm-exporter`` does for per-pod GPU metrics, so the approach is proven;
what it is not is "reuse the CPU/memory logging path", which is why it is a design decision
and not an afternoon. Its output would still have to reach a run's rows, so the join from pod
to ``(config_name, run_id)`` has to be designed too.

Until then, the honest substitute is a **calibration campaign at** ``--gpu-replicas 1``: with
one tenant the device figure *is* the per-job figure, measured once and reused, while real
sweeps run time-sliced. Per-context memory has already been measured this way
(:ref:`cluster-gpu`): 93 MiB for one 640×480 offscreen context, ~77 MiB marginal by the
sixteenth.

**Design work already done, worth keeping when this is finalised.**

* **Process-level and system-level are different kinds of metric and want different tables.**
  ``resource_usage`` is per-process by contract, and putting a device figure in it as a
  synthetic ``__gpu__`` process would surface in the web UI's process list
  (``frontend/ui/src/lib/campaignDetails.ts``) and be aggregated by ``advice.USAGE_SQL``
  (:mod:`robovast.results_processing.advice`) as though it were one. A sibling
  ``system_usage`` table is the right shape, and the split should be structural so later
  metrics of either kind have an obvious home.
* **Make the new lane column-generic.** CSV → ``data.db`` already is: any ``*.csv`` in a run
  directory becomes a table, columns are the union of row keys and types are inferred
  (``GenerateDataDb`` in :mod:`robovast.results_processing.postprocessing_plugins`, typing in
  :mod:`robovast.results_processing.csv_types`), including ``ALTER TABLE`` for a column that
  first appears in a later run. Only the sampler-CSV → per-run-CSV step is not:
  :mod:`robovast.results_processing.resource_usage` names its columns in five places (its two
  fieldname tuples, ``read_container_csv``'s row tuple, ``Tick.processes``, and the ``grouped``
  accumulator). A slicer that carries every non-key column through verbatim would make a new
  metric a one-line change in the sampler and nothing else — and would be the thing
  process-level sampling could later migrate onto.
* **Reuse the sampler and the slicing helpers.** One daemon should write both files:
  :mod:`robovast.execution.data.monitor_resources` can derive the sibling path from its
  ``argv[1]``, which leaves both entrypoint scripts — and the launch contract pinned by
  ``tests/execution/test_resource_monitor_lanes.py`` — untouched. Per-run splitting,
  ``in_window`` and the clock conversion all come from
  :mod:`robovast.results_processing.run_slices`; its ``container_of`` is deliberately the one
  place per-container artifact names are inverted, so a new filename is registered there.
* **A probe registry, not a special case.** A probe is a callable returning
  ``{metric: value}`` whose availability is decided once at startup, so an unavailable probe
  contributes no columns and costs nothing. The GPU probe's availability test is
  ``/dev/nvidiactl`` plus ``nvidia-smi`` on ``PATH`` — which is exactly the right gate without
  configuration, because the container toolkit injects both per container: a CPU-only sidecar
  has neither and simply does not sample.
* **Sample the device at 5 s, not 1 Hz.** 26 ms per call is 2.6% of a core per container at
  1 Hz, ~42% across sixteen concurrent GPU jobs — overhead charged to the very node whose
  throughput the GPU work exists to improve. GPU memory of a running renderer is near
  constant, so 5 s loses little. The existing loop already sleeps in 0.1 s increments to keep
  SIGTERM prompt, so the slower cadence has to be a tick counter rather than a longer sleep.
* **Emit** ``""`` **for a missing value, never** ``"N/A"``. One non-numeric value demotes its
  whole column to ``TEXT`` campaign-wide (``csv_types.value_type``); an empty string becomes
  ``NULL`` and contributes no type evidence. ``nvidia-smi`` returns ``[N/A]`` and
  ``[Not Supported]`` for unsupported fields on some cards.
* **If a device figure is ever recorded anyway**, record the concurrent GPU process count with
  it. Counting the device's processes needs no PID matching, and it is what turns an
  uninterpretable "1574 MiB" into "1574 MiB shared by sixteen renderers".
