.. _cluster-execution:

Cluster Execution
=================

RoboVAST can execute test scenarios at scale on a **Kubernetes cluster**,
running each test configuration as an independent Job and collecting results
via a built-in MinIO S3 server.  This section covers everything from cluster
setup and job queueing to multi-context workflows and cloud-provider-specific
configuration.

Overview
--------

When a cluster run is triggered, RoboVAST performs the following steps
internally:

1. **Config upload** — All scenario configurations (entrypoints, scenario
   files, parameter files) are uploaded to a MinIO S3 bucket inside the
   cluster.
2. **Job creation** — For each test configuration × run number, a Kubernetes
   ``Job`` is created from a manifest template.  Each job runs an ``initContainer``
   that pulls its config files from S3 and a main ``robovast`` container that
   executes the scenario.
3. **Queueing (Kueue)** — Jobs are submitted to a dedicated Kueue
   ``LocalQueue`` (``robovast``).  Kueue's gang-scheduling and resource quotas
   ensure that jobs are admitted only when sufficient CPU/memory is available,
   preventing cluster oversubscription.
4. **Result collection** — After each job, the scenario container uploads
   result files back to the S3 bucket.  ``vast execution cluster download``
   streams the archives to a local results directory and removes the bucket.


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

* Deploys a ``robovast`` pod containing MinIO, an nginx HTTP server, and a
  Python/boto3 archiver sidecar.
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

   # Run only one specific config by name
   vast execution cluster run --config my-config


Monitoring and Results
----------------------

Check the status of a running (or recently completed) run:

.. code-block:: bash

   vast execution cluster monitor

Download results once jobs have finished:

.. code-block:: bash

   vast execution cluster download

Clean up only the job objects (without touching the result storage):

.. code-block:: bash

   vast execution cluster run-cleanup
   vast execution cluster run-cleanup --run-id run-2025-06-01-120000

Remove result archives from S3 without downloading:

.. code-block:: bash

   vast execution cluster download-cleanup


Manual Deployment (prepare-run)
---------------------------------

To generate all necessary manifests and scripts **without running them**
(e.g. for airgapped clusters or CI pipelines):

.. code-block:: bash

   vast execution cluster prepare-run ./output-dir

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
``download``, ``prepare-run``, ``run-cleanup``, and ``cleanup``.

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
  lost.  Download results before modifying the pod.

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

* No HTTP result server — the nginx sidecar and archiver are not included in
  the minikube manifest.  Use ``vast execution cluster download`` which uses
  kubectl port-forwarding to access MinIO directly.
* ``emptyDir`` storage means all data is lost if the pod restarts.


API Reference
-------------

The resolution logic for per-cluster resources lives in
:mod:`robovast.common.cluster_context`:

.. automodule:: robovast.common.cluster_context
   :members: get_active_kube_context, list_all_contexts, get_config_context_names,
             require_context_for_multi_cluster, resolve_resource_value, resolve_resources
   :undoc-members:


.. _cluster-sharing:

Sharing Results via ``cluster upload-to-share``
-------------------------------------------------

Instead of downloading cluster results to a local machine and then
re-uploading them to a shared folder (Nextcloud, Google Drive, …), the
``upload-to-share`` command performs the entire transfer **inside the
archiver sidecar of the robovast pod**.  No data ever reaches the user's
machine.

.. code-block:: bash

   vast execution cluster upload-to-share

How it works
^^^^^^^^^^^^

For each available run the command:

1. Creates a compressed ``{run_id}.tar.gz`` archive in ``/data/`` on the pod
   (reuses the same mechanism as ``cluster download``).  If the archive
   already exists it is reused.
2. Executes the share-provider upload script inside the archiver container,
   streaming upload progress back to the local terminal.
3. Removes the archive from the pod on success  (use ``--keep-archive`` to
   retain it, e.g. when you also want to download the results locally later).
4. Keeps the archive if the upload fails, so you can retry or fall back to
   a plain ``cluster download``.

Configuration via ``.env``
^^^^^^^^^^^^^^^^^^^^^^^^^^

All credentials and share URLs are stored in a ``.env`` file in the project
directory (or any parent directory).  The file is **never** committed to the
``.vast`` project configuration, keeping secrets out of version control.

Load order:  ``python-dotenv`` searches for ``.env`` starting from the
current working directory and walks up to the root.

**Required variables (for all share types):**

.. code-block:: ini

   ROBOVAST_SHARE_TYPE=<provider>   # nextcloud  or  gdrive

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
Only the standard Python library is used inside the pod — no additional
packages need to be installed.

