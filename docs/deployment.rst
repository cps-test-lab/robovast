.. _deployment:

===========================
Deployment Modes and Access
===========================

The ``robovast-service`` core (see :ref:`architecture`) is deployment-agnostic:
the same FastAPI app runs in-process, on a single host, or in a cluster. This
page covers the three modes, how a client reaches each, and the v1 security
boundary.

One command owns reachability: **``vast serve`` is the foreground process that
makes a service answer on the conventional port ``127.0.0.1:8800``. While it is
running you can work with it — the web UI, the CLI, and the MCP server all
auto-detect it there, so none of them needs configuring.** ``vast ui`` is just a
shortcut that opens a browser at that port; it starts nothing.

* **Local-only, no service** — ``vast exec local run``. One-shot local Docker
  run, no persistent service (mode 1). CLI only.
* **Local service** — ``vast serve``. Persistent local service; web UI + CLI +
  MCP share its state (mode 2). ``vast ui`` opens it.
* **Cluster service** — ``vast exec cluster setup`` deploys it (mode 3); then
  ``vast serve --attach`` from your machine holds a tunnel to it, and while that
  runs the CLI, MCP, and ``vast ui`` all reach it. In-pod, the Deployment runs
  ``vast serve --backend cluster``; run ``vast serve --backend cluster -x
  <context>`` off-cluster to debug the driver locally against a real cluster.
* **Dual-lane dev service** — ``vast serve --backend local+cluster`` offers
  *both* a local Docker lane and a cluster lane in one service and chooses per
  campaign (``start_campaign`` ``backend``; default cluster). A dev-host mode
  (needs Docker **and** kubeconfig, off-cluster only): pilot a campaign locally
  and scale the same session to the cluster without re-pointing serve. The
  deployed in-cluster service stays single-backend.
* **Your own tunnel to any of the above** — an ``ssh -N -L 8800:127.0.0.1:8800
  <host>`` or ``kubectl port-forward … 8800:8800`` on the conventional port is
  equivalent to ``vast serve --attach``; the web UI and every ``vast`` command
  then follow it automatically.

.. warning::

   The service is **unauthenticated in v1**. It binds ``127.0.0.1`` by default
   and must stay behind a localhost / SSH-tunnel / ``kubectl port-forward``
   boundary. Do not expose it directly on a network until authenticated access
   (token + TLS / reverse-proxy / Ingress) is added.

The three modes
---------------

**Mode 1 — in-process** (``vast exec local run``)
    No service process; the CLI/MCP call the interface directly over
    ``LocalTransport``. Local Docker, local filesystem, sequential. Zero-config;
    the campaign dies with the client. This is the default for one-shot runs.

**Mode 2 — single-host service** (``vast serve``)
    The same FastAPI app running persistently with the Docker backend and the
    **local filesystem** as its durable home. Campaigns survive client exit, and
    the CLI, MCP server, and web UI share one workspace/campaign state. Runs on
    your machine or a **remote VM**.

    .. code-block:: bash

       vast serve --host 127.0.0.1 --port 8800   # OpenAPI at /docs

**Mode 3 — cluster service** (in-cluster Deployment)
    ``vast exec cluster setup`` deploys ``robovast-service`` as a Deployment +
    ClusterIP Service. It **drives each campaign in-process** (one worker thread
    per campaign) over the Kubernetes backend, creating the scenario Jobs itself,
    and stores results in the **object store**. There is no per-campaign controller
    pod. In-pod, ``vast serve`` auto-detects the cluster backend
    (``--backend auto`` → ``cluster`` when ``KUBERNETES_SERVICE_HOST`` is set).

Choosing a mode: mode 1 for a quick local run; mode 2 for a persistent local or
single-VM service (no Kubernetes); mode 3 for scaled, parallel execution.

Access matrix
-------------

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Mode
     - Transport
     - How the client reaches it
   * - In-process
     - ``LocalTransport``
     - Direct Python calls; no network.
   * - Local service
     - ``HTTPTransport``
     - ``http://127.0.0.1:8800`` directly.
   * - Remote VM
     - ``HTTPTransport``
     - **SSH tunnel** — ``ssh -L 8800:127.0.0.1:8800 <vm>`` — then talk to
       ``127.0.0.1:8800``. The service binds VM-localhost; SSH provides
       authentication and encryption. The VM analog of ``kubectl port-forward``.
   * - Cluster service
     - ``HTTPTransport``
     - ``vast serve --attach`` holds a ``kubectl port-forward`` to the deployed
       service on ``127.0.0.1:8800`` for as long as it runs (the equivalent of
       running ``kubectl port-forward svc/robovast-service 8800:8800`` yourself).
       While it is up, every client — including ``vast ui`` — follows it. Or
       skip the held tunnel and pass ``vast exec cluster … --cluster`` for an
       ephemeral per-call tunnel.

The deferred hardening for direct remote access (mode 2 remote / mode 3 without a
tunnel) is a public **Ingress + token/TLS**, decided once for the whole surface.

Walkthrough — a remote VM service over an SSH tunnel
----------------------------------------------------

On the VM (bound to its own localhost):

.. code-block:: bash

   vast serve --host 127.0.0.1 --port 8800

On your machine, open the tunnel on the **conventional port** and every client
auto-detects it — nothing to export:

.. code-block:: bash

   ssh -N -L 8800:127.0.0.1:8800 user@vm &          # background tunnel to :8800

   # the CLI, the web UI, and the MCP server all follow the tunnel on :8800:
   vast mcp serve                                    # drives the remote service
   vast exec cluster run                             # no flags — auto-detected
   # ...author, run, and query campaigns on the VM.

Walkthrough — the in-cluster service
------------------------------------

.. code-block:: bash

   vast exec cluster setup rke2                     # deploys robovast-service
   vast serve --attach                              # holds the tunnel on :8800

``vast serve --attach`` brings the service up on ``:8800`` and holds it there;
while it runs everything else auto-detects it — ``vast ui`` opens a browser at
it, an MCP user (an LLM) just calls tools with no URL to configure, and ``vast
exec cluster run`` (and ``workspace``/``monitor``) need no flags. Prefer your own
``kubectl port-forward svc/robovast-service 8800:8800`` and it is exactly
equivalent. To skip the held tunnel entirely, pass ``--cluster`` on a command to
open an ephemeral per-call tunnel instead.

Keeping the service up to date
------------------------------

Controllers are launched per campaign, so execution always tracks the configured
controller image. The persistent service Deployment is updated by re-running
``vast exec cluster setup`` (rolling restart). The client/service exchange a
version at ``/version`` so a stale service can be surfaced.

Re-running setup also reconciles the service's RBAC. In particular, the
``/usage`` endpoint (cluster CPU/memory capacity and usage, shown in the web UI
top bar and via the ``resource_usage`` MCP tool) needs a cluster-scoped
read-only ``ClusterRole`` over ``nodes``/``pods`` — so a service first deployed
by an older setup must be re-run through ``vast exec cluster setup`` to gain it,
otherwise ``/usage`` returns a permissions error.
