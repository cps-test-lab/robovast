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
run`` finds it — auto-detected on the conventional local port (a ``vast serve``,
a ``vast serve --attach`` or a tunnel you brought up), or pass ``--cluster`` to open an ephemeral
``kubectl port-forward`` for the call — pushes the local project into a server-side
workspace, and starts the campaign there. It is *fire-and-forget*
— it returns immediately with the campaign id, and the campaign continues in the
cluster. Internally:

1. **Launch** — The client pushes the project to a workspace and calls
   ``create_campaign``. The service starts a :class:`CampaignController` in a
   worker thread over ``KubernetesBackend``. No per-campaign controller pod is
   created; the service is the driver.
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
   on the conventional local port, or reached with ``--cluster`` — there is no
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
     - Install and upgrade Kueue (the job-queueing controller) via the Helm
       chart registry
     - `helm install guide <https://helm.sh/docs/intro/install/>`_
   * - ``k9s`` *(recommended)*
     - Terminal UI for monitoring pods, jobs, and logs in real time — not
       required but greatly simplifies observability during a run
     - `k9s install guide <https://k9scli.io/topics/install/>`_

For GCP clusters the ``gcloud`` CLI is additionally required — see
:ref:`cluster-config-gcp` below.


Cluster Setup
-------------

Before the first run, deploy the MinIO S3 server and Kueue into the cluster:

.. code-block:: bash

   vast execution cluster setup <cluster-config>

Available cluster configs (``--list``):

.. code-block:: bash

   vast execution cluster setup --list

The setup command:

* Deploys a ``robovast`` pod containing the MinIO S3 server (embedded-storage
  configs such as ``rke2``). External-storage configs (e.g. GCS) deploy no
  helper pod — the bucket is used directly.
* Installs `Kueue <https://kueue.sigs.k8s.io/>`_ via Helm and sizes its job
  queue to the cluster's available CPU/memory.

To tear everything down after use:

.. code-block:: bash

   vast execution cluster cleanup


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
auto-detected on the conventional local port, or reached with ``--cluster``; run
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

The service is auto-detected on the conventional local port, or pass ``--cluster``
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
once an **hour** with the current run progress, when the campaign **finishes**,
when it is **uploaded** to the share, and (urgently) on **failure**.

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
<config-build-section>`) are built **in-cluster** by a BuildKit Job and pushed to a
container registry the cluster can pull from. Point the deployment at a registry in
your ``.env`` before ``setup`` (or a later ``setup --force``) — the values are stored in the
service pod's environment (like the share/ntfy credentials) and read back by the
cluster config's ``get_registry_config()``; **no registry detail ever reaches a
client**.

.. code-block:: bash

   ROBOVAST_REGISTRY_PREFIX=ghcr.io/cps-test-lab      # required to enable builds
   # Optional: registry auth → a dockerconfigjson Secret used for push (build Job)
   # and pull (campaign pods). Omit for an anonymous/insecure registry.
   ROBOVAST_REGISTRY_SERVER=ghcr.io
   ROBOVAST_REGISTRY_USERNAME=<user>
   ROBOVAST_REGISTRY_PASSWORD=<token>
   # Optional: trust a self-signed / private-CA registry. The CA is stored in a
   # ConfigMap and mounted into the build Job (BuildKit trusts the registry API AND
   # its auth/token endpoint). Preferred over INSECURE for anything real.
   ROBOVAST_REGISTRY_CA_FILE=/path/to/registry-ca.pem
   # Optional: skip TLS verification instead (plain HTTP / throwaway registry).
   ROBOVAST_REGISTRY_INSECURE=true
   # Optional: default base image when build.base_image is omitted.
   ROBOVAST_BASE_EXPERIMENT_IMAGE=ghcr.io/cps-test-lab/sim-suite-nav2-eval:latest

The service prepends ``ROBOVAST_REGISTRY_PREFIX`` to the project's bare
``build.tag`` and pushes ``<prefix>/<tag>:<hash>``; campaign pods pull it via the
same credentials (added as an ``imagePullSecret``). Without a registry configured,
in-cluster builds are unavailable and a ``build:<tag>`` project fails at submit
with an actionable message.

The auth Secret and CA ConfigMap that ``setup`` creates from those values are
**found by the service itself** — it looks for the fixed names it would have created
(``robovast-registry-push``, ``robovast-registry-ca``) and uses them when present, so
nothing has to tell it they exist. This matters for a **local** ``vast serve``:
``setup`` stores its env in the *service pod*, which an off-cluster service never
reads, and the previous behaviour was to conclude there were no credentials and no CA
— pushing anonymously to an untrusted registry. Set
``ROBOVAST_REGISTRY_PUSH_SECRET`` / ``_PULL_SECRET`` / ``_CA_CONFIGMAP`` only to point
at **differently named** objects; they are overrides, not requirements.

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
   can add one. Note also that using the registry's **IP** as
   ``ROBOVAST_REGISTRY_PREFIX`` is not a substitute when it sits behind an SNI-based
   proxy: a client dialing a bare IP sends no TLS SNI, so such a proxy serves no
   certificate at all and the handshake fails before ``INSECURE`` or a CA could apply.

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
  entry no longer rebuilds the ones before it. A failing cache **export** never fails
  the build (the image is already pushed by then); a failing **import** just makes the
  build slower. Both refs inherit the deployment's ``INSECURE`` / CA settings, since
  they address the same registry as the push.

The registry therefore needs room for one extra tag per ``build.tag``. Ordering the
``python_packages`` list by change frequency is what makes the layer cache pay off —
see :ref:`the build section <config-build-caching>`.

.. note::

   Rootless BuildKit needs AppArmor **and** seccomp ``Unconfined`` (the build Job
   sets both). On nodes whose container runtime cannot pull from the registry
   without host trust (e.g. an in-cluster registry over plain HTTP on RKE2/k3s
   containerd), the node must be configured to trust it (``registries.yaml``) for
   campaign pods to pull the built image — an external registry with valid TLS
   avoids this.


Manual Deployment (prepare-run)
---------------------------------

A **batch-only** debugging aid: generate all manifests and scripts **without
running them** (e.g. for airgapped clusters, CI pipelines, or to inspect exactly
what the service would submit):

.. code-block:: bash

   vast execution cluster prepare-run ./output-dir

The generated Job manifests are produced by the same builder the controller uses
at run time, so they match what a real run submits. (For search campaigns, use
``vast execution cluster run``.)

To bake in credentials, ``prepare-run`` reads the cluster config from the deployed
robovast-service (so it needs kubeconfig access to that cluster); pass
``--cluster-config <name>`` with ``-o key=value`` credentials to run fully offline
against a cluster that isn't up yet.

The output directory contains:

* ``robovast-manifest.yaml`` — robovast base services (e.g. MinIO pod/service manifest)
* ``kueue-queue-setup.yaml`` + ``README_kueue.md`` — Kueue queue objects
* ``out_template/`` — scenario configuration files
* ``jobs/`` — individual Kubernetes Job YAML files per scenario/run
* ``all-jobs.yaml`` — all jobs in a single file
* ``upload_configs.py`` — script to upload configs to S3
* ``README.md`` + cluster-specific README files


Job Queueing with Kueue
-----------------------

Cluster jobs are queued by `Kueue <https://kueue.sigs.k8s.io/>`_, which ``vast
execution cluster setup`` installs and sizes to the cluster. It admits jobs only
when there is CPU and memory for them, so a large campaign cannot oversubscribe
the nodes, and several campaigns launched at once share the cluster instead of
fighting over it. There is nothing to configure — every job RoboVAST creates is
submitted to the queue automatically.

**Jobs waiting is normal.** A campaign whose jobs sit in the queue is healthy: it
is waiting for capacity, not stuck. ``vast execution cluster monitor`` and
``list_campaign_jobs`` report such jobs as ``blocked``, with Kueue's own reason as
the detail.

If the queue is genuinely unusable — setup was never run, or the campaign targets
a namespace that was never set up — the campaign fails at launch with a message
naming what is missing, rather than hanging. ``setup`` checks the same thing
before reporting success.

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
``prepare-run``, ``run-cleanup``, and ``cleanup``.

Contexts can be renamed to shorter, human-friendly identifiers:

.. code-block:: bash

   kubectl config rename-context <old-name> <new-name>


Per-Cluster Resource Limits
----------------------------

When the **same** ``.vast`` file is used on multiple clusters that have
different hardware, resource fields (``cpu``, ``memory``) can be expressed as
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

Uses a GCP Persistent Disk (PD) as MinIO storage, provisioned automatically
through a dedicated ``StorageClass``.

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

**Setup:**

.. code-block:: bash

   vast execution cluster setup gcp

   # With a larger disk or a faster disk type:
   vast execution cluster setup gcp \
     --option storage_size=50Gi \
     --option disk_type=pd-ssd

Available options:

.. list-table::
   :header-rows: 1

   * - Option
     - Default
     - Description
   * - ``storage_size``
     - ``10Gi``
     - Size of the GCP PD PVC
   * - ``disk_type``
     - ``pd-standard``
     - GCP PD type (``pd-standard``, ``pd-ssd``, ``pd-balanced``)

.. note::

   After a cleanup, the PersistentVolume may need to be deleted manually
   in the GCP console (the ``StorageClass`` uses ``reclaimPolicy: Delete``
   but cloud disks are not always reclaimed immediately).

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

Load order: every ``vast`` command loads ``./.env`` once before it runs — the
current directory only, no walk up to the root — so *any* variable RoboVAST reads
from the environment (share credentials, registry, ntfy, ``ROBOVAST_IMAGE`` /
``ROBOVAST_CONTROLLER_IMAGE``, …) can be kept there.  A real environment variable
beats a ``.env`` line, and a missing file is fine.

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
