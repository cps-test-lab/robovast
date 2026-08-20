.. _architecture:

============================
Client–Server Architecture
============================

RoboVAST's operations are exposed behind **one interface**, implemented **once**
server-side, so the ``vast`` CLI, the MCP server, and the :ref:`web UI <web_ui>`
are all thin **clients** of the same contract. This lets an LLM (or any client)
author, run, re-postprocess, and reason over large-scale simulation campaigns
through an interface that can live on a **different host** from the client.

One interface, one implementation, many clients
-----------------------------------------------

There is a single operation contract,
:class:`robovast.service.interface.RobovastInterface`, mirrored **1:1** in three
bindings:

* the **service** HTTP endpoints (:mod:`robovast.service.app`, FastAPI, OpenAPI
  at ``/docs``);
* the **client** :class:`robovast.service.client.RobovastClient`;
* the **MCP tools** and **``vast`` CLI** commands, which wrap the client.

Campaign status reuses :class:`robovast.client.status.Status` verbatim — the live state
the campaign driver publishes — so every client reads one status vocabulary regardless of
where the campaign runs.

.. _architecture-distributions:

Four distributions, layered by audience
---------------------------------------

The contract above is one thing; what you have to install to use it is another. RoboVAST
ships as separate distributions so each audience installs what it needs and — the part
that matters — can decline what it does not.

.. list-table::
   :header-rows: 1
   :widths: 24 46 30

   * - Distribution
     - Contains
     - Adds
   * - ``robovast-client``
     - the ``vast`` root command group and every verb that only *drives* a service, the
       interface models, the HTTP client, the credential store
     - ``pydantic``, ``click``, ``requests``
   * - ``robovast``
     - service core, config and variation, results, the MCP server, the campaign
       controller, the local Docker lane
     - no Kubernetes client
   * - ``robovast-cluster``
     - the Kubernetes execution lane, its cluster-config plugins, the deploy and operator
       commands
     - ``kubernetes``, ``boto3``, ``google-cloud-storage``
   * - ``robovast-nav`` / ``robovast-sim-roqsim``
     - navigation variation types and panels; the roqsim simulator backend
     - per package

**The dependency direction is the design.** ``robovast-client`` depends on nothing of
ours, ``robovast`` depends on it, and the lanes depend on ``robovast``. ``robovast`` must
never depend on a lane — that edge back is what would make the graph cyclic, and it is why
a lane cannot be offered as an extra. Anywhere that installs one needs its own step.

They share **one import namespace**: ``robovast/``, ``robovast/service/`` and
``robovast/execution/`` carry no ``__init__.py`` in any distribution, so they are PEP 420
namespace packages and the trees merge at import time. No import path depends on which
distribution a module ships in. In a deployed pod the core is installed editable from
``/opt/robovast/src`` while the cluster lane's files land in ``site-packages`` — one
namespace, two origins, no shadowing.

The practical consequence, and the reason it is worth the packaging: ``pip install
robovast-client`` is 13 packages and ~30 MB against the core's 88 and ~290 MB, and a
``vast`` assembled from it lists exactly the verbs it can run. See :ref:`client`, and
``AGENTS.md`` §5 for the rules a change here must hold.

Where the driver runs
----------------------

A campaign is driven by one :class:`robovast.execution.controller.CampaignController`
(batch or search) that runs **in the driving process**, against the backend for
that deployment:

* **local** — the ``vast`` CLI process *is* the driver, over ``DockerBackend``;
* **cluster** — the ``robovast-service`` runs the driver in a worker thread (one
  per campaign) over ``KubernetesBackend``, which creates the scenario Jobs.

There is **no separate per-campaign controller pod**. The service is already a
long-lived, in-cluster process with the cluster config, the Kubernetes API, the
object store and the workspace (plugins installed) — so it hosts the driver
directly instead of staging the project out and launching a second process to do
it. ``stop`` and live status are therefore in-process operations the service
exposes over the interface, not a network hop to a controller. Two things a
campaign still runs as their own Kubernetes workloads, because each needs to be
one: the scenario/postprocessing **Jobs**, and — only for variations that declare
one — a per-campaign **auxiliary-container pod** the driver execs into.

Because the driver's batch wait loop blocks on the running Jobs and the
cooperative-stop flag is only checked *between* batches (or search generations),
``ClusterService.stop`` also tears down that campaign's in-flight Jobs — reusing the
Kueue-aware, campaign-scoped ``cleanup_cluster_campaign`` (the same cleanup
``vast exec cluster run-cleanup`` performs). Deleting the Jobs unblocks the wait
loop (``get_remaining_jobs`` treats a gone Job as finished) so the campaign winds
down promptly; the ``"Hold"`` (never ``"HoldAndDrain"``) queue policy means other
queued/running campaigns are not preempted. **Service shutdown** (Ctrl+C on
``vast serve``, e.g. an off-cluster ``--backend cluster -x <context>`` driver) runs
the same teardown for *every* running campaign via the
``_terminate_running_campaigns`` hook — the cluster analogue of the local backend's
single-container kill — so a bare exit never orphans in-flight Jobs on the cluster.

A stopped campaign is reported as a **clean terminal**, not a failure. Ctrl+C also
tears down the storage port-forward (it shares the process group), so the driver's
finish work — upload-to-share (when enabled), postprocessing, finalize upload — would
otherwise fail against a dead endpoint and dump misleading tracebacks. Instead, the batch wait loop
raises ``CampaignStopped`` the moment it sees the cooperative-stop flag (before any
download), the controller sets phase ``"stopped"``, and the builders' finish tail
(``_finish_campaign``) is skipped — the per-run results the jobs already uploaded are
left as the campaign's output. The ``"stopped"`` outcome is persisted like a failure
(``_record_controller_outcome`` writes ``_execution/outcome.json`` and, on the cluster,
publishes it to the object store when the tunnel is still up — a Stop-button stop), so
the phase survives a service restart instead of reconstructing as an ambiguous
``"finished"``. Every terminal-phase filter (listings, the ``--wait-and-download``
waiter, cleanup's live-set) counts ``"stopped"`` as done.

Winding down is a race against uvicorn's graceful-shutdown deadline, so the signal
handler raises a process-wide flag (:mod:`robovast.common.shutdown`) *before* the
clock starts, and the layers that would otherwise fight the teardown consult it. Two
of them do. The driver's S3 client no longer restarts the ``kubectl port-forward`` on
a timeout that is simply the shutdown itself — and ``ClusterService`` refuses to open
one at all once the flag is up, so a read still in flight cannot resurrect the tunnel
and leak a ``kubectl`` child past exit. The SSE streams no longer *wait* for their
next pull either: a watchdog closes the stream the moment shutdown is announced and
abandons the worker thread, because a pull that returns after the deadline gets its
response task canceled and the cancellation logged as an "Exception in ASGI
application" traceback with the server already gone.

.. code-block:: text

           MCP tools ─┐
      vast CLI cmds ──┤→  RobovastClient (one interface)
         web UI ──────┘        │
          ┌────────────────────┼──────────────────────────────┐
     LocalTransport      HTTPTransport → single-host service   HTTPTransport → cluster service
     (in-process,        (`vast serve`, localhost or VM,       (in-cluster Deployment,
      DockerBackend,      DockerBackend, local FS)              KubernetesBackend, object store)
      local FS)

The service core is **backend-agnostic**: it dispatches execution to
``DockerBackend`` (local) or ``KubernetesBackend`` (cluster). See
:ref:`deployment` for the three deployment modes and how a client reaches each.

One container plan, built once
------------------------------

A campaign declares its containers by name (:ref:`execution.containers <config-containers>`)
and :func:`robovast.common.containers.plan_containers` turns that into the containers a run
actually starts. **Both lanes, the image builds, ``exec_in_container`` and the docs read that
one map.** A second lookup anywhere would be free to disagree with what the pod started, and
the disagreement would be silent -- a diagnostic entering one container while the campaign
ran another.

Two consequences worth stating, because they invert what every pre-v2 campaign assumed:

* **The scenario's container is not necessarily the campaign's "the image".** With a
  simulator or a system under test in its own container, "the main container's image" is no
  longer a synonym for "what this campaign is testing". Anything that treated the two as one
  -- digest capture, ``execution.yaml``, the compat check, ``exec_in_container`` -- resolves
  through the plan instead.
* **A role is not a container count.** ``scenario`` / ``simulation`` / ``sut`` are always
  valid names to ask about; one campaign backs all three with a single container and another
  with three. Only ``scenario`` is guaranteed to exist.

Simulators are declared, not assembled: a backend
(:mod:`robovast.common.simulators`, :doc:`simulators`) contributes container blocks, an
environment and a ``SimulationInterface`` ref before the plan is built, so nothing downstream
knows which simulator it is looking at.

**One thing in that plan is per-configuration: the simulator's own keys.** A world belongs to
a configuration, so each one resolves its own ``sim`` block over the campaign's default
(:ref:`sim-channel`). The plan is still built once and still holds one entry per container --
images, resources and package lists stay campaign-level, and every consumer of
``plan_containers`` is untouched. What varies is the simulator's *command* (one argv token:
the world) and the overrides document mounted beside the scenario channel's parameter file.
Both are produced where each lane already renders a per-job manifest, by the same backend
hooks ``apply_backend`` calls -- so a backend cannot answer one thing at composition and
another at dispatch.

