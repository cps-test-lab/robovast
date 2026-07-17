.. _deployment:

===========================
Deployment Modes and Access
===========================

The ``robovast-service`` core (see :ref:`architecture`) is deployment-agnostic:
the same FastAPI app runs in-process, on a single host, or in a cluster. This
page covers the three modes, how a client reaches each, and the v1 security
boundary.

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
     - ``kubectl port-forward svc/robovast-service 8800:8800`` then talk to
       ``127.0.0.1:8800``. ``vast mcp serve --cluster`` establishes and
       supervises the tunnel for you.

The deferred hardening for direct remote access (mode 2 remote / mode 3 without a
tunnel) is a public **Ingress + token/TLS**, decided once for the whole surface.

Walkthrough — a remote VM service over an SSH tunnel
----------------------------------------------------

On the VM (bound to its own localhost):

.. code-block:: bash

   vast serve --host 127.0.0.1 --port 8800

On your machine, open the tunnel and point clients at it:

.. code-block:: bash

   ssh -N -L 8800:127.0.0.1:8800 user@vm &          # background tunnel
   export ROBOVAST_SERVICE_URL=http://127.0.0.1:8800

   # the MCP server now drives the remote service:
   vast mcp serve
   # ...and the CLI/MCP tools author, run, and query campaigns on the VM.

Walkthrough — the in-cluster service
------------------------------------

.. code-block:: bash

   vast exec cluster setup rke2                     # deploys robovast-service
   kubectl port-forward svc/robovast-service 8800:8800 &
   export ROBOVAST_SERVICE_URL=http://127.0.0.1:8800

``vast mcp serve --cluster`` can own the port-forward instead, so an MCP user
(an LLM) never deals with tunnelling — it just calls tools. ``vast exec cluster
run`` uses the same client path.

Keeping the service up to date
------------------------------

Controllers are launched per campaign, so execution always tracks the configured
controller image. The persistent service Deployment is updated by re-running
``vast exec cluster setup`` (rolling restart). The client/service exchange a
version at ``/version`` so a stale service can be surfaced.
