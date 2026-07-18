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
run`` finds it — auto-detected on the conventional local port (a ``vast serve`` /
``vast ui`` or a tunnel you brought up), or pass ``--cluster`` to open an ephemeral
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
3. **Queueing (Kueue)** — Jobs are submitted to a dedicated Kueue
   ``LocalQueue`` (``robovast``).  Kueue's gang-scheduling and resource quotas
   ensure that jobs are admitted only when sufficient CPU/memory is available,
   preventing cluster oversubscription.
4. **Result collection** — Jobs upload result files back to the storage bucket,
   and the driver publishes the **canonical campaign** (``campaign.db`` +
   ``_execution`` + results) there. The **object store is the durable home and
   the delivery mechanism**: the service streams downloads straight from it
   (``vast results download`` / ``--wait-and-download``), so no external share is
   required. An external ``tar.gz`` share is optional and on-demand: run ``vast
   execution cluster upload-to-share`` at any time — it is a stateless service
   call that reads the finished campaign from the object store, so a failed upload
   is simply retried (no process has to stay alive). Track progress with ``vast
   execution cluster monitor``; ``vast execution cluster download-cleanup`` removes
   the buckets once results have been handled.

.. note::

   Live status, ``stop``, ``monitor`` and ``upload-to-share`` all go through the
   service — auto-detected on the conventional local port, or reached with
   ``--cluster`` — there is no controller pod to ``kubectl port-forward`` into any
   more. The web UI additionally streams each campaign's ``controller.log`` live
   from the service.


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
* Installs `Kueue <https://kueue.sigs.k8s.io/>`_ via Helm and creates a
  ``ClusterQueue`` and ``LocalQueue`` sized to the cluster's available
  CPU/memory.

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
there — no external share needed. To additionally push the campaign to an external
share, or to **retry** an upload that failed (e.g. after the share was full or
briefly unreachable):

.. code-block:: bash

   vast execution cluster upload-to-share

This is a **stateless** service call: it reads the finished campaign from the
object store, so it is safely repeatable — nothing has to have stayed alive since
the run. It needs no arguments (the service's own share settings are used); if you
set/correct the share settings in your ``.env`` first, they are sent as overrides
for this attempt (and may even switch the share type).

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

RoboVAST uses `Kueue <https://kueue.sigs.k8s.io/>`_ (version |kueue_version|)
for admission control and resource quotas.

.. |kueue_version| replace:: 0.16.1

**What Kueue does:**

* Admits batch jobs only when the cluster has enough CPU and memory.
* Queues excess jobs and starts them as capacity becomes available.
* Prevents oversubscription: no node goes out-of-memory from too many
  concurrent simulation pods.
* Enables fair sharing when the cluster is shared with other workloads.

**How it is set up:**

* A single ``ResourceFlavor`` (``default-flavor``) represents the cluster's
  homogeneous node pool.
* A ``ClusterQueue`` (``robovast-cluster-queue``) holds the combined CPU/memory
  quota, sized automatically from ``allocatable − requested`` at setup time.
* A ``LocalQueue`` named ``robovast`` in the execution namespace is the
  submission target for every RoboVAST job.

Each generated Job manifest carries the annotation
``kueue.x-k8s.io/queue-name: robovast`` so Kueue picks it up automatically.

If Kueue is not installed, jobs are still created but are *not* queued —
they start immediately, which can overload the cluster.


You can launch several ``vast execution cluster run`` campaigns at once; Kueue
keeps the cluster busy by admitting their jobs as capacity frees up.


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
``upload-to-share``, ``prepare-run``, ``run-cleanup``, and ``cleanup``.

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
  lost.  Upload results with ``vast execution cluster upload-to-share`` before
  modifying or restarting the pod.

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
share is required**. Pushing to an external share (Nextcloud, GCS, …) is an
optional, on-demand step run **in the service**: it compresses the finished
campaign (streaming from the storage bucket via ``pigz``) and uploads the
``{campaign_id}.tar.gz`` to the configured share. No data ever reaches the user's
machine, and no separate archiver pod is involved.

How it works
^^^^^^^^^^^^

``vast execution cluster upload-to-share`` is a **stateless service call**:

1. **Pre-flight** — the service verifies the share credentials work before
   compressing, so a misconfigured share fails fast.
2. **Compress + upload** — it streams the campaign (read from the object store)
   into a ``tar.gz`` and runs the share provider's upload.
3. **On success** it reports the share type.
4. **On failure** the campaign is untouched in the object store, so you just run
   the command **again** once the cause is fixed — nothing has to have stayed
   alive in the meantime (unlike the old controller-pod flow, which parked a pod
   to await a retry).

A call may also target a **different share** — set a new ``ROBOVAST_SHARE_TYPE``
(and its variables) in ``.env`` before ``upload-to-share`` to, say, redirect a
stuck gcs upload to sftp. Those settings are sent as overrides for the attempt and
pre-flight-checked before compressing. (A missing variable for the chosen type
fails loudly rather than silently reusing the service's destination.)

Configuration via ``.env``
^^^^^^^^^^^^^^^^^^^^^^^^^^

All credentials and share URLs are stored in a ``.env`` file in the project
directory (or any parent directory).  The file is **never** committed to the
``.vast`` project configuration, keeping secrets out of version control.

Load order:  ``python-dotenv`` searches for ``.env`` starting from the
current working directory and walks up to the root.

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

Retrying a failed upload:

.. code-block:: bash

   # Repeatable stateless call (uses the service's share settings)
   vast execution cluster upload-to-share

Progress output
^^^^^^^^^^^^^^^

Compression and upload run in the service; a single-line progress bar
(percentage, transferred size, rate) is written to the campaign log —
view it with ``vast execution cluster monitor`` or the web UI log panel:

.. code-block:: text

   campaign-2026-03-01-120000  [████████████░░░░░░░░]   60.0%  1.2 MiB/2.0 MiB  3.4 MiB/s
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

   # Required for upload (cluster upload-to-share) only.
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