That is also why the packer groups work items by their resolved block: containers start once
per job and are not restarted between packed items, so a job that mixed two worlds would run
the second configuration against the first one's compiled model.

Asking the simulator a question
-------------------------------

Some facts only the simulator has. Which plugins a world defines, what it is built from, and
which model values a run may change are all settled by resolving the world -- which needs the
simulator installed, and RoboVAST deliberately does not have it (the backend package is strings
and container specs, so the long-lived service carries no MuJoCo). A backend therefore answers
by returning a **question**, :class:`~robovast.common.simulators.ContainerQuery`: a command and
the image to run it in, whose one line of JSON RoboVAST reads.

Four rules make those answers trustworthy, and each of them was a bug first:

* **In the image the campaign runs.** Which world a ref even names depends on what is
  *installed*, so a query answered in a fixed base image describes a different world -- or none,
  for an experiment whose world ships in its own built image.
  :func:`~robovast.common.simulators.simulator_image` is the one place that decides, because
  :func:`~robovast.common.simulators.apply_backend` applies the same precedence to the *run* and
  a second copy would be free to disagree with it.
* **An unanswerable question says so.** ``WorldQueryUnavailable`` names which reason it is -- no
  backend, an unbuilt ``build:<tag>``, no container runner here, a command that failed. A
  pre-check that logged this at debug and carried on was indistinguishable from a check that
  passed, which is how a misspelled plugin key reached the container after the image pull.
* **Half an answer beats none.** A simulator that exits non-zero having still *printed* a payload
  answered what it could, and that payload is taken. A world whose model does not compile in the
  image (a ``*_ros`` world described where the colcon-packaged bridge does not resolve) can still
  say which plugin keys it has -- that half needs no build -- and discarding the reply cost the
  campaign a check it could have had. The rule is generic: nothing here knows which half was lost,
  because the simulator says so in the payload's own ``errors``, and each half that goes unchecked
  is warned about by name.
* **The lane is not implied.** The query runs a container, so a service offering both lanes
  routes it like ``exec_in_container`` does. In-cluster a container runner exists only *inside*
  a campaign's composition (a per-campaign aux pod), so the cluster lane refuses this query with
  that reason rather than quietly running ``docker run`` on the serve host. A standalone aux pod for
  one-shot queries is the follow-up that would lift it.

One function runs every such query --
:func:`robovast.common.config_generation.describe_world_payload` -- because the two callers
(the ``sim``-override pre-check, and
:meth:`~robovast.service.interface.RobovastInterface.describe_world` for a caller *writing* an
override) share all three traps.

Workspaces vs. campaigns
------------------------

Two independent identifiers:

* **``workspace_id``** — a server-side folder of *editable project inputs*
  (``.vast`` / ``.osc`` / run files / binaries). Clients need no filesystem of
  their own (:mod:`robovast.service.workspaces`).
* **``campaign_id``** — a *self-contained* campaign produced by running a
  workspace's project. It snapshots its project into ``_config/`` and is
  addressed by ``campaign_id`` alone.

**Workspaces are independent of campaigns.** Editing or deleting a workspace
never affects an existing campaign, and there is no campaign→workspace link.
Results/query operations key on ``campaign_id`` only.

A campaign does **record** which workspace and ``.vast`` it was launched from, on its
``campaign`` row (``origin_*``, see :mod:`robovast.common.store`) and on
``CampaignSummary.origin``. That is a *record*, not a link, and the difference is the whole
of it: nothing resolves it to run anything — a retrigger relaunches from the campaign's own
frozen ``_config/`` — so deleting the workspace it names still takes nothing with it. It
answers "where did this come from?", which is a fact about the past; it does not answer
"where do I re-run it from?", which is always the campaign itself. See
:ref:`web-ui-origin`.

**One project binding.** ``workspace_id`` is the only project binding the service
accepts, on every backend: a campaign always runs a **workspace's** ``.vast``, and
``config_path`` selects among several ``.vast`` files in that workspace. There is no
server-side "current project" — ``.robovast_project`` / ``vast init`` bind the *CLI's*
project (``vast exec local run``, ``vast results``) and never select
what the service runs. Omitting ``workspace_id`` is refused rather than resolved from
somewhere else, because the fallback that used to exist ignored ``config_path`` and so
could run a different ``.vast`` than the caller named.

**Pinned (read-only) workspaces.** ``vast serve --workspace-dir DIR`` registers a
directory as a workspace used *in place* rather than copied into the store: no
upload, present at start-up, and stable across restarts (the id is derived from
the resolved path). These entries live only in memory — never in
``registry.json`` — carry ``read_only=True``, and every mutating store op refuses
them (``WorkspaceStore._require_writable``); MCP/CLI/HTTP surface that as a clear
error.

Exactly **one** directory may be pinned. It holds as many ``.vast`` files as you
like — selected per campaign by ``config_path`` — so several pins would add no
expressiveness while leaving the service with no single sources root to report.
Pin the collection (a repo root), not each project.

Pinning needs the service to run on the host holding the directory, which decides
availability by deployment rather than by backend:

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Deployment
     - Pinning
     - How to bind a project
   * - ``--backend local``
     - yes
     - ``--workspace-dir``; edits on disk are live
   * - ``--backend cluster`` (off-cluster driver)
     - yes
     - ``--workspace-dir``; the driver reads inputs from this filesystem
   * - the deployed service (in-pod), reached over its Ingress
     - **no**
     - upload with ``vast workspace init``; edits need a re-push

Execution lanes are resolved, not imported
------------------------------------------

``vast serve`` runs one lane, fixed when it starts, and finds it through the
``robovast.execution_backends`` entry-point group — the same mechanism as simulators,
variation types and panel types, through the same resolver
(``robovast.common.plugin_ref.load_ref``). ``local`` and ``cluster`` each register a
class with a ``build()``; the core never names ``ClusterService``.

Two properties this exists for. Listing the lanes must not import them, so a caller can
report "the cluster lane is not installed" rather than raising ``ModuleNotFoundError``
from a module nobody asked for. And choosing one must not load the other: an in-pod
service has no Docker and should not import the local lane to discover that. Both are
pinned by ``tests/service/test_serve_backends.py``.

As with simulators, **a lane must import without the thing it drives** — it is imported
in a process that may have neither a kubeconfig nor a Docker socket. Reaching for either
belongs inside ``build()``.

That indirection is what lets the Kubernetes lane ship as its own distribution,
``robovast-cluster`` (``src/robovast_cluster/``), rather than as part of the core. An
install without it carries no ``kubernetes``, ``boto3`` or ``google-cloud-storage`` at
all, and still serves, validates, stores workspaces and runs local Docker campaigns.
Declining a lane is a supported configuration, not a degraded one — and where an agent is
involved, it is the strongest available guard: a lane that is not installed cannot be
silently substituted for the one you asked for.

The lane ships into the **same import namespace** as the core rather than under a name of
its own, so no import path changes: ``robovast/`` and ``robovast/execution/`` carry no
``__init__.py`` in either distribution, making them PEP 420 namespace packages whose two
source trees merge at import time. In a deployed pod the core is installed editable from
``/opt/robovast/src`` while the lane's files land in ``site-packages`` — one namespace,
two origins, no shadowing. Adding an ``__init__.py`` to either directory would break the
merge silently, so neither has one.

