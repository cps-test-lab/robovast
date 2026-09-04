.. _cluster-execution:

Cluster Execution
=================

RoboVAST can execute scenarios at scale on a **Kubernetes cluster**,
running each run configuration as an independent Job and collecting results
via a built-in MinIO S3 server.  This section covers everything from cluster
setup and job queueing to multi-context workflows and cloud-provider-specific
configuration.

Overview
--------

Every cluster run — batch **and** search — is driven by the
**robovast-service**, which runs the campaign *in-process* (one worker thread per
campaign) and creates the scenario Jobs itself. So cluster runs require a deployed
service (``vast cluster setup`` installs it); ``vast workspace run`` finds it — a service answering on the conventional local port, or the deployed
one ``vast login`` recorded — pushes the local project into a server-side
workspace, and starts the campaign there. It is *fire-and-forget*
— it returns immediately with the campaign id, and the campaign continues in the
cluster.

A campaign runs a *workspace's* project, named by the pair (workspace, path). Push the
directory once, then launch by name::

   vast workspace run growth_sim growth_sim.vast \
        --description "pilot: new inflation radius"

``--description`` is one line saying what the run is *for*. Set it: it is what tells
two same-day ``<name>-<timestamp>`` campaigns apart in the monitor and the web UI.
Repetitions come from the ``.vast``'s ``execution.runs`` unless ``--runs`` overrides
them. Internally:

1. **Launch** — The client pushes the project to a workspace and calls
   ``create_campaign``. The service starts a :class:`CampaignController` in a
   worker thread over ``KubernetesBackend``. No per-campaign controller pod is
   created; the service is the driver.

   The workspace is named after the project's directory and **reused** by later
   launches of the same project — it is overwritten (after asking; the default is
   yes, and off a terminal it proceeds and says so), and project files it holds that
   the local directory no longer has are removed, so it mirrors what is on disk. What
   the *service* generated inside it (``.cache/``, staged plugins) is left alone, as
   is ``results/`` — a campaign's output is not project input. Reusing the workspace
   is what keeps one per *project* rather than one per *launch*; pass
   ``--workspace NAME`` to push somewhere else.

   A workspace a campaign is **currently running from** is refused, naming the
   campaign: a campaign reads its project out of the workspace for its whole life (a
   search campaign re-composes from it every generation), so a push would change an
   experiment mid-flight. ``WorkspaceInfo.running_campaigns`` is what answers that —
   live state held by the service driving the run, never a stored campaign→workspace
   binding, because a *finished* campaign is workspace-independent.
2. **Config upload + job creation** — The driver composes each batch, uploads the
   scenario configurations to the storage bucket, and creates one Kubernetes
   ``Job`` per packed job. Each job runs an ``initContainer`` that pulls its
   config files from storage and a main ``robovast`` container that executes the
   scenario. (Variations that declare an auxiliary container get a per-campaign
   aux pod the driver execs into during composition. Composing *outside* a campaign —
   ``preview_configurations`` — gets the same pod held by the container-exec manager
   instead, so an authoring loop reuses one warm pod and idleness reaps it.)
3. **Queueing** — a Job is created only when sufficient CPU/memory is available,
   so a campaign cannot oversubscribe the cluster. Step 2 therefore paces itself
   against this rather than creating the whole plan up front.
4. **Result collection** — Jobs upload result files back to the storage bucket,
   and the driver publishes the **canonical campaign** (``campaign.db`` +
   ``_execution`` + results) there. The **object store is the durable home and
   the delivery mechanism**: the service streams downloads straight from it
   (``vast campaign download`` / ``--wait-and-download``), so no external share is
   required. Pushing a copy to an external ``tar.gz`` **share** is opt-in **at
   launch** — enable *Upload to share when done* in the web UI launcher (or
   ``--upload-to-share`` / the MCP ``upload_to_share`` flag). When set, the driver
   streams a **raw, pre-postprocessing** archive to the configured share the moment
   the runs finish, *before* analysis postprocessing adds derived data — so the
   shared copy stays minimal and untouched. Track progress with ``vast cluster monitor``; ``vast cluster store-cleanup`` removes the
   buckets once results have been handled.

.. note::

   Live status, ``stop`` and ``monitor`` all go through the service — auto-detected
   on the conventional local port, or the one ``vast login`` recorded — there is no
   controller pod to ``kubectl port-forward`` into any more. The web UI additionally
   streams each campaign's ``controller.log`` live from the service, and offers a
   **Download** button (the postprocessed ``tar.gz``, streamed from the object
   store) on finished campaigns.


Prerequisites
-------------

The following tools must be installed and available on ``PATH`` before using
cluster execution:

.. list-table::
   :header-rows: 1
   :widths: 15 55 30

   * - Tool
     - Purpose
     - Install
   * - ``kubectl``
     - Communicate with the Kubernetes cluster (apply manifests, port-forward,
       wait for pods)
     - `kubectl install guide <https://kubernetes.io/docs/tasks/tools/>`_
   * - ``helm``
     - Install and upgrade the NVIDIA device plugin on a GPU cluster, via the
       Helm chart registry
     - `helm install guide <https://helm.sh/docs/intro/install/>`_
   * - ``k9s`` *(recommended)*
     - Terminal UI for monitoring pods, jobs, and logs in real time — not
       required but greatly simplifies observability during a run
     - `k9s install guide <https://k9scli.io/topics/install/>`_

For GCP clusters the ``gcloud`` CLI is additionally required — see
:ref:`cluster-config-gcp` below.

To render on a GPU, the **node** additionally needs the NVIDIA driver and the NVIDIA
container toolkit installed on the host. Nothing in RoboVAST installs those: they are the
node administrator's, and the toolkit is what lets a container be handed the device at all.
RKE2 and k3s register a ``nvidia`` RuntimeClass when the toolkit is present, and that is the
signal RoboVAST detects — ``kubectl get runtimeclass`` tells you whether a cluster has it.
Everything above the host is provisioned by setup; see :ref:`cluster-gpu` below.


Cluster Setup
-------------

Before the first run, deploy the MinIO S3 server into the cluster:

.. code-block:: bash

   vast cluster setup <cluster-config>

Available cluster configs (``--list``):

.. code-block:: bash

   vast cluster setup --list

Setup acts on the *cluster*, not on a project: it reads nothing ambient and runs from any
directory. Its one optional input from a ``.vast`` is the control-pod node labels, taken
only from a config you name explicitly with ``--vast`` (:ref:`below <cluster-node-labels>`).

The setup command:

* Deploys a ``robovast`` pod containing the MinIO S3 server (embedded-storage
  configs such as ``rke2``). External-storage configs (e.g. GCS) deploy no
  helper pod — the bucket is used directly.
* Makes GPUs schedulable where the cluster has them, so a simulation container renders in
  hardware instead of in software (:ref:`below <cluster-gpu>`). A cluster without GPUs is
  left exactly as it was.

.. _cluster-node-labels:

Pinning pods to a node pool
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Both node pools are set on the **setup command**, never in a ``.vast``. A ``.vast``
describes a campaign; which machines a cluster's pods may run on is a property of the
cluster, and carrying it in a campaign file put a deploy's lasting, cluster-wide decisions
somewhere that travels with an experiment.

.. code-block:: bash

   vast cluster setup rke2 \
       --jobs-node-label node-pool=primary \
       --control-node-label node-pool=extra

``--jobs-node-label`` confines campaign jobs. It is recorded in the service's environment,
because the admission controller is what enforces it: the controller counts free capacity
only on nodes inside the pool, and every job pod carries the labels as a ``nodeSelector``
so kube-scheduler is bound by the same rule the accounting assumed. Neither half suffices
alone — filtering capacity would leave the scheduler free to place outside the pool, and
stamping alone would have admission reserving room on nodes the pods cannot reach.

``--control-node-label`` places RoboVAST's own infrastructure pods. It narrows rather than
decides: these are ANDed with the node-local data placement setup chooses (see below).

Both are repeatable, and both are written on **every** setup. Omitting one therefore
*clears* what a previous setup configured rather than preserving it, which is what keeps
the command the whole truth about the cluster. With neither given, pods schedule wherever
Kubernetes puts them.

.. _cluster-cpu-governor:

Pinning the clock
^^^^^^^^^^^^^^^^^

``setup`` sets the nodes' CPU governor to ``performance``. **On by default**, and alone among
what setup does it reconfigures the host rather than reporting something about it — so it is
the one step to know about before pointing setup at a machine you share.

It is on by default because a cluster used for measurement whose clock moves with load
produces numbers that are wrong in a way nothing downstream can detect or correct.

A node on a scaling governor changes clock speed with load, so a per-node figure a campaign
records — CPU usage, realtime factor, run duration — is taken against a clock that was not
the same for every run. Fixing the governor removes that variable; it does not claim to be
the only one.

``--no-performance-governor`` skips it and leaves the hosts alone. Naming
``--performance-governor`` explicitly changes the *failure* policy rather than the outcome: a
cluster that refuses the DaemonSet is then an error instead of a warning, because someone who
asked for a fixed clock and silently did not get one would go on to trust measurements taken
on a scaling one.

It installs a privileged DaemonSet with the host's ``/sys`` mounted writable, confined to
the job node pool when one is configured. Leaving it off is supported: RoboVAST reports a
``cpu_governor_scaling`` warning per campaign, so the effect shows up in the results rather
than silently.

``vast cluster cleanup`` removes that DaemonSet — the owner, not its pods, which a DaemonSet
would recreate at once — and says so. **It does not put the nodes back.** The pods do not
restore the previous governor when they stop, and nothing recorded what it was, so a node
stays on ``performance`` until something changes it; a reboot returns it to its own
configured default. On shared hardware that is a real lasting side effect, which is why
cleanup prints it rather than reporting a clean teardown.

.. warning::

   **On a cloud VM this usually cannot work, and the failure is not the one setup detects.**
   Setup recognises a cluster that *refuses* the privileged pod — GKE Autopilot does — and
   warns. GKE Standard and EKS generally **accept** it, and the DaemonSet then fails at
   runtime: a GCE or EC2 guest has no writable ``/sys/devices/system/cpu/*/cpufreq``, because
   the hypervisor owns the clock. The pod exits non-zero and ``CrashLoopBackOff``\ s on every
   node while setup reports the DaemonSet as applied. Node auto-repair would undo the setting
   anyway. On managed Kubernetes, pass ``--no-performance-governor`` and set the governor
   through the node image instead — and read the ``cpu_governor_scaling`` advice, which is
   what tells you whether it took effect.

.. _cluster-node-local-storage:

Where this deployment's own data lives
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The selectors above place job and control pods. This places the deployment's **own** state,
which is node-local by default: a stock cluster ships no StorageClass, so ``hostPath`` is what
the object store, the campaign index, the workspaces, the results, the registry and the build
cache all fall back to.

**Where it goes** is one flag, ``--data-root``; **which node** it goes on needs no flag at
all. The two are separate questions and are answered separately below.

Setup decides the node once, and records the decision as a node label:

.. code-block:: bash

   vast cluster setup rke2

.. code-block:: text

   robovast.io/data-node: not found on any node -- picking by free space
     node-a                            186.8 GB free  (kubelet-nodefs)  <- chosen
     node-b                             48.6 GB free  (kubelet-nodefs)
   node node-a labelled robovast.io/data-node=true
   ✓ Cluster setup completed successfully!
     workspaces, registry and store on node-a (chosen automatically: most free disk)
     build cache alongside it on node-a (--buildkit-node puts it on another disk)
     recorded as a node label, so a later cleanup + setup returns here without any flag

Because a node label is **cluster-scoped**, it outlives ``vast cluster cleanup``.
A later ``setup`` reads it back and lands on the same node with no flags at all:

.. code-block:: text

   robovast.io/data-node: node-a (existing label)

That is the point of the label. Without one, a ``cleanup`` + ``setup`` leaves no trace of the
previous placement, the scheduler is free to choose again, and on a heterogeneous cluster it
will: the service comes up on a different node with an **empty registry** -- the blobs still on
the old node's disk, intact and unreachable -- while setup reports success. The **Disk**
meter changes at the same moment, because it reports the filesystem of the node carrying the
service pod (see :doc:`web_ui`), which is then a different machine.

The pods carry the label as a **constant** ``nodeSelector``, never a hostname. That is what
makes the pin impossible to lose: there is no node *value* for a caller to forget, and so
none for an ``upgrade`` to drop.

How the node is chosen
~~~~~~~~~~~~~~~~~~~~~~

In order, stopping at the first that applies:

#. ``--data-node NODE`` / ``--buildkit-node NODE``, when given.
#. The existing label -- the ordinary path on every run after the first.
#. The eligible node with the most free space. *Eligible* means Ready, not cordoned, and
   carrying no taint the pod does not tolerate: ranking on size alone would pick a
   ``NoSchedule`` control-plane and leave the pod ``Pending`` while setup reported the
   placement as chosen. Free space comes from the kubelet's ``stats/summary``, falling back
   to ``allocatable.ephemeral-storage`` where ``nodes/proxy`` is not readable; the log names
   which was used, because they measure different things.

Two rules keep it sticky:

* **The build cache follows the data node**, not "the other one". Auto-separating would put
  a 150 GB cache on whichever node was left over, which is usually the smaller. So
  ``--data-node`` alone moves *both* labels -- one name for the whole of this deployment's
  on-disk state, because a cache left behind on the old node is exactly the stranded-bytes
  surprise the flag exists to make visible. Separate them with ``--buildkit-node`` where the
  disk is tight: a full builder disk on the service's node becomes DiskPressure evictions of
  the API, and once named, the cache stays where it was put.
* **Naming a node is enough to move the placement.** Typing a node name is already the
  deliberate act, so there is no second confirming flag. What the move costs is *reported*
  rather than guarded: setup names the node the label came off, and the bytes there are
  **not** migrated -- the new node starts with an empty registry and rebuilds what it
  needs.

Nothing is pinned where nothing is on a node: pass ``--registry-class`` /
``--workspaces-class`` (or use a provider whose store is a bucket) and the pods schedule
freely, because a ``nodeSelector`` on a provisioned volume is noise at best and
unschedulable at worst.

``--control-node-label`` still applies and is **ANDed** with the
placement label rather than replaced by it: it narrows which nodes may be chosen, while the
label decides which one of them holds the data. A pool selector alone still lets the pod
float within the pool, which is this same problem at a smaller scale.

Which directories it uses
~~~~~~~~~~~~~~~~~~~~~~~~~

The node label above decides *which machine*. This decides *where on it* — normally with one
flag, because the ordinary case is a machine with a disk mounted for RoboVAST:

.. code-block:: bash

   vast cluster setup rke2 --data-root /media/data

That places every node-local directory this deployment keeps:

.. code-block:: text

   /media/data/store           the object store, where finished campaigns live
   /media/data/index           the campaign index, placed beside it
   /media/data/workspaces      the service's workspaces
   /media/data/results         the campaign results root, placed beside them
   /media/data/registry        the built experiment images
   /media/data/buildkit        the build cache

Each tenant can also be named on its own, and overrides the root for itself:

============================  ==============================  =================================
Flag                          Environment                     Default
============================  ==============================  =================================
``--data-root``               ``ROBOVAST_DATA_ROOT``          *(unset)*
``--store-path``              ``ROBOVAST_STORE_PATH``         ``/var/lib/robovast-store``
``--store-class``             ``ROBOVAST_STORE_CLASS``        *(unset — a hostPath)*
``--store-size``              ``ROBOVAST_STORE_SIZE``         ``500Gi`` (needs a class)
``--workspaces-path``         ``ROBOVAST_WORKSPACES_PATH``    ``/var/lib/robovast-workspaces``
``--workspaces-class``        ``ROBOVAST_WORKSPACES_CLASS``   *(unset — a hostPath)*
``--registry-path``           ``ROBOVAST_REGISTRY_PATH``      ``/var/lib/robovast-registry``
``--registry-class``          ``ROBOVAST_REGISTRY_CLASS``     *(unset — a hostPath)*
``--buildkit-path``           ``ROBOVAST_BUILDKIT_PATH``      ``/data/robovast-buildkit``
``--buildkit-class``          ``ROBOVAST_BUILDKIT_CLASS``     *(unset — a hostPath)*
``--buildkit-size``           ``ROBOVAST_BUILDKIT_SIZE``      ``200Gi`` (needs a class)
============================  ==============================  =================================

Every one reads an environment variable, so a ``.env`` — or ``~/.config/robovast/env``, for
what is true of the machine rather than of a project — sets them once instead of on every
``setup``.

**Two tenants take no flag**, and for the same reason: one pod holds each pair, and derived
data must not be separated from its source. The campaign results sit beside the workspaces and
share their backing, because the service pod mirrors a campaign between them. The campaign
index sits beside the object store and shares *its* backing, because every row in the index was
ingested from a campaign in the store -- an index that outlived its sources would answer
questions about campaigns nobody can reproduce or check, confidently. A flag able to separate
either pair could only ever be ignored or refused.

In order, the first that answers wins: what you stated (flag or environment), then
``--data-root``, then **what the cluster is already doing**, then the default. That third step
is why a re-run with no flags leaves a moved deployment where it is — and why ``--data-root``
outranks it: recovery answers for a caller that said nothing, while a root is a caller saying
where this deployment's data goes. Ranked the other way, a root would move the registry and
the build cache and leave the workspaces behind.

Two settings that cannot both apply are refused rather than ordered, before anything is
applied:

.. code-block:: text

   --registry-path and --registry-class both place the registry, and they cannot both
   apply: a class provisions a volume, a path names a directory on the node. Pass one.

A class and a path for one tenant is the case worth catching: the class wins, so the path is
accepted, reported, and ignored. ``--buildkit-size`` without ``--buildkit-class`` is the same
failure a step smaller — it sizes a claim nothing will create.

.. note::

   ``--data-root`` places this deployment's **own** state, not the scratch a run produces on
   its way to the store. A campaign job's working directory is an ``emptyDir``, so it lives
   under the kubelet's root directory on whichever node the job ran — which is right: an
   ``emptyDir`` is per-pod, isolated and reclaimed automatically, and a shared host directory
   would put concurrent runs on one node in each other's way. On a node whose root filesystem
   is small, moving *those* means moving the kubelet's root directory, which is the node's
   configuration rather than RoboVAST's.

Moving it, and forgetting it
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   kubectl get nodes -L robovast.io/data-node -L robovast.io/build-node   # where is it?
   vast cluster setup rke2 --data-node node-b                   # move it
   vast cluster cleanup --forget-placement                      # forget it

The move says what it abandoned, which is the only place that node is ever named again:

.. code-block:: text

     workspaces, registry and store on node-b (as requested)
     build cache alongside it on node-b (--buildkit-node puts it on another disk)
     moved here from node-a; the bytes written there are NOT migrated
     so this deployment starts with an empty registry and rebuilds what it needs

None of these move the **data**. The workspaces and registry bytes stay on the old node's
disk; a moved deployment starts with an empty registry and rebuilds what it needs. The
results store does not come with it either, and that one is not cheap: it holds every
campaign this deployment has finished. Archive what matters (``vast share``) before moving
the placement.

One thing a re-``setup`` cannot do by itself: applying a manifest over an object that already
exists keeps the existing one, so a **running** store Pod cannot be relocated that way. Setup
refuses rather than reporting a placement it did not apply -- delete the pod, or run
``cleanup`` first.

.. _cluster-gpu:

GPU rendering
^^^^^^^^^^^^^

A simulation container renders its cameras offscreen with MuJoCo. Given a GPU it uses EGL;
without one it falls back to software rendering, which is correct but roughly an order of
magnitude slower and burns a dozen CPU cores doing it. On a sweep that is often the
difference between a campaign that finishes and one that does not.

**There is nothing to configure.** Setup detects a GPU and makes it schedulable, and the
container that runs the simulator then requests one because the cluster advertises it — no
flag, and no change to the ``.vast``. The same file renders in hardware on a GPU cluster and
in software on a CPU one.

.. code-block:: bash

   vast cluster setup rke2 -x local     # provisions GPUs if the cluster has them

What that does, when a GPU is found: installs the `NVIDIA device plugin
<https://github.com/NVIDIA/k8s-device-plugin>`_ with time-slicing so several pods can share
one card, and puts ``runtimeClassName: nvidia``
plus ``NVIDIA_DRIVER_CAPABILITIES=all`` on the pods that ask for one. That capability is the
load-bearing part: the container runtime's default (``compute,utility``) hands over the
device *without* the GL half of the driver, so the container gets a GPU it cannot render on
— and nothing errors, the job is simply slow. roqsim refuses to start in that state rather
than quietly producing a slower result.

A cluster with no GPU is not a problem to report: nothing is installed, no manifest changes,
and setup does not fail. The same is true if the plugin cannot be installed or never comes
up — unless GPUs were asked for explicitly, which turns those into errors.

**Concurrency and VRAM.** ``--gpu-replicas N`` sets how many pods may share one physical GPU:

.. code-block:: bash

   vast cluster setup rke2 -x local --force --gpu-replicas 24
   vast cluster setup rke2 -x local --no-gpu       # opt out entirely

``N`` caps concurrency; it does **not** partition device memory. Nothing in Kubernetes, in
the plugin, or in the driver gives each pod a share of VRAM — all ``N`` renderers allocate
from the same card, first come first served — so ``N`` is an assertion that ``N``
simultaneous trials fit in it. Exceed that and a trial's simulator fails mid-run.

Measured on an RTX A2000 12GB, one 640×480 offscreen context per pod, against a 337 MiB
baseline (the node's desktop session):

.. list-table::
   :header-rows: 1
   :widths: 20 25 25 30

   * - Concurrent contexts
     - GPU memory used
     - Above baseline
     - Marginal per context
   * - 1
     - 430 MiB
     - 93 MiB
     - 93 MiB
   * - 4
     - 711 MiB
     - 374 MiB
     - 93 MiB
   * - 8
     - 824 MiB
     - 487 MiB
     - 60 MiB
   * - 16
     - 1574 MiB
     - 1237 MiB
     - 77 MiB

So the cost is **sub-linear** — the driver shares part of its allocation across contexts on
one GPU — and 16 concurrent renderers used 13% of the card, with GPU utilization at 45% while
each rendered at 10 Hz. On this hardware VRAM is nowhere near the binding constraint: it is
CPU quota that limits campaign width, which is why the default sits above that ceiling.

Do not read those figures as a budget for your own worlds. A 640×480 framebuffer of a trivial
scene is the floor: the framebuffer scales with the requested frame size, each camera builds
its own renderer, and meshes and textures are extra. Re-measure with ``nvidia-smi`` on the
node during a real campaign, or from the per-run ``resource_usage`` table, before raising the
default for a heavier world.

The default of 16 is chosen to sit *above* the CPU ceiling, so GPU quota is not what limits a
campaign: a three-container scenario job asks for roughly ten cores, so a 96-core node admits
about nine concurrent jobs either way. Raising ``N`` therefore changes nothing until per-job
CPU drops — which GPU rendering itself makes possible, by freeing the cores software
rendering was using.

The value is not stored anywhere. The node's advertised capacity is the record, because
unlike a remembered number it cannot go stale::

   kubectl --context local get node <node> -o jsonpath='{.status.capacity.nvidia\.com/gpu}'

A bare re-run of ``setup --force`` preserves whatever count is deployed, so it will not
quietly undo a deliberate ``--gpu-replicas 24``.

**Opting a campaign out.** Set ``gpu: 0`` on the simulation container to leave the GPU alone
— worth doing for a camera-less world, which never renders and would otherwise hold a
replica for nothing:

.. code-block:: yaml

   execution:
     containers:
       simulation:
         resources:
           gpu: 0

``gpu`` also takes the per-cluster form, for one ``.vast`` across a GPU and a non-GPU
cluster: ``gpu: [{local: 1}, {gcp-c4: 0}]``.

**Which GPU did a run actually use?** ``sysinfo`` records it, so it is a query over a
finished campaign rather than something inferred from wall-clock:

.. code-block:: sql

   SELECT config_name, run_id, sysinfo_json FROM run_view

giving a ``gpu`` block per run — ``render_node``, ``nvidia_model``, ``nvidia_driver`` — which
together say whether that trial could render in hardware and on what. A GPU present with no
render node is the ``graphics``-capability mistake described above.

Which backend was *bound* is a property of the process that rendered, not of the node, so it
is asked there instead: ``roqsim.rendering.bound_gl_backend()`` and ``bound_gl_device()``, and
roqsim logs the pair at INFO when the application configures logging. The device string is the
informative half — ``egl`` alone is what any machine with a working hardware GL stack reports,
and on a multi-GPU host it does not say which card the driver picked.

.. note::

   Fix a campaign's GPU concurrency and record it. Time-slicing inflates each trial's
   rendering time, so cells that ran at different concurrency are not comparable with each
   other — a result that looks like an effect of the variable under study.

To tear everything down after use:

.. code-block:: bash

   vast cluster cleanup

Cleanup removes the device plugin too, but never the ``nvidia`` RuntimeClass or the host's
driver and toolkit: those belong to the cluster and its node administrator.

Cleanup deletes named objects rather than the namespace, so what it removes is a list rather
than a sweep: the campaign Jobs and their pods, the image-warm DaemonSet, buildkitd, the
``robovast-service`` Deployment and Service, the controller RBAC, the CPU governor DaemonSet,
the NVIDIA device plugin, and whatever the cluster config's own ``cleanup_cluster`` owns.
Deliberately kept: the object store (the durable data home), buildkitd's volume claim, the
node identity labels, and the placement labels — see :ref:`cluster-node-local-storage`, and
``--forget-placement`` for the last of those. The one thing it cannot undo is the CPU
governor setting itself; see :ref:`cluster-cpu-governor`.


Running Scenarios
-----------------

.. code-block:: bash

   # Run every config the workspace's .vast expands to
   vast workspace run my-experiment my.vast

   # Override the number of runs from the CLI
   vast workspace run my-experiment my.vast --runs 5

   # Run only the configs matching a name or glob (batch campaigns)
   vast workspace run my-experiment my.vast --filter my-config

``run`` is fire-and-forget: it starts the campaign on the service and returns
immediately, printing the campaign id. The campaign continues in the cluster —
watch it with ``vast cluster monitor``. (It needs a reachable service:
auto-detected on the conventional local port, or the one ``vast login`` stored; run
``vast cluster setup`` first if you have none.)


Monitoring and Results
----------------------

Check the status of a running (or recently completed) run:

.. code-block:: bash

   vast cluster monitor

The service publishes the finished campaign to the object store automatically, and
``vast campaign download`` (or ``run --wait-and-download``) streams it from there — no
external share needed:

.. code-block:: bash

   vast campaign download campaign-2025-06-01-120000
   # -> ./campaign-2025-06-01-120000.tar.gz

That is the whole command. It fetches the campaign as this service holds it —
postprocessing and all — writes one ``.tar.gz``, and stops: nothing is extracted, no
results directory is written into, and no state is kept about what you already have.
The stream is end-to-end, so a ~1TB campaign is never buffered on the service or in
memory. What you do with the archive afterwards is yours; to put it back into a
service, ``vast campaign import <archive>``.

The share's raw, pre-postprocessing copy is a different system, reached through
``vast share`` (see :ref:`cluster-sharing`). To push a copy there, either enable it
**at launch** (*Upload to share when done* in the web UI, ``--upload-to-share`` on
``vast workspace run``, or the MCP ``upload_to_share`` flag) or export a
finished campaign with ``vast share export -i <campaign-id>``.

Clean up only the job objects (without touching the result storage):

.. code-block:: bash

   vast cluster jobs-cleanup
   vast cluster jobs-cleanup --campaign campaign-2025-06-01-120000

Remove result buckets from the object store (after uploading or when no longer
needed). This runs **through the robovast-service** — it holds the object-store
credentials, so no local credentials are needed and a bulk delete never removes a
campaign that is still running:

.. code-block:: bash

   vast cluster store-cleanup
   vast cluster store-cleanup --campaign campaign-2025-06-01-120000

The service is auto-detected on the conventional local port, or named by ``vast login``
(``-x`` context, ``-n`` namespace) to tunnel to the in-cluster service for the call.
``jobs-cleanup --data`` deletes the buckets the same way after removing the Jobs.


Push notifications (ntfy)
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Because a run is fire-and-forget, the campaign driver can push `ntfy.sh
<https://ntfy.sh>`_ notifications so you don't have to poll ``monitor``. Set a
topic in your ``.env`` and subscribe with the ntfy mobile/desktop app:

.. code-block:: ini

   ROBOVAST_NTFY_TOPIC=robovast-alice-campaigns   # enables notifications
   ROBOVAST_NTFY_SERVER=https://ntfy.sh           # optional, this is the default
   ROBOVAST_NTFY_TOKEN=tk_xxx                      # optional, for protected topics

You then get a message when a campaign **starts**, when each **batch finishes**,
once an **hour** with the current run progress, when it is **uploaded** to the
share, and exactly one message when the campaign **ends** — whether that end is
a finish, a **stop**, or (urgently) a **failure**.

Re-running a campaign also announces itself, on the topic of the campaign it was
re-run *from*: the new campaign gets its own id, and someone following the old one
otherwise has no way to learn which. It is not an ending — the source campaign is
unmodified and still sends its own — and the re-run announces its own start as
usual.

The ending message is worth reading rather than glancing at. It is sent when the
campaign is genuinely over, *after* postprocessing rather than when the last run
stops, and it carries what the campaign actually produced: the run tally, and any
postprocessing or upload failure. A campaign whose trials all passed but whose
postprocessing failed is reported as "finished WITH PROBLEMS" — it has no CSVs
and nothing queryable, and reporting that as a clean finish made a campaign with no
metrics look identical to a complete one on the one screen nobody re-reads.

Notifications outlive whatever started the campaign, which is what makes them the
right thing to rely on for a long sweep: a CLI wait, a terminal session, and an
agent's attention span all end sooner.

Notifications are optional and best-effort: with no topic set the driver
stays silent, and an unreachable ntfy server never affects the campaign. Pick a
different topic per user so notifications don't cross over; each message carries
its campaign id so concurrent campaigns sharing a topic stay distinguishable.

For an **in-cluster** service the ntfy config is read from your ``.env`` at
``setup`` time and injected into the service pod (as a Kubernetes Secret, exactly
like the share credentials), so changing the topic means re-running ``setup
--force`` to redeploy. A local ``vast serve`` reads the ``.env`` live.


Experiment image builds (registry)
-----------------------------------

Agent-built experiment images (a project's :ref:`build section
<config-containers>`) are built **in-cluster** by a BuildKit Job and pushed to a
container registry the cluster can pull from. **RoboVAST runs that registry itself**, as
a container in the ``robovast`` pod ``vast cluster setup`` creates, so there is nothing to
configure and no site prerequisite: a deployment can always build. See
:ref:`the-robovast-pod` for why it lives there rather than beside the service.

The registry is published on ``/v2`` of the host the service already answers on, and the
image prefix *is* that host — ``robovast.example.org/<tag>:<hash>``. That is not a
cosmetic choice. An image ref is one string resolved by two different things: BuildKit
pushes to it from inside a pod (pod network, CoreDNS) and the kubelet pulls it on the
node (node network, node resolver, node TLS trust). Nothing in a pod spec reaches the
second one — see :ref:`cluster-registry-dns` — so a ``…svc`` name works for the push and
fails for the pull, and a plain-HTTP registry needs ``registries.yaml`` plus a runtime
restart on **every** node. The service's own published hostname already has real DNS and
a real certificate, so both halves work with no node configuration at all.

The same URL works from a workstation: ``docker pull robovast.example.org/<tag>:<hash>``
needs no login and no CA import, which is how you reproduce a campaign's exact image
locally.

.. warning::

   The registry is **unauthenticated**. It shares a hostname with the UI, which *is*
   token-gated, so it is the more reachable half of that host: anyone who can reach the
   service can push an image that campaigns then run. That is acceptable while RoboVAST
   is on a private network and is the first thing to revisit before exposing it to the
   internet. Adding auth is an htpasswd Secret plus an Ingress annotation.

**A service without a registry prefix cannot build.** The prefix is the service's own
published host — with no Ingress there is no address a node could pull a built image back
from — so a project whose container adds packages fails at submit naming that reason.

Two different states produce it, with two different fixes, and the service cannot tell them
apart: it reads the prefix out of its environment and has no RBAC to look at its own Ingress.

* **Published, but the prefix is unset.** ``vast service upgrade`` re-bakes the prefix
  from the live Ingress. A deployment whose prefix was dropped by an older ``setup`` re-run
  without ``--ingress-host`` — which left the Ingress alone, so nothing looked wrong until a
  campaign was submitted — needs exactly that upgrade.
* **Never published.** Re-run ``setup`` with ``--ingress-host``.

``vast doctor -n <namespace>`` says which of the two you are in.

**The same host is also written in as the service's declared origin**, from the same
``--ingress-host``. It is what the service reports as ``web_base``, so a client that cannot
be handed bytes -- a 20k-line log, a truncated query, a rosbag -- can be handed a link to
fetch them out of band instead. The two are separate values on purpose, even though today
they are the same host: a registry prefix may point somewhere else entirely, and the origin
must not silently follow it.

**An ``upgrade`` re-bakes it too, and needs no arguments to do it.** The origin carries a
scheme as well as a host, and an upgrade holds none of the TLS arguments the Ingress was
created with -- so it reads the whole URL back from the live Ingress, where the TLS block
decides the scheme, and states that. Which means a single ``vast service upgrade`` is
enough to publish the origin of a deployment that predates it, and enough to *clear* it
again for one that has been unpublished: reading the Ingress is what tells those two apart
from "nobody told me".

Nothing else overwrites it. A ``setup`` re-run without ``--ingress-host`` recovers only the
host, so it says nothing and the merge patch leaves the value alone -- as does an origin an
operator set by hand.

A service that was never published declares no origin at all, which is honest rather than
degraded: routes and file addresses still work, and only the absolute URLs are absent.

Storage defaults to a ``hostPath`` on the node the pod is pinned to, because a stock RKE2
cluster ships no StorageClass and a PVC there stays ``Pending`` forever. ``emptyDir`` is
deliberately not offered: every ``upgrade`` restarts the pod, which would discard every
built image on each version bump while already-submitted Jobs went to
``ImagePullBackOff``.

Only two things remain configurable, both about *other people's* registries:

.. code-block:: bash

   # Credentials for a private registry a .vast names in its `image:` field. Purely a
   # PULL credential -- the build registry is in-pod and open, so nothing needs a push
   # credential. Applied to campaign pods, aux/exec pods and the service's own image.
   ROBOVAST_REGISTRY_SERVER=harbor.example.org
   ROBOVAST_REGISTRY_USERNAME=<user>
   ROBOVAST_REGISTRY_PASSWORD=<token>
   # Optional: default base image when build.base_image is omitted.
   ROBOVAST_BASE_EXPERIMENT_IMAGE=ghcr.io/cps-test-lab/sim-suite-nav2-eval:latest

Removing one of those variables and re-running ``upgrade`` **deletes** the Secret it
created, rather than leaving a revoked credential deployed and still attached as an
``imagePullSecret``.

The pull Secret that ``setup`` creates from those values is **found by the service
itself** — it looks for the fixed name it would have created (``robovast-registry-push``)
and uses it when present, so nothing has to tell it it exists. This matters for a
**local** ``vast serve``: ``setup`` stores its env in the *service pod*, which an
off-cluster service never reads, so without the lookup it would conclude there were no
credentials at all. Set ``ROBOVAST_REGISTRY_PULL_SECRET`` only to point at a
**differently named** object; it is an override, not a requirement.

.. _cluster-registry-dns:

When the cluster cannot resolve the registry
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A registry whose name lives only in ``/etc/hosts`` on your workstation is not resolvable
from inside the cluster, and the push fails **after the whole image has been built**:

.. code-block:: text

   failed to push harbor.example.org/robovast/my-tag:<hash>: ... dial tcp:
   lookup harbor.example.org on 10.43.0.10:53: no such host

Declare the mapping instead of editing CoreDNS — RoboVAST puts it in the pod specs it
creates (the build Job and campaign Jobs) as ``hostAliases``:

.. code-block:: bash

   ROBOVAST_EXTRA_HOST_ALIASES=harbor.example.org=10.0.0.9
   # several: comma-separated, names sharing an IP are grouped automatically
   ROBOVAST_EXTRA_HOST_ALIASES=harbor.example.org=10.0.0.9,minio.example.org=10.0.0.10

.. warning::

   This fixes what a **pod** resolves — the push, and anything the scenario itself looks
   up. It does **not** fix the **image pull**: that is performed by the container runtime
   on the node, which reads neither pod specs nor CoreDNS. An unresolvable registry
   therefore also needs the name in each node's own resolver (``/etc/hosts`` or
   ``registries.yaml``) — the same node-level scope as registry TLS trust.

   A real DNS record is the one change that covers both, and is preferable wherever you
   can add one. Note also that naming a registry by **IP** is not a substitute when it
   sits behind an SNI-based proxy: a client dialing a bare IP sends no TLS SNI, so such a
   proxy serves no certificate at all and the handshake fails before any CA could apply.

   RoboVAST's own registry avoids this entirely by being published on the service's
   Ingress host, which by construction is a name both the pod and the node resolve.

.. _cluster-build-context-staging:

Where the build context is staged
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The build needs somewhere in object storage to put its context (the project directory
plus the generated ``Dockerfile``) for the BuildKit Job's init container to mirror.
**Nothing to configure on a normal cluster** — the storage the deployment already uses
is reused:

* **Embedded MinIO** (the ``rke2`` / ``minikube`` configs, where each campaign gets its
  own bucket): builds share one dedicated bucket, ``robovast-image-builds``, created on
  first use like any campaign bucket. A build belongs to no campaign, so it cannot use a
  per-campaign bucket; one stable bucket also keeps the content-addressed cache usable
  across service restarts. Objects live under ``image-builds/<build-id>/``.
* **A shared bucket** (external S3, or GCS): that bucket is used, with the same
  ``image-builds/<build-id>/`` prefix — nothing extra is created.

On GCS a bucket name is global to all of Google Cloud and the client does not create
buckets, so builds there **require** the deployment's bucket to be configured
(``-o gcs_bucket=…`` / ``ROBOVAST_GCS_BUCKET``); a missing one is reported rather than
guessed at. In-cluster builds do **not** require external-S3 mode, and enabling them
never changes how campaign results are stored.

.. _the-robovast-pod:

Where the registry and the index run
-------------------------------------

``vast cluster setup`` creates one ``robovast`` pod per deployment. Besides the object
store (MinIO, or nothing at all where campaign data goes to an external bucket) it carries
two more containers:

``registry``
   the container registry experiment images are built into, published on ``/v2`` of the
   service's own Ingress host.

``index``
   the Postgres holding every campaign's rows — one index, so a query across a search arm
   is one ``WHERE`` clause rather than a per-campaign database. The service reaches it at
   ``robovast.<namespace>.svc:5432``; the DSN is assembled from the Service name and the
   namespace and handed to the service as ``ROBOVAST_INDEX_DSN``.

Both used to be extra containers in the ``robovast-service`` pod. **They are not
service-lifetime state.** ``robovast-service`` is a Deployment, and every ``vast service
upgrade`` rolls it — so a version bump of the controller image restarted the registry and
the database, and both volumes followed the Deployment rather than the cluster. In the
store pod they are created once at setup, are pinned to the data node with the campaign
store, and are pinned to the data node with the campaign store. Losing them is
deliberate and cheap: images are rebuilt on demand, and the index is re-ingested from the
campaign data.

**The index's volume matches the store's, and its path is derived from it.** The index is
derived data — every row in it was ingested from a campaign in the object store — so it must
never outlive its sources. Sharing this pod with the store, taking its backing and sitting at
``<store>/../index``, is what makes that structural rather than something cleanup has to
remember: the two are placed, moved and removed as one thing, and no sequence of restarts,
evictions or operator mistakes separates them. An index that outlived its sources would answer
questions about campaigns nobody can reproduce or check, and answer them confidently.

Losing the index costs a re-ingest from the campaigns beside it — hours for a large corpus,
and nothing that cannot be rebuilt. That is why it is one replica, with no standby and no
backup.

The object store is where a finished campaign lives: its whole directory is published there,
and downloads, re-postprocessing and the index all read from it. It is **not** a backup. A
node's disk is one disk, so anything that must survive the machine belongs in an archive
(``vast share``), not in the store.

All four ports (``s3``, ``console``, ``registry``, ``index``) are on the pod's single
ClusterIP Service. It already selects exactly this pod, so extra Service objects would
duplicate the selector and add names that must agree with the DSN and the Ingress rule,
for no isolation — a ClusterIP is not a security boundary.

**No image ref changed with the registry's move.** The prefix is still the service's
published host, because an image ref is resolved twice — by BuildKit in a pod and by the
kubelet on a node — and only a real published name works for both. What moved is the
Ingress' ``/v2`` backend, from the service's Service to this one. ``vast service upgrade``
repoints it on a deployment published before the move.

.. warning::

   **An existing cluster does not gain these containers by re-running setup.** The store
   pod is deliberately kept as it is when it already exists (recreating it would discard
   the campaign store), so a cluster set up before the move keeps a pod without them.
   ``vast cluster setup`` and ``vast service upgrade`` both refuse in that state and say
   so, rather than deploying a service whose ``/v2`` route and index DSN point at
   containers that are not there. The remedy is ``vast cluster cleanup`` followed by
   ``vast cluster setup``.

.. _campaign-index-storage:

The campaign index marker
^^^^^^^^^^^^^^^^^^^^^^^^^

One more thing lives in object storage, resolved exactly the same way and for the same
reason: the **campaign index**, one zero-byte marker per campaign under

.. code-block:: text

   campaign-index/<campaign_id>/<created_at>

in the deployment's shared bucket, or in a dedicated ``robovast-campaign-index`` bucket
where campaigns get their own (an index belongs to no campaign either). It is what lets the
service list the campaigns the object store holds: a campaign's home is the store, but the
service pod's disk is scratch, so after a restart a scan of that disk finds nothing. There
is no bucket listing to fall back on — and a per-campaign bucket name is the campaign id
lowercased with underscores replaced, which cannot be reversed. Unlike a staged build
context this is **not** scratch: it is retired only when the campaign's data is deleted
(``vast campaign delete``, or the bucket cleanup below). See :ref:`campaign-discovery`.

What is *not* copied
^^^^^^^^^^^^^^^^^^^^

The context skips ``BUILD_CONTEXT_IGNORE`` (``robovast.common.build_context``) —
``.git``, ``results/``, ``__pycache__``, ``build/``, ``.venv`` and the rest — by exact
name, on both lanes: the cluster prunes while it stages, the local lane writes the
equivalent ``.dockerignore``.

It also skips **campaign output directories**, and those are recognised by *structure*
rather than by name: a directory holding an ``_execution/`` child. There is no name to
list, because a campaign directory is named after its campaign id. ``results/`` was
already excluded, but ``workspace run --wait-and-download`` extracts a campaign
*beside* the sources rather than into ``results/`` — so a project that had used it a few
times was staging several hundred megabytes of rosbags around a fifteen-megabyte tree,
uploaded and mirrored back down once per container on every build, with nothing reporting
it. Structure means the rule cannot go stale on a project nobody had in mind when it was
written.

This changes what is *copied*, never what is *hashed*: a campaign output is not a
``python_packages`` entry, so ``build_hash`` never saw it and the context hash is
unchanged either way.

A staged context is **scratch, and is cleaned up** — it is a full copy of the project
directory, so a build per experiment would otherwise pile up copies in the bucket
indefinitely. Nothing reads it after the build: a rebuild re-stages, the layer cache
lives in the registry, and a failure is diagnosed from the build log. So:

* the context is deleted as soon as the build is **seen to be terminal** (either
  outcome) — that is any ``vast image build`` without ``--no-wait``, and every campaign
  submit, since both poll the status to completion;
* on the next build, any context whose **Job no longer exists** is swept. Build Jobs
  self-destruct at ``ttlSecondsAfterFinished`` (1 h), so an absent Job means the build
  ended at least that long ago — or died with a previous service instance. This is the
  backstop for a ``--no-wait`` submit nobody polled, and it runs on a cache hit too.

Builds the service still has in flight are held back from the sweep explicitly: their
context is staged *before* their Job is created, so "no Job" on its own does not mean
stale. The Job itself needs no cleanup of ours, and neither does the ``:buildcache``
tag — one tag per build tag, overwritten in place.

Caching in-cluster
^^^^^^^^^^^^^^^^^^

Builds are solved by **one long-lived BuildKit daemon** (``robovast-buildkitd``), not by a
BuildKit spawned inside each build pod. The build Job still exists and still stages its own
context -- it is now a *client* that dials the daemon, which works because ``buildctl --local``
resolves its paths on the client and streams them over the session.

That daemon keeps its store on a volume, and the store is the point. When every build got a
fresh BuildKit, two costs were paid every single time and neither looked like anything but a
slow build: the base image was pulled again (measured at 95-110 s per container, on builds
where every layer was a cache hit), and ``RUN --mount=type=cache`` was discarded, so a pip
layer that missed re-downloaded its wheels in full. No registry-side cache can help with
either -- a cold builder has to materialise the base whatever the registry holds.

Two registry-backed mechanisms remain, and now sit *above* the daemon's own store:

* **Is it already built?** The service asks the registry whether
  ``<prefix>/<tag>:<hash>`` already has a manifest, and skips the build if so. This
  is deliberately not derived from the build Job's status: that Job is deleted after
  ``ttlSecondsAfterFinished`` (1 h) and the in-process record is lost on a service
  restart, after which a bit-identical image would otherwise be rebuilt and re-pushed. The
  probe **fails closed** — if the registry cannot be reached or authenticated, the
  image counts as absent and is rebuilt, because a wrong cache hit would leave the
  campaign pods in ``ImagePullBackOff``.
* **Layer reuse across hashes.** The build imports from and exports to
  ``<prefix>/<tag>-<scope>:buildcache`` (``mode=max``, so intermediate layers are kept
  too). This tag is *not* hash-qualified — that is the point: the build for a new hash
  reuses the layers of the previous one, so changing one late ``python_packages``
  group does not rebuild the ones before it. A failing cache **export** never fails
  the build (the image is already pushed by then); a failing **import** just makes the
  build slower. Both refs inherit the deployment's ``INSECURE`` / CA settings, since
  they address the same registry as the push.

.. _build-cache-scope:

The ``<scope>`` segment is what keeps that reuse from reaching too far. ``<tag>`` is the
*container's name*, so it is ``sut``, ``simulation`` or ``scenario`` for very nearly every
project there is. Keyed on the name alone, every project in a deployment exported
``mode=max`` to the same three tags, overwritten in place — so a campaign whose large,
stable install group had been built an hour ago found it gone, because an unrelated
campaign with a container of the same name had built in between. The group an author had
deliberately ordered *first* to protect was the one that kept being rebuilt.

``cache_scope`` (``robovast.service.image_build``) therefore hashes what decides *which
layers exist* — the resolved base image, the apt set, and the install groups in order —
with the per-iteration churn removed: a local wheel counts as its **distribution name**,
not its filename, so bumping ``pkg-0.1.24`` to ``pkg-0.1.25`` keeps the namespace it is
meant to reuse. A resolved VCS commit is deliberately *not* an input: a moved branch
changes a layer's content, not the chain's shape, and retiring the namespace for it would
throw away the layers *below* the one that actually changed.

The registry therefore needs room for one extra tag per built container **per chain** —
a project retires its own old scope whenever it changes its base image or its package
set. Grouping the ``python_packages`` list by change frequency is what makes the layer
cache pay off — see :ref:`containers <config-containers>` — and the scope is what makes
that grouping survive other projects building alongside it.

The build daemon
^^^^^^^^^^^^^^^^

``robovast-buildkitd`` is applied by ``setup`` and converged by ``upgrade``, one replica with
``Recreate``. Its storage is chosen the same way the registry's is:

.. code-block:: bash

   vast cluster setup rke2 --buildkit-path /data/robovast-buildkit
   vast cluster setup gcp  --buildkit-class premium-rwo --buildkit-size 200Gi

A **hostPath** by default, because a stock RKE2 cluster ships no StorageClass and a PVC there
stays ``Pending`` forever. That makes the cache node-local, so ``--buildkit-node`` pins the
daemon; keep it off the registry's node, since those are the deployment's two large on-disk
tenants and the service pod is pinned to that one. On a cluster that can provision volumes,
prefer an **SSD** class: BuildKit's snapshotter is small-file heavy, and a slow disk becomes
the bottleneck the cache was meant to remove.

The store is **bounded** — a component whose whole purpose is that state survives is also the
one that fills a disk. The daemon's ``buildkitd.toml`` sets ``reservedSpace`` / ``maxUsedSpace``
/ ``minFreeSpace`` (the keys the pinned BuildKit understands; the older ``gckeepstorage`` is
gone, which is half the reason ``BUILDKIT_IMAGE`` is pinned at all), and each has a flag:

.. code-block:: bash

   vast cluster setup rke2 \
     --buildkit-cache-max 150GB \        # ceiling on the cache
     --buildkit-cache-min-free 50GB \    # free space kept on the filesystem
     --buildkit-cache-reserved 100GB      # cache kept even when old

**Size these for the disk the store lands on.** The defaults suit a large one. The load-bearing
one is ``--buildkit-cache-min-free``: it is measured against the *filesystem* rather than the
cache, so it forces pruning long before an oversized ceiling is reached and is what keeps a
fixed ceiling honest on a disk smaller than the ceiling. Without it a fixed ceiling is not a
ceiling at all — the store grows until the node runs out, and the kubelet answers DiskPressure
by evicting pods, on the node the daemon is pinned to and the service pod may share. On a disk
whose size you do not know, a percentage (``70%``) is accepted for any of the three.

All three are recovered from the running daemon by ``upgrade``, like the storage settings: they
are set by a flag and recorded nowhere else, so re-rendering from defaults would silently
re-size a store somebody had bounded on purpose.

Three consequences worth knowing before they surprise you:

* **An upgrade kills builds in flight.** ``Recreate`` replaces the daemon pod, and a campaign
  waiting on a build *detaches* rather than cancels — so a sibling campaign can be waiting on
  a build a restart destroys. Nothing drains.
* **The endpoint is unauthenticated.** BuildKit offers mTLS and nothing else, and this ships
  with neither mTLS nor a NetworkPolicy. It is a ClusterIP with no Ingress, but that is not a
  boundary: campaign pods run images a ``.vast`` chose and can reach it. The pip download cache
  is shared across every build and now *persists*, so anything that can dial the daemon can
  leave something in it for the next build to install. The registry's "deliberately
  unauthenticated" note does not transfer — that one is excused by sharing a token-gated
  hostname, and this has no hostname at all.
* **A wedged cache has no remote remedy.** Changing a cache scope fixes a bad *registry* cache;
  a bad local store needs the daemon restarted or its volume cleared, on the node that holds
  it.

If the daemon is not ready, a campaign is **refused at submit**, naming it — before staging a
context or creating a Job. A daemon that dies mid-build is classified as an infrastructure
failure rather than a problem with the project's packages, and the daemon's own recent log is
appended when the client produced none.

.. note::

   Rootless BuildKit needs AppArmor **and** seccomp ``Unconfined``, for rootlesskit's mount
   namespace. That is now set on the **daemon**; the build Job creates no such namespace and
   carries no exemption. On nodes whose container runtime cannot pull from the registry
   without host trust (e.g. an in-cluster registry over plain HTTP on RKE2/k3s
   containerd), the node must be configured to trust it (``registries.yaml``) for
   campaign pods to pull the built image — an external registry with valid TLS
   avoids this.


.. _cluster-admission:

Job Queueing
------------

.. note::

   **If your cluster has Kueue installed, remove it by hand, once.** Nothing here
   installs or removes it, so a cluster that has it keeps a controller pod and its CRDs
   indefinitely — inert, since no job carries a queue label, but running.

   .. code-block:: bash

      helm uninstall kueue -n kueue-system
      kubectl delete crd -l app.kubernetes.io/name=kueue

   Delete RoboVAST's own ``ClusterQueue`` and ``ResourceFlavor`` first if the CRD delete
   hangs — an orphaned custom resource is what holds its CRD open. Check for a leftover
   ``kueue`` mutating/validating webhook afterwards: that is the one leftover that would
   actually break Job creation, rather than merely idling.

   A cluster set up fresh needs none of this.

Cluster jobs are queued by RoboVAST itself. A job is **created only once the cluster
has room for it** — CPU and memory, and GPUs on a cluster that has them — so a large
campaign cannot oversubscribe the nodes, and several campaigns launched at once share
the cluster instead of fighting over it. There is nothing to configure and nothing to
install: the queue lives in the RoboVAST service and is sized from what the nodes
advertise, each cycle rather than once at setup.

Creating jobs as capacity appears, rather than creating them all and letting them wait,
also means a campaign never asks one kubelet for a thousand image pulls at once.

A request no node could **ever** satisfy — more CPU than the largest machine has, or a
GPU on a cluster with none — is refused at launch, with the request and each node's
allocatable named. That is a different thing from a cluster that is merely full, which
is not an error and is simply waited out.

So is a pod that declares **no** CPU at all: a request of nothing fits every node, so the
queue would create the whole plan in one pass and gate nothing. A campaign whose containers
declare no ``resources.cpu`` is refused at launch, naming them. If only *some* declare it
the campaign runs, but the queue paces on less than the pod really takes — that is a
warning naming the silent containers, not an error.

**Older campaigns finish first.** When several campaigns run at once, the one that
started earliest is admitted first: each slot that frees up goes to the oldest campaign
that still has work queued, so a campaign is not overtaken by one launched after it.
There is nothing to configure and no way to get it wrong — the order comes from the
campaign id, which carries its start time.

Two properties are worth stating, because they are what make this safe to leave on:

* **It never stops work that is already running.** Priority orders the *queue* only. A
  younger campaign's runs finish undisturbed; nothing is preempted, and no partial run
  data is produced. The older campaign takes the capacity as it is released, not by
  taking it away.
* **The cluster still stays full.** A high-priority job that does not fit does not block
  smaller ones behind it, so a younger campaign keeps using capacity the older one cannot.
  Utilization is unchanged; only the order in which queued jobs are admitted changes.

The one thing this does not do is reserve capacity. A search campaign postprocesses
between batches, and while it is composing its next batch it has nothing queued — so a
younger campaign legitimately fills the cluster in that window, and the older one
reclaims the slots as they free. That is the intended trade: the alternative is idling
the cluster to hold capacity for a campaign that is not yet asking for it.

**Jobs waiting is normal.** A campaign whose jobs sit in the queue is healthy: it
is waiting for capacity, not stuck. ``vast cluster monitor``, the web UI
and ``list_campaign_jobs`` report such jobs as ``waiting`` — a status of its own,
distinct from ``pending`` (a pod exists and is being scheduled) and from ``blocked``
(the job cannot start and needs a human). The reason capacity was refused rides along
as the job's ``detail``.

A campaign that is *entirely* queued is also not reported as stalled. The no-progress
deadline measures how long a **run** has gone without completing, and while nothing of
the campaign is running it is timing a queue rather than a run — so the verdict is
withheld and ``stall_verdict`` says why, instead of accusing a healthy campaign that is
simply behind an older one.

A ``waiting`` job has not been created on the cluster yet, so it appears in the web UI
only as the ``waiting N`` counter, never as a row: the per-job list mirrors the jobs that
actually exist there (what ``k9s`` shows), and those are the ones with a pod and a log to
read. ``list_campaign_jobs`` still returns every one of them with its reason.

The listing spans the two jobgroups a campaign creates for its own work — its trials
(``scenario-runs``) and its postprocessing conversion (``postprocessing``) — in one
set-based selector, because it is polled while the campaign is live and a second listing
would double both the Job read and the pod read behind it. Each row carries the
``JobKind`` that says which it is, and only ``run`` rows reach ``JobCounts``: the counts are
read as facts about runs, and a conversion or a probe among them would enter the run meter
and the ETA's divisor. The conversion is the one job a campaign in its ``postprocessing``
phase has, so listing it is the difference between a busy campaign and an apparently idle
one. Its row carries no log of its own, and that is deliberate: the conversion runs in init
containers, which the per-job log does not report (it reports the containers that run for the
pod's whole life), and every container's output is already published to the campaign's
POSTPROCESSING section while the Job runs (:func:`publish_live_log`) — a copy in the object
store, which is the one that is still readable after ``ttlSecondsAfterFinished`` has taken the
pod away.

An unreachable cluster (VPN down, cluster stopped, a kubeconfig context pointing at
an endpoint that no longer answers) is reported the same way: one line naming the API
server and the transport error, within about a minute — every API call has a 10-second
connect timeout (``ROBOVAST_KUBE_CONNECT_TIMEOUT``), so a connect that cannot succeed
is not left to the operating system's multi-minute TCP timeout. Read timeouts stay
unlimited; a slow answer is still an answer.

When it happens *after* the runs — the postprocessing step that follows a finished
campaign — the campaign stays ``finished`` with the reason on
``postprocessing_error``; the results are already published, and
``run_postprocessing`` re-runs the step once the cluster is back.

.. note::

   Admission does not depend on anything being installed in the cluster. If the service
   cannot measure the cluster it refuses to submit rather than falling back to creating
   every job at once, which is the one way a campaign could still overload the nodes.

What the queue is not
^^^^^^^^^^^^^^^^^^^^^

This is a special-purpose queue for RoboVAST's own work, not a general cluster
scheduler, and two consequences are worth stating rather than discovering.

**The service runs as a single replica, and that is load-bearing.** The queue lives in
the service process's memory, so a second replica would be a second queue spending the
same free capacity against the same cluster. Nothing would report an error -- the symptom
would be over-admission and pods that cannot be placed. Scaling the Deployment past one
replica requires making the queue cluster-wide state first; the ``replicas: 1`` in the
service Deployment says so at the line.

**A service restart leaves the campaigns that were running alone.** The queue is the
part that does not survive it; the campaigns do.

* Jobs already created keep running. They carry no owner reference, so Kubernetes does
  not collect them, and startup reaping covers aux pods only -- they run to completion
  and write their results to the object store, which is where a campaign's results live
  anyway.
* Jobs still queued were never created, so there is nothing to orphan. They are re-queued
  when the campaign is adopted.
* The successor process does **not** over-admit against the surviving Jobs. Capacity is
  measured from the pods actually bound to nodes rather than bookkept, so their requests
  are visible to it exactly like any other tenant's -- which is also what lets it adopt
  them without any bookkeeping of its own.

This is what ``ClusterService._adopts_on_restart`` says, and why this lane has no
shutdown-time job teardown: stopping a campaign is ``vast campaign stop``, and exiting the
service is not. The distinction matters more than it looks, because a cooperative stop
persists a terminal ``outcome.json`` -- and a campaign that has recorded an ending is one
no successor will ever pick up again.

**And the successor picks them back up.** At startup, before it answers anything, the
service re-launches every campaign the store lists that recorded no ending
(``campaign_resume``). There is no resume mode anywhere in the launch path, the batch loop
or the controller; a resumed campaign is a re-launch under its own id, and four properties
-- each equally true of a campaign starting now -- are what make that safe:

* the campaign's records (``_execution/launch.yaml``, ``campaign.db``) are published when
  they are written rather than at ``finalize_campaign``, so an unfinished campaign has
  something to be re-launched from;
* the campaign root is restored from the store first (``fetch_campaign``), so the driver
  re-enters a directory holding what the earlier life produced;
* the batch runner plans against that root, adopting every job whose runs already have a
  verdict instead of running them a second time;
* ``create_campaign`` is idempotent by name, so the restored store re-opens its row.

**A search is picked up too.** Nothing about its strategy is serialized; the strategy is
re-driven through the exact ``ask``/``tell`` sequence its own ``unit`` rows recorded, which
reproduces the original search for a strategy that is a function of its seed and its
evaluations -- every strategy shipped here. That is why ``campaign.db`` is published at
each batch boundary: those rows *are* the checkpoint. Two conditions are checked before the
campaign is re-launched rather than discovered halfway through its second half:
``search.seed`` is set (an unseeded strategy re-seeds from entropy, so the replay would
rebuild a different search) and the strategy does not declare ``RESUMABLE = False``.

Campaigns are deliberately **left alone** when nothing says what to run (no launch record,
or no frozen ``_config/``), when the frozen config cannot be read unchanged (resuming would
mean migrating it mid-campaign, making the second half a different experiment from the
first), or when a search fails either condition above. Each says which it is in the service
log, keeps the ``crashed`` phase ``reconstruct_status_from_disk`` gives it, and its data
stays recoverable with ``vast campaign import``.

**A running postprocess is re-attached to separately** (``postprocess_reattach``, called
from startup right after the resume). Postprocessing is a Job that outlives the service
process too, but only the *waiting* process writes the campaign's postprocessing verdict --
so a restart mid-postprocess leaves a Job that converts every bag, finishes, and a campaign
still carrying the previous attempt's message: fully derived data marked as none. The resume
above cannot cover it, because a postprocess retriggered on a finished campaign runs against
a terminal ``outcome.json``, which ``owed_work`` excludes precisely so that a campaign that
recorded an ending is never restarted. What is owed here is a verdict, not work.

The live Jobs are found with one labelled listing (``jobgroup=postprocessing``) and the
label's campaign resolved against the store's index, then confirmed against the
campaign-level Job name -- a *discriminated* Job is a search's per-batch conversion and is
owed to its batch's driver, not to any campaign record. The waiter creates and replaces
nothing (the Job already mounts the scripts it was created with), publishes the live log
while it waits, and records nothing at all when the Job cannot be read: a campaign whose
conversion succeeded must never be marked failed because the API server was unreadable.


How free capacity is measured, and why it is not the whole cluster
------------------------------------------------------------------

Admission may only hand out capacity the scheduler can actually place against, so free
capacity is **allocatable minus the requests of every pod already bound to a node**, minus
a small headroom. Everything sharing the nodes counts: the CNI and ingress DaemonSets,
MinIO, the RoboVAST service, the build daemon, and the campaign pods themselves.

Counting at 100 % of allocatable instead admits one job more than the nodes can hold as
soon as anything else runs — the extra pod is created, the scheduler has nowhere to put
it, and it sits ``Unschedulable`` with ``Insufficient cpu``. That is the failure a single
campaign rarely reaches and several concurrent ones reach reliably, which is what makes it
look like a concurrency bug rather than an arithmetic one.

**It is measured every cycle, not snapshotted at setup.** This replaced a fixed quota
sized once when the cluster was set up, which had to be re-sized by hand after anything
long-lived was added to or removed from the nodes, and was silently wrong until someone
did. Nothing needs re-running now.

**Headroom** protects the shared tenants no campaign owns, and is set on the service
deployment: ``ROBOVAST_NODE_HEADROOM_CPU`` (default ``1``) and
``ROBOVAST_NODE_HEADROOM_MEMORY`` (default ``2Gi``). Deliberately not a ``.vast`` setting —
a per-campaign override would let one campaign shrink the margin every other one depends on.

**Capacity is counted per node, and each job is pinned to the node it was counted against.**
A pod runs on one machine, so a cluster-wide figure cannot see fragmentation: it says there
is room while no single node has enough, and the job is created and then cannot be placed.
It happens whenever the free cores are spread across nodes and no single node holds the
4.75 a pod needed.

The pin is the label ``robovast.io/node-id``, whose value is the same hash a run records as
``runs.node_label`` — so the selector that placed a run and the record of the machine it ran
on are provably the same node, and no hostname appears in any pod spec. ``setup`` applies
it, and an **unlabelled node still takes work**: it simply cannot be named, so its jobs are
created without a selector and the scheduler places them. A cluster upgraded without
re-running ``setup``, or a node that joined since, therefore behaves exactly as it did
before per-node placement existed rather than refusing to run anything.

A pinned pod whose node is momentarily full is **contention, not a fault**. It waits for
that node — the fifteen-minute window, not the sixty-second one — because it is waiting for
capacity that is coming back, and the alternative is destroying a run for being patient.

**RoboVAST's own infrastructure is not evenly spread.** The service pod, the registry and
(on the bare-metal providers) the results store are pinned to one node
(:ref:`cluster-node-local-storage`), so that machine has materially less left for campaign
work than an even split implies. Per-node budgets see this correctly, because they measure
what is committed on each node rather than dividing a cluster total.

**Reserve for the node itself, too.** Kubernetes hands out ``allocatable``, which is
``capacity`` minus what the kubelet was told to hold back for the OS, the kubelet and
the container runtime. Distributions differ: where nothing is reserved, ``allocatable``
equals ``capacity`` and a full cluster leaves the machine's own processes competing with
pods for the last core — ``setup`` warns when it sees a node like that, because nothing
it can do reaches the setting that fixes it. On RKE2/k3s, reserve it in
``/etc/rancher/rke2/config.yaml`` (k3s: ``/etc/rancher/k3s/config.yaml``) on each node:

.. code-block:: yaml

   kubelet-arg:
     - "system-reserved=cpu=1,memory=2Gi"
     - "kube-reserved=cpu=1,memory=2Gi"

Applying it restarts the kubelet, which restarts every pod on the node: do it in a
maintenance window, not while campaigns are running. The new ``allocatable`` is picked up
by the next measurement with nothing to re-run (``kubectl get node <name> -o
jsonpath='{.status.allocatable.cpu}'`` shows it took effect). This is independent of the
subtraction above and complements it: the kubelet reservation protects the machine, the
subtraction protects the schedule.


.. _cluster-node-calibration:

Per-node container sizing
^^^^^^^^^^^^^^^^^^^^^^^^^

**A campaign asks for it, per campaign, with** ``execution.sizing: calibrated`` (see
:ref:`declaring the sizing mode <config-sizing>`). Before it places work on a node, one *calibration probe* runs there;
what it measured becomes that node's figures, and every run of that campaign on that node is
sized from them. A campaign that says nothing keeps ``fixed`` and its declared sizing.

The cluster's part is the **bootstrap**: what a container asks for before anything has been
measured for it, which is what the probe itself runs at.

Set in your ``.env``, like the git token and the ntfy topic — ``setup`` carries it into the
Deployment, so it is a property of the cluster rather than of any campaign:

.. code-block:: bash

   ROBOVAST_BOOTSTRAP_CPU={"sut": 5, "simulation": 3, "scenario": 2}
   ROBOVAST_BOOTSTRAP_MEMORY={"simulation": "4Gi"}

A role left out keeps its default rather than disappearing, so raising one cannot silently
drop another. With neither set the defaults below apply, and the service log says which of
the two it is using.

**A campaign may override it per container**, by declaring ``resources`` on that container:
under ``execution.sizing: calibrated`` those figures are what the probe and every
not-yet-measured node run at, and the ceiling a measured figure may not exceed. The
deployment default still applies to every container that declares none, and per *field* — a
``.vast`` stating only ``cpu`` keeps this ``memory``. See
:ref:`the sizing mode <config-sizing>`.

**How the measurement is turned into an allocation** is settable the same way, in a
``calibration`` block on the container, defaulted per role from one more ``.env`` entry:

.. code-block:: bash

   ROBOVAST_CALIBRATION={"sut": {"size_on": 100, "limit": "request"},
                         "simulation": {"headroom": {"cpu": 1.25}}}

The same block a container may carry, keyed by role — so an option added to it later is
settable here with no new variable.

.. warning::

   **The three roles sum to a pod that has to fit your smallest node.** The probe is pinned
   to the node it measures, so a sum larger than that node's allocatable (less the cluster
   headroom) leaves it unmeasurable -- refused at launch, naming the figure and the node.

   **Do not set a figure below what the container actually wants.** The bootstrap is also
   what the probe runs at, so a container capped under its own demand throttles against that
   cap; the probe is then refused as having measured its ceiling rather than the demand, no
   node is calibrated, and every run of the campaign stays on the bootstrap — the outcome
   calibration exists to avoid, reached by tightening the one figure that must not be tight.
   The refusal says so per node in the campaign log, naming the container and its throttle
   ratio. Size these from a finished campaign's ``resource_usage`` peaks, with margin.

Per **role**, because the three want very different amounts and CPU and memory rank them
differently: the system under test wants cores and little memory, the simulator is the
opposite — it compiles a world once and then sustains very little CPU. One ranking for both
would starve whichever resource the other role dominates. A container whose name is not a
known role takes a small default; it is one probe away from a measured figure, while a
generous default for every unnamed container is what makes a probe unplaceable.

It is per **container**, so a three-container pod reserves the sum until its node reports —
which makes the bootstrap the floor on what calibration costs, and a reason not to set it
generously on a small or busy cluster.

Why per node at all: the same trial costs about **1.6x more CPU on the slowest machine of a
mixed cluster than on the fastest**, and wall time does not show it — a realtime-paced
simulator holds one simulated second per wall second, so every machine finishes at roughly the
same time and the difference lands entirely in CPU consumed. One declared number is therefore
wrong on every node but the one it was measured on.

It is a **validity** matter as much as a throughput one. At a uniform 3.0 cores for the system
under test, one node was quota-bound in 100 % of its runs at 2.5 and below while three others
were never quota-bound at any allocation down to 2.0. Equal *cores* are not equal *compute*,
so an equal declaration produces unequal conditions — the thing a uniform number was meant to
prevent.

How the figure is found:

* **One probe per node, and it is never a campaign run.** It writes to ``_calibration/<node-id>/``
  — a reserved directory nothing walks looking for runs — so it cannot enter the results in
  the first place. A campaign of 50 runs still delivers 50.
* **A probe is listed, marked, and counted apart.** It holds real capacity on a real node, so
  it carries the campaign's labels and appears in the job listing — as ``kind: calibration``,
  named for the node it measures, and outside every figure in ``JobCounts``, which a reader
  takes as facts about *runs*. It reports its own status like any other job, because a probe
  that failed is one worth looking at, but it cannot be stopped individually: there is no run
  to record as killed. In the web UI's campaign view it is the row with the ``calibration``
  chip beside its status.
* **A node with an outstanding probe takes no campaign work.** Otherwise the runs placed
  while it is measuring are the odd ones out on a node whose later runs are calibrated,
  reintroducing the inconsistency the probe exists to remove.
* **How a measurement becomes an allocation depends on the container's role**, and what
  decides that is whether anything would report that squeezing it cost something. The system
  under test is read at its maximum, as request *and* limit, so it never throttles: a run
  clipped mid-plan fails in a way that looks like the stack's fault rather than the
  allocation's. The simulator is read at the 95th percentile and keeps its ceiling — its
  peak-to-mean ratio is about 18, so reserving the maximum would cost more than not
  calibrating, and the realtime factor reports if the squeeze cost anything. The scenario
  runner is read the same way, but nothing grades how well *it* ran, so its ceiling is what
  must not be tight; on a probe its own tick rate fills that gap (see below).

  Every one of those is a default, and a container may state its own under
  :ref:`the sizing mode <config-sizing>`. Memory is different in kind and takes one
  rule for every role: the
  measured **maximum**, with headroom. Exceeding a CPU reservation slows a container;
  exceeding a memory one kills it, so no role is sized on a figure most of its samples sat
  below.
* **Memory is measured wherever the node can report it.** Both cgroup layouts are read into
  the same columns, so a mixed cluster stays comparable. Where a node reports neither — an
  older runtime, a kernel without the counter — the container keeps its declared or bootstrap
  memory rather than being sized from nothing, and the same absence disables the OOM check
  for that node rather than passing it.
* **The scenario runner reports on itself, on probes only.** A probe runs with
  ``--tick-log``, so ``tick_timing.csv`` records how closely the behaviour tree held its
  configured period. A probe whose scenario could not keep up measured a starved container,
  and is refused rather than believed. Campaign runs do not carry the flag: it is per-tick
  instrumentation on the trial's hot path, and only the probe's file is ever read.
* **A calibrated figure never exceeds what the ``.vast`` declared.** Calibration sizes a
  node's jobs down to what they need; it does not raise a ceiling the author set.
* **Frozen once set, and dropped when the campaign ends.** Continuing to adapt would mean run
  5 and run 40 on the same node ran in different environments. The figures are deliberately
  not reused by the next campaign — they were measured under this one's contention, for this
  one's containers.
* **Pilots calibrate nothing.** With no more jobs than the cluster has nodes, no node runs a
  second one, so the probe would cost as much as the work it was meant to improve. The
  mechanism is skipped and the campaign behaves as it did before any of this existed.

**Whether it is worth turning on is a question about your cluster, not about RoboVAST.**
The gain is the spread between your fastest and slowest node; on a homogeneous cluster there
is nothing to recover, and on an unlike one it is bounded by how much of a pod is the system
under test, whose ceiling calibration does not lower. A matched pair of campaigns — one with
``execution.sizing: calibrated``, one with ``fixed``, everything else equal — measures it
directly, and ``run_health`` says whether the tighter ceilings cost the stack anything --
provided the campaigns declared a check under ``results_processing.health_checks``. Nothing
runs undeclared, so an empty ``run_health`` for the pair means *not graded*, never *not
degraded*; that is the same rule as everywhere else in the table. Both campaigns' grades sit
in the one index, so compare them in one query -- naming both ids in the query call's
``campaigns`` argument -- rather than expecting a per-campaign database. A query that names
only one campaign sees only that one, whatever its ``WHERE`` clause says.

**Check the realtime factor when you do.** ``clock_map_sim_span_s / clock_map_wall_span_s``
is what says whether a tighter allocation cost the simulator its pacing, and a campaign whose
factor drops is no longer comparable with one sized any other way — worse results rather than
merely slower ones.

**A peak measured on an idle probe is an unvalidated basis for a hard limit on a loaded
machine.** That is why this is switchable, and why the probe is one run rather than a
guarantee. A workload with heavier planning spikes than the one a cluster was measured on has
not been tested against its own calibration.

**The evidence behind the design**, kept here because it is what rules out the cheaper
alternatives. Two campaigns of one configuration times twenty runs, so the machine was the only
variable; forty trials, all passed. Per-container CPU comes from ``resource_usage``, summed per
tick before averaging (a row is one process name, not a container), then divided by the run's
realtime factor -- ``cpu_percent`` is per *wall* second, so a node that meets fewer step
deadlines otherwise reads as cheaper than it is:

Measured on one four-node cluster, the same pod cost **1.6x more CPU per simulated second on
the slowest node than on the fastest** — and the ordering did not follow clock speed. Your own
figures come from ``get_campaign_summary``; the point is that the spread exists and is not
guessable from the hardware.

The ranking tracks **microarchitecture rather than clock**: the two Skylake-derived parts sit
together at ~2.2 despite a 1.5x clock difference, Zen 3 at 1.71, Raptor Lake at 1.37.

**A cached per-node factor is refuted, and that is why this is measured per campaign.** Every
design that stores a number and reuses it -- a scalar per node, a ``robovast.io/cpu-factor``
label written at setup, a reference campaign run once -- assumes a figure that transfers between
campaigns. Measured against two unlike campaigns on the same cluster, it does not. Container
rankings **invert between nodes**: one machine was the cheapest for the system under test and
among the dearest for the simulator, while another was the reverse, so no single per-node scalar
can be both greater and less than one at once -- the shape is wrong, not the calibration. Even
per ``(node, container)`` it does not transfer: between two campaigns one node's simulation cost
moved +42 % while another's moved +1 %, flipping their order.

**Why a hard limit is sized on the peak and not on p95.** Sizing a limit at ``p95 x 1.25`` looks
safe and is not: a container clipped at its limit does not lose the clipped work, it queues it,
so it stays pegged working the backlog off and the next spike arrives into a full budget. A
configuration whose static clip rate was **0.5 %** produced **44 % saturation and lost 22 % of
the runs**. That is why the system under test takes the peak as request *and* limit, while
everything else splits the two.

**What remains true, and is why a campaign chooses.** A peak measured on an idle probe is
still an unvalidated basis for a hard limit on a loaded machine; a workload with heavier
planning spikes than the cluster was measured on has not been tested against its own
calibration. ``execution.sizing: fixed`` — the default — honours the declared sizing exactly,
which is what a campaign wants when the allocation is itself the variable under study.

**A probe that measured its own ceiling is refused**, and the node stays on its current
sizing rather than being sized from a limit: throttled past what its own statistic can absorb,
or OOM-killed at all — a memory ceiling that binds kills rather than slows, so one is enough.
Both counters come from the same file the sizing is read from.

**A probe that loses a workload container stops the campaign, naming that container.** Those
containers are native sidecars, so one that dies is *restarted* rather than ending the job:
the probe goes on sampling a stack that keeps dying, holds its node while it does, and what
is read out of it at the end measures the restart loop rather than a trial — surfacing as
whichever statistic that fragment fails, with the container's own error nowhere in it. A
probe runs one of the campaign's own configurations, so this is not a flaky trial to
re-sample: every run would meet the same fault. The campaign therefore ends on the crash
itself, and what the container printed before it died is captured in
``_execution/container_failures.json``, beside the probe's own output under
``_calibration/<node-id>/``.

How much throttling a container may survive depends on **which statistic its figure comes
from**, because clipping removes the top of the distribution. A container sized on its peak is
spoiled by the first clipped tick, so it keeps a strict allowance covering bring-up only. One
sized on its sustained figure — a percentile that already discards a tail — is unaffected
while the clipped ticks stay inside that tail, and is judged against exactly what the
percentile throws away. A single strict allowance for both refuses probes whose sustained
figure is perfectly good and leaves those nodes unmeasured, which costs more than the
distortion it was guarding against.

**Where calibration does not apply, the bootstrap stands and is checked.** A campaign with no
more jobs than the cluster has nodes, or a cluster that can grow, never probes. Those runs use
the bootstrap, and one that is OOM-killed or throttled hard against it **stops the campaign**
with an error naming the container. Unlike a declared or measured allocation — where such a
run is recorded and kept — nobody chose the bootstrap for that workload, so a run that dies
against it reports that the default does not fit rather than anything about the stack.

.. note::

   **Not applied on a cluster that can grow.** There a job that fits no current node is
   created unpinned and the scheduler places it — possibly on a node that is already
   calibrated, at the declared size, which is the mixed sizing this exists to prevent and is
   invisible after the fact. Declared sizing everywhere is the honest behaviour there.

   The same caveat applies on any cluster whose nodes are replaced while campaigns run
   (managed node pools autoscaling, auto-upgrading or reclaiming spot capacity): a node that
   joined since the last ``setup`` carries no ``robovast.io/node-id``, so it cannot be probed
   or pinned and its jobs run at the declared sizing beside calibrated ones. See
   :ref:`cluster-cloud-limits`.

Which image bytes a run uses
----------------------------

Before the first batch creates a Job, every image ref the campaign's pods will run is
resolved against the registry to the digest it names right now (one ``HEAD`` per distinct
ref, cached for the campaign), and each container carries an explicit
``imagePullPolicy``: ``IfNotPresent`` for a digest ref, ``Always`` for anything still a
tag.

Both halves matter, and the second is the one that bites. Kubernetes defaults the policy
to ``IfNotPresent`` — *except* for a ``:latest`` tag, where it silently becomes
``Always``. A campaign image is a floating tag in the ordinary case, so every container of
every scenario pod re-contacted the registry on every start for an image the node already
had. A batch of 35 pods is then ~140 registry round trips in one instant, against a
kubelet whose image-pull limiter is five per second (``registryPullQPS``, burst ten): the
pods past the burst come back ``ErrImagePull: pull QPS exceeded``, and that is arithmetic
rather than bad luck. Pinning removes the round trips; writing the policy out removes the
dependence on how the tag happens to read.

It is also what makes a campaign one experiment: a floating tag can be re-pushed between
batch 1 and batch 50, and a system under test that changes underneath a sweep invalidates
the comparison the sweep exists to make. ``_execution/execution.yaml`` then records what
ran rather than what was asked for.

Resolution is **fail-soft**. An unreachable registry, a ref this deployment holds no
credential for, or a registry that omits the digest header leaves the ref exactly as it
was — which is what would have run anyway — and it keeps ``Always``, correct for a name
that may move. A campaign never fails to start because this could not be applied; the log
says which refs stayed unpinned.


Waiting, blocked, and merely busy
---------------------------------

Three things a job that has not started can be doing, and the run loop treats them
differently:

``waiting``
   Planned, but not created yet: the cluster has no room for it. Normal, and unbounded
   — see above.

busy
   Created, but waiting its turn — for a node or for a pull. Either the resource exists
   on a node and something else is holding it, usually another campaign's run; or the
   image exists and the credential works, and the pull is rate-limited behind the other
   pulls a whole batch of jobs asked for at the same instant (the kubelet's own
   ``registryPullQPS`` limiter, or the registry's account limit). Both start by
   themselves, so the batch waits **15 minutes** (``CONTENDED_GRACE_SECONDS``) before
   giving up, and repeats the reason in the log each minute meanwhile. Failing either on
   the short timer threw campaigns away for the very conditions that resolve themselves.

``blocked``
   It will not start on its own: an image reference that names nothing, missing pull
   credentials, or a reservation no node can satisfy — one larger than the biggest
   machine, or a GPU that no node advertises because its device plugin is down. These
   look the same in ten minutes as in one, so the batch fails after **60 seconds**
   (``BLOCKED_GRACE_SECONDS``) with Kubernetes' own message.

The job *listing* (``vast cluster monitor``, the web UI, ``list_campaign_jobs``)
reports all three, and a busy job appears there as ``pending`` carrying Kubernetes'
message as its detail. ``pending`` is the literal truth about such a job — its pod exists
and has not started — and the reason it has not started is worth reading without being
worth acting on.

Reporting the two alike, as ``blocked``, would put a red row and a ``Blocked:`` count in
front of anyone running two campaigns at once, for a job that starts by itself as soon as a
neighbour finishes; ``blocked`` is defined just above as the state that will *not* clear, so
the count that exists to say "someone must do something" would be saying it about nothing.
It is the same mistake the ``waiting`` phase exists to avoid, and it has the same cost: a
reader who learns to ignore the alarming word stops reading it when it is real. The run loop
separates them only by how long to wait — ``blocked`` fails the batch in a minute, busy gets
fifteen — and the reason is repeated in its log each minute meanwhile.

A busy job is therefore invisible in the monitor's aggregate counts, which have no
per-job detail: it is one of ``Pending``. The run loop's log is where a CLI reader learns
which pending job is waiting on what.

The split is made per job, and on each side it is an allowlist of what is known to clear
by itself — so a reason nobody anticipated is blocked, and fails fast, rather than
sitting for the long grace on a guess.

*Scheduling*: the scheduler's message plus what the nodes advertise. ``Insufficient
<resource>`` counts as busy only when the pod's own requests fit inside some node's
``allocatable``; any other stated cause — an untolerated taint, an unmatched node
selector — is blocked, as is every cause when the node list cannot be read.

*Pulling*: the kubelet's message alone, since no node fact can make a throttled pull
permanent. Only a pull-attempt reason (``ErrImagePull``, ``ImagePullBackOff``) whose
message names a rate limit counts as busy; a manifest that is not there, a registry that
refuses the credential, a host that does not resolve, and every non-pull reason are
blocked.

If a batch reaches the 15-minute limit, the cluster is oversubscribed rather than busy:
check what else is running and whether the queue admits more than the nodes can hold —
or, for a pull reason, run fewer jobs at once than the registry will serve.


Selecting a Cluster Context
---------------------------

RoboVAST uses **kubeconfig contexts** to address different clusters.  Pass
the ``--context`` flag to any cluster sub-command to select a specific context
(as listed by ``kubectl config get-contexts``):

.. code-block:: bash

   # Use the currently active context (default)
   vast workspace run my-experiment

   # Explicitly target a context
   vast workspace run my-experiment --context gcp-c4

The ``--context`` flag is available on ``workspace run``, ``cluster setup``,
``cluster monitor``, ``cluster jobs-cleanup``, and ``cluster cleanup``.

Contexts can be renamed to shorter, human-friendly identifiers:

.. code-block:: bash

   kubectl config rename-context <old-name> <new-name>


Per-Cluster Resource Limits
----------------------------

When the **same** ``.vast`` file is used on multiple clusters that have
different hardware, resource fields (``cpu``, ``memory``, ``gpu``) can be expressed as
a list of ``{context-name: value}`` mappings instead of a plain scalar.

.. code-block:: yaml

   execution:
     resources:
       cpu:
         - gcp-c4: 4
         - local:  8
       memory:
         - gcp-c4: 10Gi
         - local:  20Gi
     secondary_containers:
       - nav:
           resources:
             cpu:
               - gcp-c4: 2
               - local:  4
       - simulation:
           resources:
             cpu:
               - gcp-c4: 2
               - local:  4
             memory:
               - gcp-c4: 8Gi
               - local:  16Gi

Rules:

* **Scalars take precedence** — a plain integer/string is used unchanged on
  every cluster.
* For per-cluster lists the entry whose key matches the active context is
  used.  If no entry matches, RoboVAST raises a ``ValueError``.
* Fields can be mixed: ``cpu`` as a scalar and ``memory`` as a per-cluster list
  is valid.
* If a per-cluster list is present and no ``--context`` is supplied, RoboVAST
  will ask you to provide one.

Running the same config on two clusters:

.. code-block:: bash

   vast workspace run my-experiment --context gcp-c4
   vast workspace run my-experiment --context local


Cloud Provider Configurations
------------------------------

Three cluster configurations are shipped out of the box.  Select the one
matching your environment. Read :ref:`cluster-cloud-limits` first: several parts of the
scheduler assume a static, hand-managed cluster, and on a managed one they degrade quietly
rather than loudly.

.. _cluster-cloud-limits:

What does not hold on managed Kubernetes
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The admission arithmetic itself is provider-agnostic: allocatable minus bound requests minus
headroom, per node, measured every cycle. What is built around it was designed against a
static bare-metal cluster, and these follow from that. None of them is a crash; each is
a silent degradation, which is why they are written down.

**A cluster whose configuration cannot report an autoscaler maximum is held at its current
size.** Admission never creates a job that no current node can hold, which is correct on a
static cluster and self-defeating on an elastic one — a pod the scheduler cannot place is
exactly what makes an autoscaler add a node. ``get_cluster_allocatable_resources`` is where a
configuration reports that maximum; admission then creates work unpinned and lets the
autoscaler respond. A configuration that does not implement it, including the generic base
one an unlisted provider falls back to, gets the static behaviour.

**The GKE implementation reports it by shelling out to** ``gcloud``, **which the service pod
does not have.** Admission runs inside that pod, whose image ships neither ``gcloud`` nor
``kubectl``; the call fails, the failure is a debug line, and the cluster is treated as
static. ``setup``'s ``gcloud`` prerequisites are for the workstation ``setup`` runs on, which
is not where admission runs.

**Node identity labels are applied at ``setup``, not continuously.** ``robovast.io/node-id``
is what pins a job to the node its capacity was reserved on, and what a calibration probe
measures against. A managed node pool replaces nodes constantly — autoscaling, auto-upgrade,
auto-repair, spot reclaim — and every replacement arrives unlabelled. Such a node still takes
work (refusing it would turn adding capacity into an outage), but it cannot be pinned to and
cannot be probed, so its jobs run at the declared sizing beside calibrated ones. Re-run
``vast exec cluster setup`` after the pool changes to bring new nodes back under
:ref:`cluster-node-calibration`.

**The CPU governor DaemonSet usually cannot work on a cloud VM** — and the way it fails is not
the way setup detects. See the warning under :ref:`cluster-cpu-governor`.

.. _cluster-config-gcp:

GCP (Google Kubernetes Engine)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

**Config name:** ``gcp``

Stores results in a **Google Cloud Storage bucket you provide**. Nothing is deployed
for storage — no MinIO pod, no PersistentVolume — so there is also nothing to reclaim
afterwards. RoboVAST never creates the bucket: it is user-managed, and setup fails
loudly rather than inventing one.

**Prerequisites:**

1. Install and authenticate the ``gcloud`` CLI.
2. Install the GKE auth plugin required by ``kubectl`` to authenticate against
   GKE clusters:

   .. code-block:: bash

      sudo apt-get install google-cloud-cli-gke-gcloud-auth-plugin

3. Fetch the cluster credentials into your kubeconfig:

   .. code-block:: bash

      gcloud container clusters get-credentials <cluster-name> --region <region>

4. Optionally rename the context for brevity:

   .. code-block:: bash

      kubectl config rename-context \
        gke_<project>_<region>_<cluster-name> gcp-c4

5. **Create the bucket yourself** and give the credential below read/write on it.

   Pass ``--ingress-class gce`` when publishing with ``--ingress-host`` on GKE's built-in
   controller. Unlike ingress-nginx it cannot route to a plain ClusterIP, so both Services
   the Ingress fronts — the UI on ``/`` and the registry on ``/v2`` — are annotated for
   container-native load balancing only when the class is named. A backend without it never
   becomes healthy, and the reason is visible in the load balancer rather than in anything
   RoboVAST prints.

6. Generate the credential — either HMAC keys for the bucket, or a service-account
   JSON key.

**Setup:**

.. code-block:: bash

   vast cluster setup gcp \
     -o gcs_bucket=my-robovast-results \
     -o gcs_access_key=GOOG... -o gcs_secret_key=...

   # or with a service-account key file instead of HMAC keys:
   vast cluster setup gcp \
     -o gcs_bucket=my-robovast-results -o gcs_key_file=./sa-key.json

Available options:

.. list-table::
   :header-rows: 1

   * - Option
     - Required
     - Description
   * - ``gcs_bucket``
     - **yes**
     - The bucket results are written to. Must already exist.
   * - ``gcs_access_key``
     - unless ``gcs_key_file``
     - HMAC access key with read/write on the bucket.
   * - ``gcs_secret_key``
     - unless ``gcs_key_file``
     - The matching HMAC secret.
   * - ``gcs_key_file``
     - unless HMAC keys
     - Service-account JSON key file, inlined into the deployment instead.

Each also has an environment variable — ``ROBOVAST_GCS_BUCKET``,
``ROBOVAST_GCS_ACCESS_KEY``, ``ROBOVAST_GCS_SECRET_KEY``, ``ROBOVAST_GCS_KEY_FILE`` —
so they can live in ``.env`` rather than on the command line. See ``.env.example``.

.. warning::

   These credentials are stored in clear text in the Deployment's
   ``ROBOVAST_CLUSTER_CONFIG_KWARGS`` environment variable, so anyone who can
   ``kubectl get deploy -o yaml`` in the namespace can read them. Scope the HMAC key
   or service account to that one bucket.

.. _cluster-config-rke2:

RKE2
^^^^

**Config name:** ``rke2``

Targets on-premise clusters managed by
`Rancher RKE2 <https://docs.rke2.io/>`_.  Uses MinIO backed by a directory on the
data node, which is where finished campaigns live; it survives the pod, and
``vast cluster cleanup`` leaves it alone.

**Prerequisites:**

* Ensure the kubeconfig for the RKE2 cluster is available (typically provided
  by the cluster administrator as ``/etc/rancher/rke2/rke2.yaml``).

**Setup:**

.. code-block:: bash

   vast cluster setup rke2

**Notes:**

* The store survives a pod restart and a ``vast cluster cleanup``, but it is one
  directory on one node and no more: archive anything that must outlive the machine
  with ``vast share``, or launch with *Upload to share when done*.
* ``vast cluster cleanup --delete-data`` is what empties it, and nothing else does.

.. _cluster-config-minikube:

Minikube
^^^^^^^^

**Config name:** ``minikube``

Targets a local `minikube <https://minikube.sigs.k8s.io/>`_ cluster.
Uses MinIO backed by a directory on the node.  Intended for development and local
integration tests.

**Prerequisites:**

* Start a minikube cluster:

  .. code-block:: bash

     minikube start

**Setup:**

.. code-block:: bash

   vast cluster setup minikube

**Notes:**

* No archiver sidecar — it is not included in the minikube manifest.  Use
  ``vast cluster store-cleanup`` to remove S3 buckets after
  processing results via ``kubectl port-forward``.
* The store outlives the pod; ``vast cluster cleanup --delete-data`` empties it.


.. _cluster-sharing:

Sharing Results
---------------

The object store is the campaign's durable home and the default delivery path —
``vast campaign download`` streams the campaign straight from it, so **no external
share is required**. An external share (Nextcloud, GCS, …) is for getting a campaign
somewhere the object store does not reach: another deployment, or a colleague.

``vast share`` — the six verbs, and who performs them
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

=======================  ======================  ============
verb                     moves                   performed by
=======================  ======================  ============
``vast share list``      (reads the share)       you
``vast share download``  share → your machine    you
``vast share upload``    your machine → share    you
``vast share remove``    (deletes on the share)  you
``vast share export``    service → share         the service
``vast share import``    share → service         the service
=======================  ======================  ============

**Who performs a verb is decided by its endpoints, not its direction: the service
acts when a campaign in the service is one end; you act when the two ends are your
machine and the share.** That is what makes a **read-only** share credential a
supported way to be set up — ``list`` and ``download`` work with it, and
``upload``/``remove`` are refused by the share, which is what read-only means.
``remove`` deliberately does *not* borrow the service's credentials to get around
that: doing so would let anyone who can reach RoboVAST delete share content the share
itself would refuse them. Only ``export`` uses the service's write access, which is
its whole purpose and exactly what the campaign-end upload already does.

So your ``.env`` holds *your* share credentials, and the service's
``robovast-share-credentials`` Secret holds the ones that can write.

``vast share import`` is the one worth knowing about: the **service** downloads from
the share, so a multi-gigabyte campaign never travels through your machine to get
between two servers, and you need no share credentials at all for it. What arrives
raw is postprocessed automatically once it lands, so you get a campaign with its
metric tables rather than a directory to remember to reprocess.

**The share is not a subset of what a service has.** A campaign can be deleted here
while its archive stays up there — ``vast share list`` marks such an archive
``importable``, and that case is the main reason import exists.

Archive names
^^^^^^^^^^^^^

``<campaign-id>.raw.tar.gz`` or ``<campaign-id>.postprocessed.tar.gz``. Nobody is
asked which: it is read off the campaign (``_transient/postprocessing.yaml`` is
postprocessing's own provenance record, written last and by nothing else), so the
campaign-end upload and a later
``vast share export`` cannot disagree. An archive uploaded before the name carried a
variant is read as ``raw``, which is what it is.

Raw is the right default for the share because it is the irreplaceable part, not
because it is small: rosbags and ``rosout`` dominate a campaign and are in both
variants, so a raw archive is roughly three quarters the size of a postprocessed one,
not a fraction of it.

How it works
^^^^^^^^^^^^

Pushing at launch is an opt-in step run **in the driver**: enable *Upload to share
when done* in the web UI, pass ``--upload-to-share`` to ``vast workspace run``, or set the MCP ``upload_to_share`` flag. No data reaches the user's machine and
no separate archiver pod is involved.

When the toggle is set, the driver — the moment the scenario runs finish and
**before** analysis postprocessing — streams the campaign to the share:

1. **Pre-flight** — the driver verifies the share credentials before streaming, so
   a misconfigured share fails fast.
2. **Stream + upload** — the campaign (already on the driver's scratch from the run)
   is tarred and gzipped **on the fly** straight into the share provider's request
   body. No compressed copy is written to disk — decisive for ~1TB campaigns — and
   the archive is the **raw, pre-postprocessing** snapshot, so the shared copy stays
   minimal and untouched (postprocessing only *adds* derived data, which lands in
   the object store and the postprocessed download instead).
3. **On failure** the campaign is untouched in the object store and the run
   continues normally — the share is best-effort and never loses results. The failure
   reason is recorded on the campaign's ``share_error`` (durable across a service
   restart) and shown as a warning in the UI; the campaign still reports ``finished``.

The upload can be run again on a finished campaign at any time — ``vast share export
-i <campaign-id>``, the web UI's *Export to share* action, the MCP ``run_share`` tool,
or ``POST /campaigns/{id}/share/run`` — and it works from the stored campaign alone, so
it is available even after the service was restarted. A re-trigger uses the share
provider currently configured in the environment, so adjusting ``ROBOVAST_SHARE_TYPE``
and re-triggering uploads to a different provider.

A re-trigger runs in the **service**, not the driver, and stages nothing on the way: the
campaign's objects are tarred straight out of the object store into the provider's
request body, the same no-scratch path that serves ``GET /campaigns/{id}/archive``. Only
the campaign's small status objects (``_execution/outcome.json``, ``_execution/data.db``,
``campaign.db``) are pulled down, because the outcome is edited and published back. The
variant is read off what is there, so a campaign that has since been postprocessed goes
up as ``postprocessed`` where the campaign-end upload sent ``raw``.

**Watching it.** The campaign enters the ``sharing`` phase and publishes live progress
into its status (``extra.upload``), which the campaign view renders as a bar and
``vast cluster monitor`` prints. Because the archive is gzipped on the fly its
compressed length is unknown until the last byte, so the **bar measures the campaign
bytes going into the archive** (``source_done``/``source_total``) and the bytes actually
sent are reported beside it — the two differ by the compression ratio. A provider or lane
that can offer no total leaves ``percent`` null and the bar indeterminate.

The destination is whatever ``ROBOVAST_SHARE_TYPE`` (and its variables) name in the
service's ``.env`` — the launch flag is a pure on/off switch and carries no
credentials.

Configuration via ``.env``
^^^^^^^^^^^^^^^^^^^^^^^^^^

All credentials and share URLs are stored in a ``.env`` file in the directory you
run ``vast`` from.  The file is **never** committed to the ``.vast`` project
configuration, keeping secrets out of version control.

Load order: every ``vast`` command loads two files once before it runs — ``./.env``
(the current directory only, no walk up to the root) and then
``~/.config/robovast/env`` — so *any* variable RoboVAST reads from the environment
(share credentials, registry, ntfy, ``ROBOVAST_PROJECT``, …) can be kept in either.
They merge key by key: a real environment variable beats both, ``./.env`` beats the user
file, and a missing file is fine.

Which of the two: ``./.env`` is this *project's* configuration; the user file is for what
is true of your machine, and is the one that keeps working when you run ``vast`` from
another directory.  See :doc:`images` for the image settings specifically.

**Reading it back.** A ``.env`` says what you *sent*; the Admin page's **Service
configuration** panel says what the service is actually running with, which is not the same
thing the moment a value changes. A pod loads its Secrets at container start and never
again, so an edited ``.env`` reaches it only through ``vast service upgrade`` **without**
``--no-restart`` — ``setup --force`` also works, and does more besides. A local
``vast serve`` picks a change up when it is restarted. Credentials are reported there as
set or not set; their values never leave the service. See :ref:`web-ui-admin`.

**Required variables (for all share types):**

.. code-block:: ini

   ROBOVAST_SHARE_TYPE=<provider>   # e.g. nextcloud

**Provider-specific variables** are listed in the sections below.

.. note::

   If any required variable is missing, the command prints a clear error
   message listing what is needed before performing any cluster operation.

Nextcloud
^^^^^^^^^

The Nextcloud share must be a **public link that allows file uploads without
a password** ("Allow upload and editing" enabled in the Nextcloud sharing
dialog).

.. code-block:: ini

   ROBOVAST_SHARE_TYPE=nextcloud

   # Copy the link from the Nextcloud sharing dialog.
   # Example: https://cloud.example.com/s/AbCdEfGhIjKlMn
   ROBOVAST_SHARE_URL=https://cloud.example.com/s/<token>

The upload uses the WebDAV public-share endpoint (``/public.php/webdav/``)
with the share token as the HTTP Basic-Auth username and an empty password.
Only the standard Python library is used — no additional packages need to be
installed.

Progress output
^^^^^^^^^^^^^^^

The tar+gzip stream and upload run in the driver; a single-line progress bar
(transferred size and rate — no percentage, since the streamed archive's length is
not known up front) is written to the campaign log — view it with ``vast cluster monitor`` or the web UI log panel:

.. code-block:: text

   campaign-2026-03-01-120000  streaming to share  1.2 MiB  3.4 MiB/s
   campaign-2026-03-01-120000  uploaded (2.0 MiB)  ✓

Google Cloud Storage (GCS)
^^^^^^^^^^^^^^^^^^^^^^^^^^

The GCS provider uploads archives directly from the service to a GCS
bucket using a service-account key.  Downloads use the public GCS HTTP API
and **do not require credentials** when the bucket is publicly readable.

.. code-block:: ini

   ROBOVAST_SHARE_TYPE=gcs

   # GCS bucket name
   ROBOVAST_GCS_BUCKET=my-robovast-results

   # Required for upload-to-share (launching with the toggle) only.
   # Not needed for results download on public buckets.
   ROBOVAST_GCS_KEY_FILE=/path/to/service-account-key.json

   # Optional: object-name prefix inside the bucket (default: bucket root)
   # ROBOVAST_GCS_PREFIX=results/

**Service-account setup (upload only):**

1. Create a service account in the GCP IAM console.
2. Grant it the *Storage Object Creator* role on the target bucket.
3. Generate a JSON key, download it, and set ``ROBOVAST_GCS_KEY_FILE`` to its
   path.

**Making the bucket publicly readable (for download):**

Grant the ``Storage Object Viewer`` role to the special principal
``allUsers`` in the GCP console (or via ``gsutil iam``):

.. code-block:: bash

   gsutil iam ch allUsers:objectViewer gs://my-robovast-results

Once the bucket is public, ``vast campaign download`` works without
any credentials — only ``ROBOVAST_SHARE_TYPE`` and ``ROBOVAST_GCS_BUCKET``
need to be set.