Example usage:

.. code-block:: bash

   # Upload all available runs
   vast execution cluster upload-to-share

   # Keep the pod-side archive after upload (so you can also download it)
   vast execution cluster upload-to-share --keep-archive

   # Force recreation of the tar.gz even if it already exists
   vast execution cluster upload-to-share --force

Google Drive
^^^^^^^^^^^^

Uploads to a Google Drive folder using a **service account** with write access
to the target folder.  The folder **must** be located in a **Shared Drive** —
service accounts have no personal storage quota and cannot write to regular
"My Drive" folders.

Prerequisites:

1. Create a service account in Google Cloud Console and download its JSON key
   file.
2. Add the service account email address as a member of the Shared Drive
   (or share a specific folder within it) with at least "Contributor" role.

.. code-block:: ini

   ROBOVAST_SHARE_TYPE=gdrive

   # Full URL or just the folder ID from the address bar.
   # Example URL: https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv
   ROBOVAST_SHARE_URL=https://drive.google.com/drive/folders/<folder-id>

   # Absolute or project-relative path to the service account JSON key file.
   ROBOVAST_GDRIVE_SERVICE_ACCOUNT_JSON=/home/user/.secrets/my-project-sa.json

.. note::

   ``google-auth`` and ``google-api-python-client`` are pre-installed in the
   ``robovast-archiver`` image, so no extra setup is needed inside the pod.

Example usage:

.. code-block:: bash

   vast execution cluster upload-to-share
   vast execution cluster upload-to-share --keep-archive

Progress output
^^^^^^^^^^^^^^^

A single-line progress bar is printed for each run during upload, showing
the percentage, transferred size, and upload rate:

.. code-block:: text

   run-2026-03-01-120000  [████████████░░░░░░░░]   60.0%  1.2 MiB/2.0 MiB  3.4 MiB/s
   run-2026-03-01-120000  uploaded (2.0 MiB)  ✓

   ✓ Uploaded 3 run(s) to nextcloud successfully!

Adding a new share provider (plugin system)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Share providers are discovered as **entry-point plugins** under the
``robovast.share_providers`` group.  To add a new provider:

1. **Create a provider class** that inherits from
   :class:`~robovast.execution.cluster_execution.share_providers.base.BaseShareProvider`
   and implements the three abstract methods:

   .. code-block:: python

      from robovast.execution.cluster_execution.share_providers.base import (
          BaseShareProvider,
      )

      class MyShareProvider(BaseShareProvider):
          SHARE_TYPE = "myshare"

          def required_env_vars(self) -> dict[str, str]:
              return {
                  "ROBOVAST_SHARE_URL": "URL of the target folder",
                  "MY_SHARE_TOKEN":     "API token for the share service",
              }

          def get_upload_script_path(self) -> str:
              import os
              return os.path.join(os.path.dirname(__file__), "myshare_upload_script.py")

          def build_pod_env(self) -> dict[str, str]:
              import os
              return {
                  "MY_SHARE_URL":   os.environ["ROBOVAST_SHARE_URL"],
                  "MY_SHARE_TOKEN": os.environ["MY_SHARE_TOKEN"],
              }

2. **Create a pod-side upload script** (``myshare_upload_script.py``).  It
   runs inside the ``robovast-archiver`` image (``python:3.12-alpine`` +
   ``pigz``, ``boto3``, ``google-auth``, ``google-api-python-client``).  It
   receives the run ID as ``sys.argv[1]`` and finds the archive at
   ``/data/{run_id}.tar.gz``.  Env vars from ``build_pod_env()`` are
   available via ``os.environ``.

3. **Register the provider** in your package's ``pyproject.toml``:

   .. code-block:: toml

      [tool.poetry.plugins."robovast.share_providers"]
      myshare = "mypackage.myshare:MyShareProvider"

4. Re-install the package (``pip install -e .``) so the entry point is
   registered.

After that, ``ROBOVAST_SHARE_TYPE=myshare`` in ``.env`` will select your
provider automatically.

Share provider API reference
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. autoclass:: robovast.execution.cluster_execution.share_providers.base.BaseShareProvider
   :members:
   :undoc-members:

.. autoclass:: robovast.execution.cluster_execution.share_providers.nextcloud.NextcloudShareProvider
   :members:

.. autoclass:: robovast.execution.cluster_execution.share_providers.gdrive.GDriveShareProvider
   :members:

.. autoclass:: robovast.execution.cluster_execution.upload_to_share.ShareUploader
   :members:
