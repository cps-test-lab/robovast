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
service (``vast execution cluster setup`` installs it); ``vast execution cluster
run`` finds it — a service answering on the conventional local port, or the deployed
one ``vast login`` recorded — pushes the local project into a server-side
workspace, and starts the campaign there. It is *fire-and-forget*
— it returns immediately with the campaign id, and the campaign continues in the
cluster.

The project it runs comes from ``vast init``, or straight from a file, which needs no
project at all::

   vast -V configs/examples/growth_sim/growth_sim.vast exec cluster run \
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
   aux pod the driver execs into during composition.)
3. **Queueing (Kueue)** — Jobs are admitted only when sufficient CPU/memory is
   available, so a campaign cannot oversubscribe the cluster.
4. **Result collection** — Jobs upload result files back to the storage bucket,
   and the driver publishes the **canonical campaign** (``campaign.db`` +
   ``_execution`` + results) there. The **object store is the durable home and
   the delivery mechanism**: the service streams downloads straight from it
   (``vast results download`` / ``--wait-and-download``), so no external share is
   required. Pushing a copy to an external ``tar.gz`` **share** is opt-in **at
   launch** — enable *Upload to share when done* in the web UI launcher (or
   ``--upload-to-share`` / the MCP ``upload_to_share`` flag). When set, the driver
   streams a **raw, pre-postprocessing** archive to the configured share the moment
   the runs finish, *before* analysis postprocessing adds derived data — so the
   shared copy stays minimal and untouched. Track progress with ``vast execution
   cluster monitor``; ``vast execution cluster download-cleanup`` removes the
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
     - Install and upgrade Kueue (the job-queueing controller) and, on a GPU
       cluster, the NVIDIA device plugin, via the Helm chart registry
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

Before the first run, deploy the MinIO S3 server and Kueue into the cluster:

.. code-block:: bash

   vast execution cluster setup <cluster-config>

Available cluster configs (``--list``):

.. code-block:: bash

   vast execution cluster setup --list

Setup acts on the *cluster*, not on a project: it neither needs nor reads a ``vast
init`` / ``.robovast_project``, and runs from any directory. Its one optional input
from a ``.vast`` is the node labels, taken only from a config you name explicitly
(:ref:`below <cluster-node-labels>`).

The setup command:

* Deploys a ``robovast`` pod containing the MinIO S3 server (embedded-storage
  configs such as ``rke2``). External-storage configs (e.g. GCS) deploy no
  helper pod — the bucket is used directly.
* Installs `Kueue <https://kueue.sigs.k8s.io/>`_ via Helm and sizes its job
  queue to the cluster's available CPU/memory — and to its GPUs, where it has any.
* Makes GPUs schedulable where the cluster has them, so a simulation container renders in
  hardware instead of in software (:ref:`below <cluster-gpu>`). A cluster without GPUs is
  left exactly as it was.

.. _cluster-node-labels:

Pinning pods to a node pool
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Node selectors for the job pods and the control pod come from
``execution.kubernetes.jobs.node_labels`` / ``execution.kubernetes.control.node_labels``
(see :doc:`configuration`) and are baked into the cluster at setup. Name that ``.vast``
explicitly — it is the only config setup will read:

.. code-block:: bash

   vast -V my_campaign.vast execution cluster setup rke2

Without ``-V`` no node selectors are deployed (logged at INFO) and pods schedule
wherever Kubernetes puts them. A project's ``.vast`` is deliberately *not* consulted: a
``.robovast_project`` is found by walking up to the filesystem root, so one several
directories above the CWD would otherwise pin a cluster's pods — or, if it names a
``.vast`` that has since moved, fail the deploy over a file nobody mentioned.

A named ``.vast`` that cannot be read is an error rather than a silent "no labels" — a
config that fails to load cannot be asked whether labels were intended, and guessing
"none" would scatter job and control pods across arbitrary nodes.

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

   vast execution cluster setup rke2 -x local     # provisions GPUs if the cluster has them

What that does, when a GPU is found: installs the `NVIDIA device plugin
<https://github.com/NVIDIA/k8s-device-plugin>`_ with time-slicing so several pods can share
one card, adds ``nvidia.com/gpu`` to the Kueue quota, and puts ``runtimeClassName: nvidia``
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

   vast execution cluster setup rke2 -x local --force --gpu-replicas 24
   vast execution cluster setup rke2 -x local --no-gpu       # opt out entirely

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

   vast execution cluster cleanup

Cleanup removes the device plugin too, but never the ``nvidia`` RuntimeClass or the host's
driver and toolkit: those belong to the cluster and its node administrator.


Running Scenarios
-----------------

.. code-block:: bash

   # Run all configs defined in the project's .vast file
   vast execution cluster run

   # Override the number of runs from the CLI
   vast execution cluster run --runs 5

   # Run only one specific config by name (batch campaigns)
   vast execution cluster run --config my-config

``run`` is fire-and-forget: it starts the campaign on the service and returns
immediately, printing the campaign id. The campaign continues in the cluster —
watch it with ``vast execution cluster monitor``. (It needs a reachable service:
auto-detected on the conventional local port, or the one ``vast login`` stored; run
``vast execution cluster setup`` first if you have none.)


Monitoring and Results
----------------------

Check the status of a running (or recently completed) run:

.. code-block:: bash

   vast execution cluster monitor

The service publishes the finished campaign to the object store automatically,
and ``vast results download`` (or ``run --wait-and-download``) streams it from
there — no external share needed:

.. code-block:: bash

   # Postprocessed campaign (full, incl. derived data), streamed from the service:
   vast results download -i campaign-2025-06-01-120000

   # List what is downloadable and from where (service = postprocessed, share = raw):
   vast results list-downloads

   # Restrict the listing to specific campaigns (positional, one or more):
   vast results list-downloads campaign-2025-06-01-120000

``vast results download`` picks its source automatically from what is reachable — a
running service serves the **postprocessed** archive from the object store; a
configured external share serves the **raw** (pre-postprocessing) archive that
*Upload to share when done* produced. Force one with ``--variant postprocessed`` or
``--variant raw``. Both stream end-to-end, so a ~1TB campaign never has to be
buffered on the service or in memory.

To push a copy to an external share, enable it **at launch** (*Upload to share when
done* in the web UI, ``--upload-to-share`` on ``vast execution cluster run``, or the
MCP ``upload_to_share`` flag). The share/``.env`` settings determine the
destination; the archive delivered there is the raw pre-postprocessing snapshot.

Clean up only the job objects (without touching the result storage):

.. code-block:: bash

   vast execution cluster run-cleanup
   vast execution cluster run-cleanup --campaign campaign-2025-06-01-120000

Remove result buckets from the object store (after uploading or when no longer
needed). This runs **through the robovast-service** — it holds the object-store
credentials, so no local credentials are needed and a bulk delete never removes a
campaign that is still running:

.. code-block:: bash

   vast execution cluster download-cleanup
   vast execution cluster download-cleanup --campaign campaign-2025-06-01-120000

The service is auto-detected on the conventional local port, or named by ``vast login``
(``-x`` context, ``-n`` namespace) to tunnel to the in-cluster service for the call.
``run-cleanup --data`` deletes the buckets the same way after removing the Jobs.


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

The ending message is worth reading rather than glancing at. It is sent when the
campaign is genuinely over, *after* postprocessing rather than when the last run
stops, and it carries what the campaign actually produced: the run tally, and any
postprocessing or upload failure. A campaign whose trials all passed but whose
postprocessing failed is reported as "finished WITH PROBLEMS" — it has no CSVs
and no ``data.db``, and reporting that as a clean finish made a campaign with no
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
a second container in the service pod, so there is nothing to configure and no site
prerequisite: a deployment can always build.

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

* **Published, but the prefix is unset.** A ``setup`` re-run *without* ``--ingress-host``
  used to drop it while leaving the Ingress alone, so nothing looked wrong until a campaign
  was submitted. ``vast exec cluster upgrade`` re-bakes the prefix from the live Ingress.
  (Setup no longer drops it; deployments set up before that fix still need the upgrade.)
* **Never published.** Re-run ``setup`` with ``--ingress-host``.

``vast doctor -n <namespace>`` says which of the two you are in.

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
created. Rotation always worked; removal used to be ignored, leaving a revoked credential
deployed and still attached as an ``imagePullSecret``.

The pull Secret that ``setup`` creates from those values is **found by the service
itself** — it looks for the fixed name it would have created (``robovast-registry-push``)
and uses it when present, so nothing has to tell it it exists. This matters for a
**local** ``vast serve``: ``setup`` stores its env in the *service pod*, which an
off-cluster service never reads, and the previous behaviour was to conclude there were no
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

   ROBOVAST_EXTRA_HOST_ALIASES=harbor.example.org=10.181.120.39
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

.. _campaign-index-storage:

The campaign index
~~~~~~~~~~~~~~~~~~

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
(``vast results delete``, or the bucket cleanup below). See :ref:`campaign-discovery`.

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

Every build gets a **fresh** BuildKit pod, so nothing on a node's disk is reused
between builds. Two registry-backed mechanisms replace that:

* **Is it already built?** The service asks the registry whether
  ``<prefix>/<tag>:<hash>`` already has a manifest, and skips the build if so. This
  is deliberately not derived from the build Job's status: that Job is deleted after
  ``ttlSecondsAfterFinished`` (1 h) and the in-process record is lost on a service
  restart, after which a bit-identical image used to be rebuilt and re-pushed. The
  probe **fails closed** — if the registry cannot be reached or authenticated, the
  image counts as absent and is rebuilt, because a wrong cache hit would leave the
  campaign pods in ``ImagePullBackOff``.
* **Layer reuse across hashes.** The build imports from and exports to
  ``<prefix>/<tag>:buildcache`` (``mode=max``, so intermediate layers are kept too).
  This tag is *not* hash-qualified — that is the point: the build for a new hash
  reuses the layers of the previous one, so changing one late ``python_packages``
  group no longer rebuilds the ones before it. A failing cache **export** never fails
  the build (the image is already pushed by then); a failing **import** just makes the
  build slower. Both refs inherit the deployment's ``INSECURE`` / CA settings, since
  they address the same registry as the push.

The registry therefore needs room for one extra tag per built container. Grouping the
``python_packages`` list by change frequency is what makes the layer cache pay off —
see :ref:`containers <config-containers>`.

.. note::

   Rootless BuildKit needs AppArmor **and** seccomp ``Unconfined`` (the build Job
   sets both). On nodes whose container runtime cannot pull from the registry
   without host trust (e.g. an in-cluster registry over plain HTTP on RKE2/k3s
   containerd), the node must be configured to trust it (``registries.yaml``) for
   campaign pods to pull the built image — an external registry with valid TLS
   avoids this.


Job Queueing with Kueue
-----------------------

Cluster jobs are queued by `Kueue <https://kueue.sigs.k8s.io/>`_, which ``vast
execution cluster setup`` installs and sizes to the cluster. It admits jobs only
when there is CPU and memory for them — and GPUs, on a cluster that has them — so a
large campaign cannot oversubscribe the nodes, and several campaigns launched at once
share the cluster instead of fighting over it. There is nothing to configure: every job
RoboVAST creates is submitted to the queue automatically, and the queue is sized from
what the nodes advertise.

One consequence is worth knowing, because Kueue's answer to it is silence. A job that
asks for a resource the ClusterQueue does not cover is not rejected — it is **suspended,
indefinitely**, and a suspended job still counts as active, so a campaign would report
"still running" forever with nothing to show. RoboVAST therefore checks coverage before
creating any job and fails with the remedy instead. If you see that error, re-run
``vast execution cluster setup`` (or ``upgrade``, which reconciles the queues too).

**Jobs waiting is normal.** A campaign whose jobs sit in the queue is healthy: it
is waiting for capacity, not stuck. ``vast execution cluster monitor``, the web UI
and ``list_campaign_jobs`` report such jobs as ``waiting`` — a status of its own,
distinct from ``pending`` (a pod exists and is being scheduled) and from ``blocked``
(the job cannot start and needs a human). Kueue's own reason rides along as the
job's ``detail``.

A ``waiting`` job has no pod, so it appears in the web UI only as the ``waiting N``
counter, never as a row: the per-job list mirrors the jobs that actually exist on the
cluster (what ``k9s`` shows), and those are the ones with a pod and a log to read.
``list_campaign_jobs`` still returns every one of them with its reason.

If the queue is genuinely unusable — setup was never run, or the campaign targets
a namespace that was never set up — the campaign fails at launch with a message
naming what is missing, rather than hanging. ``setup`` checks the same thing
before reporting success.

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

   Without Kueue installed, jobs are still created but never queued: they all
   start at once and can overload the cluster.


Selecting a Cluster Context
---------------------------

RoboVAST uses **kubeconfig contexts** to address different clusters.  Pass
the ``--context`` flag to any cluster sub-command to select a specific context
(as listed by ``kubectl config get-contexts``):

.. code-block:: bash

   # Use the currently active context (default)
   vast execution cluster run

   # Explicitly target a context
   vast execution cluster run --context gcp-c4

The ``--context`` flag is available on ``setup``, ``run``, ``monitor``,
``run-cleanup``, and ``cleanup``.

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

   vast execution cluster run --context gcp-c4
   vast execution cluster run --context local


Cloud Provider Configurations
------------------------------

Three cluster configurations are shipped out of the box.  Select the one
matching your environment.

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

6. Generate the credential — either HMAC keys for the bucket, or a service-account
   JSON key.

**Setup:**

.. code-block:: bash

   vast exec cluster setup gcp \
     -o gcs_bucket=my-robovast-results \
     -o gcs_access_key=GOOG... -o gcs_secret_key=...

   # or with a service-account key file instead of HMAC keys:
   vast exec cluster setup gcp \
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
`Rancher RKE2 <https://docs.rke2.io/>`_.  Uses MinIO with an ``emptyDir``
volume — data persists as long as the pod is alive.

**Prerequisites:**

* Ensure the kubeconfig for the RKE2 cluster is available (typically provided
  by the cluster administrator as ``/etc/rancher/rke2/rke2.yaml``).

**Setup:**

.. code-block:: bash

   vast execution cluster setup rke2

**Notes:**

* ``emptyDir`` is ephemeral: if the ``robovast`` pod is restarted, all data is
  lost.  Download results with ``vast results download`` (or launch with *Upload
  to share when done*) before modifying or restarting the pod.

.. _cluster-config-minikube:

Minikube
^^^^^^^^

**Config name:** ``minikube``

Targets a local `minikube <https://minikube.sigs.k8s.io/>`_ cluster.
Uses MinIO with ephemeral ``emptyDir`` storage.  Intended for development
and local integration tests.

**Prerequisites:**

* Start a minikube cluster:

  .. code-block:: bash

     minikube start

**Setup:**

.. code-block:: bash

   vast execution cluster setup minikube

**Notes:**

* No archiver sidecar — it is not included in the minikube manifest.  Use
  ``vast execution cluster download-cleanup`` to remove S3 buckets after
  processing results via ``kubectl port-forward``.
* ``emptyDir`` storage means all data is lost if the pod restarts.


.. _cluster-sharing:

Sharing Results
---------------

The object store is the campaign's durable home and the default delivery path —
``vast results download`` streams the campaign straight from it, so **no external
share is required**. Pushing to an external share (Nextcloud, GCS, …) is an opt-in
**launch-time** step run **in the driver**: enable *Upload to share when done* in
the web UI, pass ``--upload-to-share`` to ``vast execution cluster run``, or set the
MCP ``upload_to_share`` flag. No data ever reaches the user's machine, and no
separate archiver pod is involved.

How it works
^^^^^^^^^^^^

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

The upload can be **re-triggered** on a finished campaign at any time — from the web
UI's *Retrigger upload-to-share* action, the MCP ``run_share`` tool, or
``POST /campaigns/{id}/share/run`` — and it works from the stored campaign alone, so
it is available even after the service was restarted. A re-trigger uses the share
provider currently configured in the environment, so adjusting ``ROBOVAST_SHARE_TYPE``
and re-triggering uploads to a different provider.

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
not known up front) is written to the campaign log — view it with ``vast execution
cluster monitor`` or the web UI log panel:

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

Once the bucket is public, ``vast results download`` works without
any credentials — only ``ROBOVAST_SHARE_TYPE`` and ``ROBOVAST_GCS_BUCKET``
need to be set.