The direction of the dependency is load-bearing: ``robovast-cluster`` depends on
``robovast``, never the reverse. See ``AGENTS.md`` §5 for the packaging rules that follow
from it, including why a lane cannot be offered as an extra.

.. _container-exec-architecture:

Container exec: a diagnostic that cannot become a run
------------------------------------------------------

``exec_in_container`` runs one command in an experiment image. Everything about its design
follows from one requirement: it must be **structurally incapable of producing a result**,
not merely discouraged from it. An exec that could write ``/out`` or leave a
campaign-shaped directory would become the path of least resistance, and its output would
*look* like a campaign's while having no pinned provenance and no repetitions.

So: no campaign id, no campaign directory, ``/out`` never mounted, and ``OUTPUT_DIR``
pointing at a container-local path that dies with the container. A staged ``/config`` is
read-only, and the workspace — mounted at its own ``/sources/<id>`` address so a path from
``write_file`` is usable verbatim — is read-only too.

**Two sources, one path.** A workspace project (``workspace_id`` + ``config_path``) and an
existing campaign (``campaign_id``) both resolve to a *project directory*, because a
campaign's ``_config/`` is one. From there the staging is identical:
``build_campaign_data`` → ``filter_configs_by_name`` → ``prepare_campaign_configs``, and
the image resolves from that project's ``.vast`` exactly as a run resolves it.

**The entrypoint is rendered, never inherited.** ``prepare_campaign_configs`` substitutes
lane-specific init and post-run blocks into ``entrypoint.sh`` (``fixuid`` locally, config
fetch and S3 mirroring in-cluster), so a script rendered for one lane is wrong on the
other. Reusing a cluster campaign's staged entrypoint for a local exec would run cluster
post-run logic on a developer's machine. ``render_entrypoint`` exists so the bare-image
case can have that script without expanding a config tree it does not need.

**Why reuse the entrypoint at all**, rather than building an environment: the environment a
run sees is the ROS overlay, ``/ws/install``, the init block, ``execution.env`` and
``PRE_COMMAND`` — and it will grow. A diagnostic that reconstructed it would drift and
answer a different question than the run. Reuse also has a visible consequence worth
knowing: the entrypoint redirects its own stdout when given no argv, so a *started
scenario* logs to a file inside the container and the result carries ``log_path`` instead
of that output.

**A running campaign is never a target.** There is no code path from this operation to a
job's container or pod. An earlier draft made exec'ing a live campaign "safe" by annotating
its log; that was an admission the operation was wrong rather than a fix. To inspect a live
stack, the caller starts that stack in its own exec container.

**One container, so there is no state to manage.** At most one exists at a time, under a
fixed name (``robovast-exec`` — deliberately not the campaign container's single-flight
``robovast``, which ``LocalTransport`` force-removes to unblock a stop). That removes
session ids, a listing operation, and the leak class: a stray container is found and reaped
by name at service start. Its cost is reported instead of hidden — ``ResourceUsage`` carries
``exec_container``, so a lane with no room can be attributed to the caller's own container.

Two clocks bound it, and keeping them apart is load-bearing: an **idle** reap that runs
only while nothing the tool started is alive (otherwise a running scenario would be killed
while it worked), and a **hard deadline** at ``max(idle cap, limit + grace)`` so a project
declaring ``timeout: 900`` is not truncated at the idle cap. Limits are derived rather than
passed — ``ExecRequest`` has no timeout field — and the result reports which rule applied,
so a ``timed_out`` result names its own remedy.

The lane-specific half is a small protocol (``ExecLane``): ``DockerExecLane`` runs
``docker run``/``docker exec``, ``KubeExecLane`` an aux pod plus ``pods/exec``. Everything
else — validation, staging, limits, the lifetime state machine — is shared, as are the
pod primitives both in-cluster users need (``wait_pod_ready``, ``wait_pod_gone``,
``exec_stream`` in ``robovast.execution.cluster_execution.kube_client``; they live in ``common`` because the execution
engine may not import ``robovast.service``).

**The diagnostic stages the way a run stages.** In-cluster, ``/config`` is uploaded to the
object store and mirrored down by an ``mc`` init container on the shared sidecar image —
the same transport a campaign Job and an image-build context use. This replaced a
ConfigMap, which was simpler but capped the tree at ~900 KiB and answered "too big" with
"run it as a campaign instead", the exact expense this tool exists to save. It is
deliberately not the aux pod's tar-over-``pods/exec`` either: that needs the pod *running*
before its files exist, and needs ``tar``/``base64`` in an image we do not control, while
``mc`` is already required of every cluster experiment image. The deeper reason is that a
diagnostic with its own staging path can pass on a config the run would fail to stage.

Staging fits an init container because it happens exactly once per pod: the manager
discards a redundant staging when it reuses a held pod, and replaces the pod outright when
the identity changes, so ``/config`` never needs refreshing under a live pod. A staged tree
is torn down with its pod; a tree left by a dead service process is reaped at the next
stop, since nothing else would.

**The per-campaign aux pod stages the same way**, so there is one in-cluster transport
rather than three. Its contract is harder than the exec lane's — a *bidirectional* mirror
around every ``run()`` on a kept-alive pod, which an init container running once before
the pod starts cannot serve — so the init container injects ``mc`` into an ``emptyDir``
instead of fetching anything, and the runner mirrors through the store on each call. The
binary has to be injected because the aux image belongs to a plugin author and is not ours
to add tools to; that is the trick the rosbag postprocess Job already used to run ``mc``
inside the system-under-test's own image.

What this replaced was a base64 tarball piped through ``pods/exec``. It worked, but the
exec channel is a text websocket the client cannot half-close, so a receiver waiting for
EOF waited forever — observed for 2m47s against a live pod, on an *empty* workspace. Going
through the store needs no stdin at all, which removes that failure mode by construction
along with the ~1.33x encoding overhead and the whole-tarball buffering in the service.
The cost is that campaign *composition* now needs the object store reachable; it fails
loudly rather than falling back, since a second transport is the thing being removed.

.. _file-address-space:

One address space for files
---------------------------

Every file the service can reach has a single address, and that address **is** the URL
that serves it — the string a caller passes to ``read_file`` is the string it can
``GET``:

.. list-table::
   :header-rows: 1
   :widths: 34 46 20

   * - Address
     - Root
     - Writable
   * - ``/results/<campaign_id>/<path>``
     - the campaign directory (on the cluster, its object-store prefix)
     - **no**
   * - ``/sources/<workspace_id>/<path>``
     - the workspace's project directory
     - yes

Three properties are load-bearing.

**Content lives apart from control.** ``/campaigns/{id}/`` and ``/workspaces/{id}/`` are
*control* namespaces — a fixed vocabulary of service-owned verbs (``status``, ``logs``,
``query``, ``validate`` …, plus every name a service-endpoint plugin registers). A user's
config or output file may be called anything, so putting content there means a route can
shadow a file name — today, or the next time a route is added. Separate content
namespaces make "no reserved words, ever" true by construction.

**The namespace is the permission**, dispatched once rather than checked per operation:
there is no ``PUT``/``POST``/``DELETE`` route under ``/results`` at all, so a write there
is a 405 from the router. Results are immutable — on the cluster the local tree is a
cache of object-store objects, so a write would be a cache edit that silently vanishes.
Each namespace is confined against **its own** root via
``robovast.client.safe_path.safe_join``; a results address must never resolve inside a
workspace, or the read-only tree would inherit the writable one's permissions. The
cluster's results lane has no filesystem to resolve against, so it uses ``check_relative``
— the substrate-independent half of the same check — before composing an object key.

**The path is the real on-disk path.** ``<config_name>/<run_id>/<file>`` for a run
artifact; ``_config/``, ``_execution/``, ``_transient/``, ``_jobs/`` by their actual
names. (The predecessor route interposed a synthetic segment that matched no directory
and collided with the ``.vast`` ``run_files:`` inputs — two opposite meanings for one
name.)

A trailing slash means "directory": ``GET`` on it returns a listing, **non-recursive by
default** with a ``total`` — a campaign holds one directory per configuration and one per
run, so a recursive listing of its root is thousands of entries. (A bare owner,
``/results/<campaign>``, also lists: it can only be a directory.) The slash is how a
*client* says which representation it wants, not part of the path: ``list_files`` and
``read_file`` mean the same thing whichever way the address was spelled, because the
HTTP client normalizes before it builds the URL. Making the character load-bearing would
give one interface call two answers depending on which client made it. Listing entries are
plain names, directories suffixed ``/``, relative to the echoed address, so the next
address is a concatenation. ``GET`` on a file returns its bytes (mime-typed, so a browser
resource fetching siblings by relative URL resolves within its own directory);
``?as=text`` returns a paginated, binary-refusing text view. Paging happens **server
side** — reading 100 lines of a cluster log transfers 100 lines.

On the cluster, ``/results`` is served straight from the object store: a read is
``StorageClient.read_object`` (one object, not ``fetch_campaign``'s whole-prefix
download), and a non-recursive listing is a *delimited* ``list_entries``, so it is
non-recursive at the store and not merely in the response.

.. _fetch-what-the-caller-needs:

Fetch what the caller needs, not the campaign
----------------------------------------------

``ClusterService`` makes a caller **say which part of a campaign it needs**, because the
same answer is a directory read locally and an object-store transfer on the cluster:

``_query_dir``
    Just ``_execution/data.db`` and ``campaign.db`` — the only two objects
    ``data_query._open_db`` opens. Used by ``describe_campaign_data`` and
    ``query_campaign_data_sql``.

``_config_dir``
    The frozen ``_config`` snapshot: a handful of small objects. Used by the cheap
    readers — declared plots, panel assets, visualization workloads.

``_whole_campaign_dir`` → ``fetch_campaign``
    The whole prefix, downloaded into ``/tmp/robovast-campaigns/<id>``. For the callers
    that genuinely cannot know which files they will read: notebook rendering against run
    outputs, the ``/results`` address space, and the endpoint plugins reached via
    ``resolve_data_dir``.

``_data_dir``
    **Refused on this lane.** It is the local transport's "the campaign's directory",
    which on the cluster has no cheap answer.

The refusal is the design. While ``_data_dir`` silently meant ``fetch_campaign``, every
*inherited* method that touched it became a whole-campaign download — and nothing errored,
so the only symptom was slowness. A query arrived that way, so ``SELECT COUNT(*)`` over a
40 MB ``data.db`` pulled every rosbag the campaign produced, inside an HTTP request whose
client timeout was 30 s; the web UI survived it only because ``fetch`` sets no timeout at
all. ``list_campaign_plots`` arrived that way too, and the Results page calls it *per
campaign*, so opening the UI moved gigabytes to render a list of plot names.

Fixing those one at a time left the trap armed for the next method. Now a caller that
reaches for ``_data_dir`` fails immediately, naming the three alternatives, instead of
quietly moving a terabyte in production.

Two properties of the narrow path are load-bearing:

* **The cached copy is validated by size, not existence.** ``data.db`` is the one campaign
  object that is *mutable* — re-postprocessing rewrites it in place, which is what
  ``fetch_campaign(force=True)`` exists for. An existence check would pin the first version a
  service ever saw and serve stale metrics indefinitely.
* **It writes through** ``_download_atomic`` **and under the campaign's fetch lock.** The
  results explorer fires one query per sub-view on first load; without both, one request
  opens a ``data.db`` another is still streaming and SQLite reports "no such table: runs".

Both seams write into the *same* cache directory, so a later whole-campaign fetch finds the
two databases already at the right size and skips them, and ``delete_campaign`` still clears
one place.

The data-status probe (``GET /campaigns/{id}/data-status``, exposed as ``describe_campaign_data(preflight_only=True)``) reports whether a query would
transfer anything and what it would cost, so a caller can explain the wait *before* it waits.
It is bounded to two ``stat_object`` calls — a probe that itself enumerated the prefix would
only move the cost it exists to warn about. It is a **control** route, not a ``/results``
path: every segment there is a user-chosen file name, so a literal ``data-status`` under it
would shadow a campaign file of that name.

``get_service_info`` publishes both address templates, plus ``results_root`` /
``sources_root`` filesystem paths — but only when the service is local-filesystem *and*
the request came from loopback, so a caller is never handed a path it cannot open.

Token-efficient file transfer
------------------------------

Because inline tool content passes through an LLM's context (costing tokens
twice — once to generate, once per later turn), the file API is split:

* **Inline authoring is limited to ``.vast`` / ``.osc``** — the small text an LLM
  writes. ``edit_file`` sends a diff, so the validate→fix loop stays cheap.
* **Everything else uses the HTTP PUT side channel**: ``create_upload`` returns a
  one-time, TTL-scoped URL the client ``curl``\ s the bytes into
  (``curl -X PUT --data-binary @file <url>``), so run files, notebooks and
  binaries never enter the token stream. Executability is preserved (explicit
  flag or ``#!`` shebang auto-detect).

Data flow and result access
---------------------------

For cluster campaigns, results live in the **object store** (the durable home);
the service is a stateless gateway that streams finished campaigns from it —
``GET /campaigns/{id}/archive`` tars the campaign's objects **on the fly** into the
response (no scratch on the service, nothing buffered in memory), which is what
``vast results download`` and the web UI **Download** button use. The external
``tar.gz`` share is **opt-in at launch** (``upload_to_share``): when set, the driver
streams a raw, pre-postprocessing archive to the share the moment the runs finish,
*before* postprocessing — so the shared copy stays minimal while the object store
(and the postprocessed download) carry the derived data. For a local ``vast serve``,
the durable home is simply the local filesystem, so it has no share and refuses the
archive route.

Analysis postprocessing is **editable and re-runnable**: the raw rosbags are
always preserved, so ``results_processing.postprocessing`` entries can be changed
and re-run to compute different metrics later without re-executing the campaign.
Because it is config (not captured data), an edit **overwrites that block in the
campaign's own ``_config/<name>.vast`` in place** — no override files, no revisions
(:mod:`robovast.service.postprocessing_edit`); the raw rosbags and the as-ran
``configuration``/``execution`` are untouched. A re-run is **dispatched in the
background** and shows up in the campaign view as a live ``postprocessing`` phase (see
:ref:`results-retrigger`).

.. _database-or-address-space:

What goes in the database, and what stays a file
------------------------------------------------

A campaign writes a lot of artifacts, and each is reachable in exactly one of two ways:
as rows in a database, or as a file through the :ref:`one address space
<file-address-space>`. The rule deciding which:

   An artifact belongs **in the database** when it is a *per-entity record you filter,
   aggregate, or join across runs* — and stays **a file** when it is a *whole document
   read once*.

"One row" is not the test; *aggregatable* is. A campaign's ``_execution/execution.yaml``
is a single document, but its contents (which robovast, which image) are exactly what one
compares *across* campaigns — and the SQL interface can attach several campaigns at once —
so it is lifted onto the ``campaign`` row. Applied to what a campaign writes:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Artifact
     - Home
   * - each run's ``test.xml``
     - DB — ``campaign.run`` (per-run record, counted constantly)
   * - each job's ``sysinfo.yaml``
     - DB — ``campaign.job``; "did the slow runs share a host?" is a join
   * - per-run metric CSVs
     - DB — one ``data.db`` table per CSV stem, after postprocessing
   * - ``_execution/execution.yaml``
     - DB — provenance columns on ``campaign.campaign`` (+ ``execution_json``)
   * - ``_transient/postprocessing.yaml``
     - DB — ``data.db``'s ``postprocessing_steps``; the file stays, as the PROV-O input
   * - the ``.vast``
     - Both — ``campaign.config_json`` for effective values, the file for authored intent
   * - each configuration's ``_config/sim.config``
     - File — what the simulator was given, a whole document read once (by a person
       replaying a cell, or by the run view). Its *effective* values already reach the DB
       through ``campaign.unit``, so a second copy would be a second source of truth
   * - ``_execution/outcome.json``
     - File — the campaign's terminal status, read by ``get_campaign_status``
   * - ``_execution/killed_jobs.json``
     - File — the jobs an operator stopped by hand (``stop_job``), keyed by job artifact
       dir. It *becomes* DB content: ``read_run_outcome`` turns each entry into
       ``campaign.run.status = 'killed'`` for the runs it cut short, so the queryable fact
       has one home. The file stays because the two writers differ — the **service**
       records the kill, the **controller** writes ``campaign.db``, and a SQLite file
       shared between them would be a race. It exists only for a campaign somebody
       intervened in, which is what keeps every other campaign's read path unchanged.
   * - ``_execution/launch.yaml``
     - File — how the campaign was *asked for*; a whole document, read once by a
       retrigger. Its ``config_filter`` / requested ``runs`` are comparable across
       campaigns ("which of these were pilots?"), so by the rule above they are a
       candidate for lifting onto the ``campaign`` row later, exactly as
       ``execution.yaml``'s were. Nothing aggregates them yet, so nothing is lifted.
   * - ``_transient/configurations.yaml``
     - File — already duplicated into ``campaign.unit``; a second copy would drift
   * - ``run_log`` (the merged per-run log)
     - DB — one row per log *event*, with ``sim_time``, container, node, severity. Built by
       postprocessing from the container streams **joined with** ``/rosout``: the two carry
       the same events (473 of 521 on a measured campaign), so concatenating them would
       report most of a run twice. ``rosout`` itself is *not* a second table -- it is a
       source of this one, selected with ``WHERE source = 'rosout'``. See
       :ref:`merged-run-log`.
   * - ``resource_usage`` (per-container CPU/memory)
     - DB — one row per container per process name per ~1 s tick, with ``timestamp``,
       ``in_window``, ``cpu_percent``, ``memory_rss_bytes``. Built by postprocessing from the
       job's ``resource_usage_<container>.csv``, which stay the record of what was sampled.
       In the DB because it answers a question about a *result*: a lane gives a job fixed
       cores, so a starved stack is a competing explanation for what a run did, and ruling
       that out means joining it to ``runs.available_cpus`` and to the behaviour itself.
       Unlike ``run_log``, a packed job's ticks are **partitioned** between its runs rather
       than shared — another run's CPU is not this run's. See :ref:`per-run-resource-usage`.
   * - ``system.log``, ``controller.log``
     - File + the log tools — the raw bytes, and the **live** case is the point of reading
       them: ``data.db`` does not exist while a campaign runs. The tools reduce them on read
       (``min_severity``, ``summarize`` — see :ref:`mcp-liveness`). What *is* stored is the
       derived, time-aligned ``run_log`` above; the files stay the record of what was
       actually printed

Two consequences worth stating. A **file** is never *also* a table: putting
``configurations.yaml`` in the DB would create a second source of truth for the resolved
configuration list. And a **document in a cell** is not a queryable artifact:
``campaign.config_json`` holds the whole ``.vast``, which exceeds the per-cell limit and
comes back truncated, so it is queried through ``json_extract`` or the ``config_view``
rows — never ``SELECT config_json``.

Querying results
----------------

Per-run metrics are consolidated into ``<campaign>/_execution/data.db`` (one
table per CSV stem) plus a ``runs`` **dimension table** — per-run
``status``/``duration_s`` and each scenario parameter as a ``param_*`` column.
That ``runs`` table is the analytics-wide *view* over ``campaign.db``'s ``run``
table (the operational source of truth for per-run outcomes, written live from
each ``test.xml``); see :ref:`the store schema <campaign-store>`. The MCP
``run_data`` plugin exposes read-only **SQL**
(``query_campaign_data_sql`` + ``describe_campaign_data``), with ``campaign.db``
attached as schema ``campaign`` — so ``campaign.run`` is queryable for raw
pass/fail even before postprocessing builds ``data.db``. Joining ``runs`` to any
metric table on ``(config_name, run_id)`` answers "how does *<param>* affect
*<metric>*" in one query. Analysis notebooks read the same ``data.db`` through
:mod:`robovast.common.analysis.db`, which scopes a table to the notebook's ``DATA_DIR`` (see
:ref:`evaluation-reading-results`).

**Versioned, never migrated.** The layout is stamped into ``PRAGMA user_version``
(``DATA_DB_SCHEMA_VERSION``), and no migration table goes with it — the deliberate difference
from ``campaign.db``, whose :data:`~robovast.common.store.SCHEMA_VERSION` does carry one. The
store is *authored*: written as the campaign runs, and the only record of what happened, so an
old one must be upgraded in place. ``data.db`` is *derived*: postprocessing deletes and rebuilds
it from the run directories, which keep their CSV/JSONL, so the upgrade path for an old one is
to run postprocessing again — which re-executes no trial, and needs neither ROS nor the
campaign's execution image, since only the ``rosbags_*`` → CSV step does and everything after
it is plain Python. A migration here would be code maintained to reproduce what the builder
already does.

The stamp does not gate reads, because a version is too coarse to answer the question a reader
actually has: most campaigns on disk predate it, and many carry everything a given notebook
needs. What gates a query is whether the columns are there, which the reader checks directly;
the version only sharpens the error when they are not.

**Two flat views carry the joins, so a caller cannot omit one.** ``run_view`` (one row per
run: config, status, duration, params, search round, host record) and ``config_view`` (the ``.vast`` as
one row per key) are created on the query connection as ``TEMP`` views, and are queried
unqualified. They exist because a forgotten join does not raise — ``run_id`` is unique only
*within* a configuration, so a query filtering on ``run_id`` alone silently returns rows
from every configuration and averages across them. Making the join part of the schema
removes that failure mode rather than documenting it.

They are views on the *connection*, not objects in the file, because ``campaign.db`` is
attached read-only (nothing may be written to it), because a store predating the ``job``
table would otherwise carry a view over a table it does not have, and because a change to
a view then never needs a schema migration. Where the underlying tables are missing,
``run_view`` keeps its column set and reports NULL for the host and ``batch`` columns — one
query shape for every store version, with "not recorded" reading as NULL rather than as a
broken query. That the views are computed per query is also what makes a *new* column
retroactive: adding ``batch`` gave every campaign already on disk its search history back,
with no migration and no re-postprocessing.

``describe_campaign_data`` lists both views first and carries the canonical query for each
question a caller is likely to ask — the per-run lookup, a configuration's parameters, how
a metric was produced. That is deliberate: the replacement for a tool has to be discoverable
from the tool output an assistant already reads, not only from this page.

**Columns are typed from the data, not left as text.** A CSV yields only strings, so an
untyped ingest makes every comparison lexicographic — ``ORDER BY timestamp`` puts
``"10.022"`` before ``"9.5"``, shuffling a trajectory and producing a path length that is
wrong by a factor rather than an error. So ingest infers a type per column
(:mod:`robovast.results_processing.csv_types`): a column whose every non-empty value is a
plain decimal number becomes ``INTEGER``/``REAL`` and is stored numerically, and everything
else stays ``TEXT`` verbatim. The rule is deliberately strict — one ``n/a`` demotes the
column, and ``"007"``/``"nan"`` are text (a zero-padded identifier must keep its text, and
NaN has no SQLite representation, so accepting it would delete data instead of typing it).
Scenario ``param_*`` columns are typed the same way from their resolved values.
``describe_campaign_data`` reports each column as ``"name TYPE"``, which is what tells a
caller whether a column can be ordered directly or needs ``CAST(col AS REAL)``. A
``data.db`` built before typed ingest is all-``TEXT``; re-running postprocessing retypes it.

**The declaration never outlives the evidence.** A column is declared by the first run that
writes it, but the evidence is every run — a later one can turn an ``INTEGER`` column real,
or a numeric column textual. A stale declaration is not cosmetic: a schema claiming ``REAL``
over a column holding one ``'n/a'`` makes ``AVG()`` return a plausible wrong number (SQLite
reads the text as 0) and ``MAX()`` return the text itself — the original bug wearing a
different hat. So after the last run is ingested, any table whose verdict moved is rebuilt
with the corrected types and its values carried over (``_retype_table``), which also
normalizes a mixed column so every row in it is text. Only affected tables are touched, so
the usual case rebuilds nothing.

Because a type alone cannot say "numeric except in the runs that failed", such a column is
also recorded in ``data.db``'s ``_column_notes`` and surfaced by ``describe_campaign_data``
as ``column_notes`` on the owning table — postprocessing logs a warning too, but no SQL
caller ever reads that log. The note names the fix (exclude the text rows, e.g.
``WHERE col GLOB '[0-9-]*'``) because ``CAST`` alone would silently read them as 0.

Run view (web panel framework)
------------------------------

The web :ref:`Run view <run-view>` is a small **panel framework**, built so it can grow
(more panels, a future 3D scene, and eventually a live/streaming data source) without
reworking the core. It has four decoupled contracts, and panels only ever see the bottom
three:

* **Declaration** — the campaign's ``.vast`` carries a top-level ``visualization.results.run_view.panels``
  list (:class:`robovast.common.config.VisualizationConfig`), served to the UI by
  ``list_campaign_panels`` (the same raw-load pattern as ``list_campaign_plots``) with the
  *contributed* panels merged in: ``config.ALWAYS_ON_PANELS`` (the ``playback`` transport, which
  no run view can do without) and the configured simulator's ``default_panels``. Merged there
  rather than in the UI, so the served list and the view cannot disagree; a campaign that
  declares the type itself keeps its own entry. The frontend normalizes the result against each
  panel type's registry defaults (``frontend/ui/src/lib/panels/parsePanels.ts``).
* **Registry + host** — panel plugins self-register (``frontend/ui/src/lib/dashboard/registry.ts``);
  ``PanelHost`` resolves each spec's anchor/size to CSS and mounts the component. Adding a
  panel is one ``registerPanel`` call.
* **Shared panel kit** — ``@robovast/panel-kit`` (``frontend/panel-kit/``) holds the panel contract
  (``PanelProps`` / ``PanelSpec`` / ``DataProvider`` / ``PlaybackClock``) and the clock-driven
  scaffolding (``useCanvasClock``, the time-index binary search, ``keyframes``). It exists
  because the host and the package-provided panel *remotes* are separately-built npm packages
  that must agree: a remote shares only ``react``/``react-dom`` at runtime, so before this it
  kept a hand-maintained copy of the host's types and its own copy of the scaffolding — and the
  drift is what let a staleness bug live in one panel and nowhere else. Both consumers resolve
  it **by path** (a ``tsconfig`` ``paths`` entry plus a matching vite ``resolve.alias``); it has
  no build of its own and is deliberately *not* an MF ``shared`` module, so each side bundles
  its own copy and a version skew is an ordinary build rather than a remote-load failure.
* **Clock** — ``PlaybackClock`` (``frontend/panel-kit/src/clock.ts``) is the single shared
  time source (seconds on the rosbag timeline). The playback panel is the only writer; the
  rest subscribe. It is an external store, so the ~display-rate ``t`` updates while playing
  don't re-render the tree.
* **Data seam** — ``DataProvider`` (declared in ``frontend/panel-kit/src/dataProvider.ts``, implemented
  by ``dbDataProvider`` in ``frontend/ui/src/lib/dashboard/dataProvider.ts``) is how a panel gets
  rows/frames by table + time, decoupled from transport. Today it reads one run's rows from
  ``data.db`` through the existing ``query``/``describe`` endpoints, plus the dedicated
  ``costmap`` endpoint for grids. The interface (``nearest`` / ``series`` / ``timeRange`` /
  ``has`` / ``fetchRun``) is shaped so a future ``liveDataProvider`` over a live topic buffer
  drops in without touching any panel.

**Camera delivery.** A recorded video is the one panel input that never passes through the data
seam: the panel resolves a ``videos`` row to a URL (``DataProvider.runFileUrl``) and puts it in a
``<video src>``, and the **browser** does the fetching, ranged requests and all. Nothing streams
media through the service's Python. The ``videos`` row exists because the encode re-times frames
onto a constant rate and drops the bag stamps, so ``t_start`` is the only thing that can place
the file on the clock; the ``get_camera_frame`` MCP tool reads the same row, so the two surfaces
cannot hold different opinions about when a recording began. The panel is a **reader** of the
clock like every other, which is why it shows no controls of its own — two things in charge of
"now" is a run view disagreeing with itself.

That path is also why ``local_file`` has to dispatch per lane. ``FileResponse`` is what carries
``Range``, and the route asks the transport for a path outright rather than probing for the
method: every transport has it (they all subclass ``LocalTransport``), so a presence check can
only ever succeed. While one was believed to be meaningful, a cluster campaign fell through to
the *local* resolver, whose ``_data_dir`` used to mean ``fetch_campaign`` — pulling an entire
campaign to serve one file. Each lane now answers with its own cost: local hands back the path,
the cluster fetches the single object behind the address. (That fall-through is also what
``_data_dir`` refusing on the cluster now catches outright, rather than by being slow.)

**Screenshots are the deliberate opposite of geometry.** ``scene_cache`` builds a scene
descriptor per *world*, so one build serves every run that used it — worth a background thread,
a cache and a status to poll. ``robovast.service.screenshot`` renders one moment of one run from
a caller's viewpoint, so its key would be a camera pose and a time and would never be hit twice:
no cache, no thread, one synchronous ``POST`` that returns the image or the reason. That second
part matters as much as the first — an asynchronous render would have to stash its failure
somewhere the caller could find later, which is exactly the in-memory dictionary that makes a
failed scene build visible to nothing but ``get_run_scene_status``.

**Costmap delivery.** Occupancy grids can't ride the generic CSV flatten (a grid becomes
thousands of per-cell columns, past SQLite's column limit; and the read path caps a cell at
2 KB). The ``rosbags_costmap_to_csv`` handler
(:class:`robovast.results_processing.data.rosbags_process.CostmapToCsvHandler`) instead
decodes each grid once during postprocessing and re-encodes it compactly — int8 cells
zlib-compressed, base64 in a ``costmaps`` table row with the pose/geometry metadata. The
``/campaigns/{id}/costmap`` endpoint (``robovast_nav``'s ``CostmapEndpoint``, a
``robovast.service_endpoints`` plugin — it is *not* a core interface operation) delivers the
frame nearest a time **untruncated**; the browser inflates it with the native
``DecompressionStream``. The ``costmaps`` table description in ``describe_data_db`` gives an
LLM the map's size in meters, resolution, and layers for spatial reasoning without decoding
grids.

Alongside the frame it returns ``t_prev`` / ``t_next``, the timestamps recorded either side of
it for that topic. This is the one panel that fetches **per clock position** rather than
preloading its series, so "nearest" alone is not an interpretable answer — the query always
returns something, however far away, and the panel could not tell a current frame from the
first or last one clamped to a cursor minutes off. From that pair
(``frontend/panel-kit``'s ``frameValidity``) it derives the interval over which the frame stays the
nearest one — so it re-requests only when the answer could change, and a latched topic such as
``/map`` is fetched once per session — and the local publish period, hence how far the cursor
may drift before the layer is reported as absent instead of drawn as current. A latched topic
has no neighbors, so it is exempt from staleness *by construction* rather than by name.

That staleness threshold is floored at the panel's own **fetch cadence**, which is not a detail:
nav2 publishes costmaps far faster than any viewer fetches them (50 Hz local costmaps are
ordinary, against a ~140 ms round trip plus throttle). Deriving the threshold from the publish
rate alone inverts there — every frame is judged stale within 40 ms, long before it could be
replaced, and the layer is blanked essentially permanently. A frame older than the publish
period *because the viewer sampled coarsely* is not stale data; the threshold exists to catch
real gaps and off-the-end clamps, which are seconds.

The interface surface
---------------------

The operation contract (Phase 0 + workspaces + postprocessing shown; data-query
lives in the ``run_data`` MCP plugin):

* **Workspaces** — ``create_workspace`` / ``list_workspaces`` / ``get_workspace``
  / ``delete_workspace`` / ``create_upload``. A workspace's *files* are not separate
  operations: they are the writable half of the file address space below.
  ``create_workspace`` also takes ``from_campaign``, which seeds the new workspace from
  that campaign's frozen ``_config/`` — the one bridge from the read-only results tree
  back into editable inputs, and a reconstruction rather than a copy (the scenario is
  placed where the ``.vast`` declares it, as a retrigger does).
* **Files** — ``list_files`` / ``read_file`` / ``read_file_bytes`` / ``write_file``
  (``.vast``/``.osc`` only) / ``edit_file`` / ``delete_file`` / ``create_upload``, each
  taking one **address** (see :ref:`file-address-space`). The upload grant is
  ``POST /uploads`` rather than a workspace sub-route: its request already names the
  workspace in its address, and a path segment that had to agree with it would be an
  argument the handler either ignores or has to re-check. Bulk directory sync is **client-side
  glue over these primitives**, not a new operation: ``sync_directory_to_workspace``
  (in ``robovast.service.project_push``) walks a local directory and drives
  ``write_file`` for ``.vast``/``.osc`` and ``create_upload`` for everything
  else, with an optional ``prune`` that deletes workspace files absent locally. It is
  the single implementation behind two callers — the
  ``vast workspace init`` / ``vast workspace update`` CLI commands and the web UI's
  drag-a-folder upload — so both
  stay transport-agnostic (in-process ``LocalTransport`` or HTTP client) and can
  never drift.
* **Campaigns** — ``create_campaign`` (backend implicit in the deployment;
  ``upload_to_share`` is a per-campaign launch flag on the request, not a separate
  operation) / ``get_status`` / ``list_campaigns`` / ``list_jobs`` / ``get_job_log``
  / ``stop`` / the ``/archive`` stream. ``list_jobs`` + ``get_job_log`` are the **live per-job** view
  (the current batch's execution units and a single running job's log); each transport
  implements them over its own source — the local job dirs + their ``logs/system*.log``
  (``LocalTransport``), or the campaign's Kubernetes Jobs + ``read_namespaced_pod_log``
  (``ClusterService``). Both merge **every** container the job runs into one append-only
  stream through the shared ``common.log_tail.MergedLogBuffer``, since a job is not one
  container: the ROS shape gives the simulator and the system under test their own, and
  on the cluster those are *native sidecars*, which live in ``spec.initContainers`` and
  are found via ``cluster_execution.kube_client.pod_workload_containers``. They report live state only; the persisted per-run logs remain
  part of the campaign result data, served by ``get_campaign_logs`` unchanged.
  ``GET /campaigns/events`` is a **browser-only** Server-Sent-Events transport over
  the same ``list_campaigns`` pull (the same server-side-loop idiom as the campaign
  log stream, so there is no second enumeration to drift): it pushes the full list on
  connect and on every change, which is how the webui shows a launched campaign — and
  its ``building`` / ``variation`` / ``running`` / … phase — the instant it is
  registered, without polling. ``list_campaigns`` stays the authoritative pull for
  MCP and the CLI. Both draw from one rule: a campaign tracked in the in-process
  registry reports its live ``ControllerState``; an untracked one is reconstructed
  from its recorded facts (``reconstruct_status_from_disk`` over ``_record_dir``) — the
  same precedence ``get_status`` uses. Which campaigns *exist* is the union of three
  sources: the results directory, the registries of what is being driven, and — for a
  lane whose durable home is not that directory — an object-store index
  (:ref:`campaign-discovery`).
* **Postprocessing** — ``get_postprocessing`` / ``update_postprocessing`` /
  ``run_postprocessing``. The structured ``*_postprocessing`` pair is the programmatic
  API (MCP, CLI); ``get_postprocessing_source`` / ``update_postprocessing_source`` are
  the YAML-text twin the web UI re-run dialog edits in Monaco (mirroring the run-view
  ``*_panels_source`` visualization editor). Both overwrite the edited block in the
  campaign's own ``_config/<name>.vast`` in place. ``run_postprocessing`` dispatches the
  re-run in the background and returns at once (watch the campaign view for progress).
* **Data query** (MCP ``run_data``) — ``describe_campaign_data`` /
  ``query_campaign_data_sql``.

.. _image-resolution:

Why images resolve the way they do
----------------------------------

:doc:`images` says what the mechanism is. This is why it is that, because it is the part
someone will otherwise re-litigate.

A family image ref glues three independent facts into one string, and authoring all three
in one place is what made image configuration a five-variable problem::

    harbor.example/robovast / robovast-roqsim : latest
    \_______ WHERE _______/   \____ WHAT ___/   \ WHICH /
       deployment config         never a choice   version

**WHAT is never authored.** Which member a container needs follows from its role and the
campaign's mode. Once that is granted, a ``.vast`` has no reason to mention a family image
at all, and the question that used to be hard — what should an override do to a
digest-pinned ref in a published dataset's config? — stops existing rather than being
answered.

**``family:<member>`` mirrors ``build:<tag>``.** Both are symbolic refs core resolves late
from context the author does not hold, and both fail loudly if one reaches a container spec
unresolved. The alternative considered was a rewrite pass that matched family
repositories in already-concrete refs and moved them to another project. It was
rejected: it needs a
whitelist of names to match, and anything a whitelist can match by mistake it can
*redirect* by mistake — a campaign's own ``sut`` image silently pulled from somewhere the
author never named. A prefix marker cannot do that, because writing it is a request.

**Resolution is per-campaign and explicitly threaded.** ``image_project`` rides on
``CreateCampaignRequest`` → ``RunOptions`` → ``build_campaign_data`` →
``generate_scenario_variations``, and is resolved in one pass right after ``apply_backend``.
Two properties force the threading rather than an ambient module-level or context value:
a campaign is composed in a worker thread, and a ``plugins:``-declaring config is composed
in a subprocess of that thread (which is why the value travels in the compose worker's job
file, not its environment). The service drives several campaigns concurrently, so a process
-wide value would be whichever campaign set it last. The composition cache key includes the
project for the same reason: the composed data carries resolved refs, so an entry composed
against one project must not satisfy a request for another.

**The image build is a second resolution point, threaded the same way.** A build spec is not
read from the composed campaign data — it is re-derived from the raw config, because it has to
exist *before* composition (the images are what the campaign then runs in). So
``extract_build_specs`` calls ``apply_backend`` itself, and therefore has to resolve what that
contributes itself: ``image_project`` rides ``CreateCampaignRequest`` → ``RunOptions`` →
``_start_build_images`` / ``_resolve_built_images`` → ``extract_build_specs`` on both lanes.
Missing this was asymmetric, which is what hid it: a container taking the default member was
resolved on the composition path, so ``sut`` and ``scenario`` built correctly while the one
container declaring a ``backend:`` carried ``family:robovast-roqsim`` into its Dockerfile's
``FROM``. Docker read that as repository ``family``, tag ``robovast-roqsim``, and the campaign
died in BuildKit with a registry ``insufficient_scope`` — a credentials error three layers from
the cause. ``generate_dockerfile`` now refuses either prefix outright, so the promise that an
unresolved ref fails loudly holds for a build and not only for a pod spec.

Resolving into the campaign data — rather than at each point of use — is what makes
``_execution/execution.yaml`` record a concrete image. Postprocessing reads that record to
choose the image it deserializes rosbags in, so a symbolic ref surviving there would be a
``family:`` string handed to Kubernetes as an image name.

**Where an image lives is a lane's answer, not a caller's.** Everything above is about the
*recipe* — which containers build and what their inputs hash to, one derivation shared by
both lanes. Where the resulting image ends up is the other half, and it is the half that had
no name: :class:`~robovast.service.image_store.ImageBuildStore`, with the local docker daemon
and the deployment's registry as its two implementations. A lane overrides exactly one
factory (``_images``); every question asked of a store — what is this image called here, is
it actually here — is then written once.

That seam is the fix for a class of bug rather than one bug. Before it, the local store was a
class and the cluster's identical responsibilities were nineteen methods of
``ClusterService``, so ``self._image_builds`` existed on **both** lanes and quietly answered
wrongly on one. Every cross-lane concern needed an override someone had to remember, and one
was forgotten: ``exec_in_container`` ran ``docker image inspect`` inside a service pod that
has no docker, and reported every built image in the registry as unbuilt. Two rules the ABC
now carries, because both were learned the expensive way:

* ``present()`` **raises when it cannot tell.** "I could not check" and "it is not there"
  are different answers; collapsing them is what turned a missing *dependency* into a
  missing *artifact*, three layers from the cause. The build path wants the opposite trade
  and says so explicitly (``_registry_has_image``): uncertainty means rebuild, because a
  redundant push is cheap and a wrong cache hit leaves pods in ``ImagePullBackOff``.
* only ``ImageRef.identity`` may cross the API boundary. The concrete ref is a docker tag on
  one lane and a registry-qualified ref on the other, and the second would carry registry
  knowledge to a client that must not have it — so there is exactly one field that is
  allowed out, rather than a rule to remember at each return.

A diagnostic against a **campaign** does not use a store at all: the campaign recorded which
image each role ran on, and ``campaign_role_image`` returns those bytes. Re-deriving a hash
from the campaign's frozen ``_config/`` cannot work anyway — that snapshot holds the ``.vast``
and the run files, not the build inputs — and the recording is the better answer regardless,
because a diagnostic about a run should run what the run ran.

**A registry is not a node, and only one lane needed telling.** The store answers where an
image *lives*; whether a pod can *start* it quickly is a different question, and the two lanes
answer it differently for a reason worth writing down. Locally they are the same question:
``buildx --load`` on the pinned ``docker`` builder writes into the daemon store the runner
reads, so a built image is runnable the instant the build returns. On the cluster the build
pushes to a registry and stops there — the image exists and is on **no node** — so whoever
runs it first pays the whole multi-GB pull. That is usually ``exec_in_container``, whose entire
justification is answering "is the package installed?" in seconds; the web UI's 3D panel even
names the wait out loud (*Fetching the simulation image onto the node*).

So the cluster lane pulls the image onto a node as soon as it knows the image exists, with a
throwaway Job whose one container *is* that image running ``/bin/true``
(:mod:`~robovast.execution.cluster_execution.image_warm`). **The pull is the work** — nothing
reads the Job's result, so a broken entrypoint or a nonzero exit warms the node just as well,
which is what makes it safe to point at an arbitrary experiment image. It fires where the
service *learns* an image is in the registry rather than where a caller asks for one: after a
build's ``succeeded`` transition, at the two cache-hit returns, and — for the resolved base,
concurrent with the build itself — at submit. A campaign that builds inherits all of it, because
those fire points are on the build and not on the caller.

The **family** images are warmed from a different place and for a different reason:
``vast exec cluster setup`` / ``upgrade``, which is both the moment every node is cold for the
whole family — a tag bump or a moved project means the next campaign pays a full pull of
``robovast-roqsim``, the largest image there is — and the moment it is free, since the pod is
being restarted anyway so nothing is mid-campaign. It resolves from the caller's own
``ROBOVAST_PROJECT`` / ``ROBOVAST_PROJECT_TAG``, which is correct precisely because those are
the values being baked into the service pod, so it warms the set the deployment is being
pointed *at*. ``robovast-controller`` is excluded: it *is* the Deployment, running
``imagePullPolicy: Always``, so the kubelet pulls it during the rollout already happening.
This also covers what the build fire points cannot — a campaign whose containers declare no
``build:`` section runs family images that no build ever produced, so there is no
build-completion transition to hang a warm on.

They are also warmed with a different *shape*, and the asymmetry is the point. A Job schedules
one pod and therefore warms one node, which is the right answer for an experiment image: the pod
that wants it is the next one to run, and the only one that will. Any node may run any cell of a
sweep, so for the family that leaves every other node to pay the pull — so the family gets a
**DaemonSet** instead, one container per image, each asleep. An exited container's image is
collectable again, so init containers would warm a node and then let it go cold; a running
container is what stops the kubelet reclaiming the bytes. The cost is a few millicores and tens
of mebibytes per image per node, against nodes sized for a simulator.

Its containers run ``imagePullPolicy: Always`` while every campaign pod runs ``IfNotPresent``,
which is deliberately backwards and is the whole reason the DaemonSet earns its place. A floating
tag is never re-pulled under ``IfNotPresent`` once a node holds bytes for it, and campaign pods
keep ``IfNotPresent`` precisely so that a sweep does not depend on the registry being reachable
at every pod start — which leaves this DaemonSet as the only place a re-pushed ``:latest`` can
reach a node at all. It buys freshness, not reproducibility: a pinned ``ROBOVAST_PROJECT_TAG`` is
still what makes two runs comparable, and with a floating tag ``Always`` can just as well drift
new bytes into the middle of a sweep. The pod template carries the same restart annotation the
service Deployment does, because without it a re-pushed floating tag leaves every field
byte-identical, so the patch rolls nothing and ``Always`` never gets a container to start.

Being a DaemonSet, it gives up the self-collection below: it is meant to persist, so teardown
removes it explicitly. Skipping that would leave a pod on every node holding multi-GB images for
a deployment that no longer exists.

Three properties of the **Job** shape, and each replaces machinery rather than adding it:

* **Idempotent by name.** The Job name is derived from the image ref, so a duplicate create is
  a 409 meaning "already warming". No in-process record, and a service restart changes nothing —
  the same trick ``build_id_for`` uses to make a resubmit idempotent.
* **It terminates itself.** ``ttlSecondsAfterFinished`` is *not* sufficient, and assuming it was
  would have reproduced a bug this codebase already paid for: TTL starts only once a Job is
  terminal, and with ``backoffLimit: 0`` a pod wedged in ``ImagePullBackOff`` leaves both
  counters at zero and the Job ``active`` forever. That is precisely why the build path carries a
  ``blocked``-phase probe. A prewarm has nobody watching it, so it gets
  ``activeDeadlineSeconds`` instead of a watcher.
* **It never fails its caller.** A failed prewarm leaves exactly the situation that held before
  the feature existed — a slow first pod — so raising would turn a missed optimization into a
  failed build. It warns instead, and the warning is load-bearing: nothing reads a prewarm back,
  so the log is the only place a permanently broken one can surface.

Deliberately outside Kueue, which is the same choice the build Job makes (campaign and
postprocessing Jobs are the ones carrying the queue label). A prewarm admitted behind a full
sweep would warm the node *after* the thing that needed it, which is worse than not warming; the
cost is a few millicores of quota Kueue has not accounted for, and no Workload object at all.

It is **not** wired to the 3D panel's geometry compile, though that is the one place a human
watches this wait and the panel names it out loud. The compile runs in
``campaign_role_image(..., SIMULATION_CONTAINER)`` — the exact image that campaign's simulation
container ran — so at the moment a campaign finishes, the image is by construction already on
the node that ran it, and a prewarm there would find it present and exit in a second. The wait
the panel reports is the *later* one: a view that lands on a node which never ran that campaign,
or one whose kubelet has since garbage-collected the image. Neither is reachable from a
campaign-completion hook — the first is the placement limit below, and the second would evict a
prewarmed copy just as readily. Warming at view time is no better, since ``AuxPodSession``
creates a pod with that image immediately anyway and a second pod would race the same pull for
no gain. So the honest answer is that this particular latency needs multi-node warming rather
than another fire point — which the family DaemonSet now provides for a campaign that ran a
family image directly, and still does not for one that built its own.

Two further things it deliberately does not do. It does **not** fire on the restart branch of
``get_image_build_status``, which holds only a ``build_id`` — and ``build_id_for`` does not
reverse into a ref, since it lowercases and folds ``_`` to ``-`` where ``concrete_image_ref``
does not, so a tag like ``my_sut`` would yield a ref no registry serves and a prewarm that
reports nothing while warming nothing. And ``exec_in_container`` does not *wait* on a prewarm in
flight: the kubelet serializes pulls per node, so a pod asking for the same ref queues behind the
prewarm rather than duplicating the transfer, and a wait loop would add latency without moving a
byte earlier.

**The registry stays authoritative.** Warmth is an optimization on top of it, never a source of
truth: ``present()`` still asks the registry, a node holding an image is not evidence it was
pushed, and nothing about digests or provenance changes. Placement is where the Job shape is
still best-effort — it carries no ``nodeSelector``, and neither does the exec pod, so on a
single-node cluster they are necessarily the same node and on a larger one an experiment image
may be warm on a node the exec pod does not get. The family DaemonSet has no such limit by
construction, and carries no ``nodeSelector`` either for the opposite reason: it wants every
node. It does mirror the ResourceFlavor's toleration, read from where that flavor is written, because a
warm pod that does not tolerate what campaign pods tolerate would skip exactly the nodes worth
warming — and report success doing it.

**The pod env is the site default; the request overrides it.** That ordering is the whole
reason a dev run needs no redeploy. It is also a bug fixed: of the five per-image variables
that used to exist, only two were ever carried into the service pod, so
``vast exec cluster setup --force`` appeared to move the images and moved only the
controller — three of the five were read in-pod and set nowhere.

**Reproducibility lives in the recorded digest.** ``resolve_robovast_image`` used to refuse
a mutable default outright, so that the image a campaign ran was always pinned by its
author. That rule aimed at the right thing from the wrong layer: it made every ``.vast``
carry a registry-specific string, which is how a shipped example came to pin a private
registry only one site could pull. What makes a run reproducible is the digest captured
*from* it (``pullable_digest`` / ``_capture_image_digest``, replayed by
``from_campaign``) — a fact about what happened, not an intention recorded beforehand. The
unpinned case now resolves and warns.

The default tag is ``latest`` and not this installation's version. Deriving it reads well
and is wrong: it assumes every version has a published tag, and CI publishes semver tags
only for ``v*`` pushes. Tried, it produced ``robovast:2.0`` — a tag no workflow had ever
built — which is precisely the failure the whole change set out to remove.
