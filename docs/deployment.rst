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

``vast serve`` also exposes its MCP tools at ``/mcp`` on that same port by
default, so a single tunnel to ``8800`` reaches the web UI, the REST API, *and*
MCP together — pass ``--no-mcp`` to serve the API without them (see :ref:`mcp`).

* **Local-only, no service** — ``vast exec local run``. One-shot local Docker
  run, no persistent service (mode 1). CLI only.
* **Local service** — ``vast serve``. Persistent local service; web UI + CLI +
  MCP share its state (mode 2). ``vast ui`` opens it.
* **Cluster service** — ``vast exec cluster setup --ingress-host …`` deploys and
  publishes it (mode 3); users then reach it in a browser, or with ``vast login
  <url>`` for the CLI and MCP. **No kubeconfig, no kubectl, nothing to hold open.**
  In-pod, the Deployment runs ``vast serve --backend cluster``; run ``vast serve
  --backend cluster -x <context>`` off-cluster to debug the driver locally against a
  real cluster.
* **Your own tunnel to any of the above** — an ``ssh -N -L 8800:127.0.0.1:8800
  <host>`` or ``kubectl port-forward svc/robovast-service 8800:8800`` puts a service
  on the conventional port, and every client finds it there. That is the break-glass
  route when the Ingress itself is broken; it is kubectl's feature, not a mode
  RoboVAST wraps.

.. note::

   **Every request needs the shared token.** There is no unauthenticated mode: when
   ``ROBOVAST_AUTH_TOKEN`` is unset, ``vast serve`` mints one and prints a login URL
   carrying it, and ``vast exec cluster setup`` generates one and preserves it across
   re-runs (``--rotate-token`` issues a new one, logging everyone out).

   Browsers authenticate with a cookie obtained at ``/login``; the CLI and MCP send
   ``Authorization: Bearer``. The cookie is not a preference — ``EventSource`` cannot
   set headers, so it is what keeps the live streams in the web UI working.

   Publishing the service insists on TLS, and on a token being configured. Both
   refusals are deliberate: a campaign names its own container image, so an open
   Ingress lets anyone who finds the URL run containers in the cluster.

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
     - ``https://robovast.<domain>`` directly, over the Ingress that
       ``vast exec cluster setup --ingress-host`` created. A browser logs in at
       ``/login``; the CLI and MCP use ``vast login <url>``. Nothing is held open and
       no kubeconfig is involved.

That last row is the hardening this page used to defer — "a public **Ingress +
token/TLS**, decided once for the whole surface". It is decided: one shared secret,
presented as a cookie by browsers and a bearer header by everything else, in front of
an Ingress that refuses to exist without TLS.

Walkthrough — a remote VM service over an SSH tunnel
----------------------------------------------------

On the VM (bound to its own localhost):

.. code-block:: bash

   vast serve --host 127.0.0.1 --port 8800

On your machine, open the tunnel on the **conventional port** and every client
auto-detects it — nothing to export:

.. code-block:: bash

   ssh -N -L 8800:127.0.0.1:8800 user@vm &          # background tunnel to :8800

   # the CLI and the web UI follow the tunnel on :8800:
   vast exec cluster run                             # no flags — auto-detected
   # ...author, run, and query campaigns on the VM.

That one tunnel also reaches MCP, since ``vast serve`` mounts it at ``/mcp`` on
the same port by default: a client points at ``http://127.0.0.1:8800/mcp``
through the tunnel, with no second port to forward.

Walkthrough — the in-cluster service
------------------------------------

The operator, once, with a kubeconfig:

.. code-block:: bash

   python tools/setup_ingress_tls.py                # hostname + certificate
   vast exec cluster setup rke2 \
       --ingress-host robovast.example.org \
       --ingress-class nginx --issuer robovast-ca   # deploys and publishes it

Setup prints the access token once, and waits for the pod to be Ready before
reporting success — so an image that cannot be pulled is reported *here*, with the
pod's own reason, rather than as a connection failure from the next command.

To read it back later, ``vast exec cluster token`` prints the URL, the token and the
three ways to connect (``-q`` for the token alone). Each cluster mints its own token, so
the URL it names is the only one that token opens.

Everybody else, from a machine with no kubeconfig:

.. code-block:: bash

   # a browser: open https://robovast.example.org and log in — nothing to install

   pip install robovast-client                      # the CLI, 13 packages, ~30 MB
   vast login https://robovast.example.org

   claude mcp add --transport http robovast \
       https://robovast.example.org/mcp \
       --header "Authorization: Bearer <token>" \
       --header "X-Robovast-User: <name>"           # an agent: MCP over HTTP

``vast login`` prints that last command filled in with the token and the name it just
stored, so nobody has to assemble it by hand — and campaigns an agent starts carry the
same name as the ones its owner starts.

``robovast-client``, not ``robovast``: a user of a deployed service drives it over HTTP and
needs no simulator, no Docker and no Kubernetes client. The full distribution is 88
packages against the client's 13. See :ref:`client`.

``vast ui`` opens whichever service this machine talks to. When the Ingress itself is
broken, ``kubectl port-forward svc/robovast-service 8800:8800`` puts one back on the
conventional port and every client finds it there.

Keeping the service up to date
------------------------------

Controllers are launched per campaign, so execution always tracks the configured
controller image. The persistent service Deployment does not, so it has to be
updated deliberately.

.. code-block:: bash

   vast exec cluster upgrade

That rolls the Deployment onto the resolved image, reconciles RBAC, and waits for
the pod to be Ready before saying anything. **The access token is preserved**, so
nobody is logged out by a version bump.

Run it from the checkout whose ``.env`` describes this deployment: like every ``vast``
command it reads ``./.env`` from the current directory only. That ``.env`` is also how
the image is chosen — there is no ``--image`` flag, because a one-shot override is just
``ROBOVAST_CONTROLLER_IMAGE=repo@sha256:… vast exec cluster upgrade`` (a real environment
variable beats a ``.env`` line), and a pin that should last belongs in the file.

**It always restarts the pod**, even when nothing looks different, and that is
deliberate. An image ref that is a floating tag, or a change confined to the Secrets,
leaves the Deployment spec byte-identical; Kubernetes then creates no new ReplicaSet,
the readiness wait passes immediately against the *old* pod, and the command reports
success while nothing rolled. ``imagePullPolicy: Always`` does not rescue that — it
governs a container that is starting, and none was. The restart is also the only thing
that makes a changed Secret take effect: the pod reads them through ``envFrom`` once, at
container start. The cost is a few seconds of API downtime, during which open MCP
connections and log streams are dropped; campaigns run as their own Jobs and keep going.

RBAC reconciliation is not decoration. The ``/usage`` endpoint (cluster CPU/memory,
shown in the web UI top bar and by the ``resource_usage`` MCP tool) once needed a new
cluster-scoped ``ClusterRole`` over ``nodes``/``pods``; a service deployed before that
returned a permissions error until it was set up again. An upgrade that skipped RBAC
would reintroduce exactly that, as a runtime 403 that reads like a bug rather than a
missed migration.

The three lifecycle verbs are deliberately distinct:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Command
     - What it changes
   * - ``vast exec cluster upgrade``
     - The image, RBAC, and the credential Secrets it can rebuild from ``.env``
       (git, share, ntfy, registry) — which is how a registry move or a rotated
       password reaches the cluster. The **access token is preserved**, so nobody is
       logged out.
   * - ``vast exec cluster setup --force``
     - The same, plus it will re-mint the access token when asked
       (``--rotate-token``) — which does log everyone out, so it is not what you
       want for a version bump.
   * - ``vast exec cluster cleanup``
     - Removes the deployment entirely.

Campaign data lives in the object store and survives all three. Plain ``setup`` over
a live service is refused (``Cluster is already set up``).

Checking a deployment
---------------------

.. code-block:: bash

   vast doctor              # prerequisites, capacity, permissions
   vast doctor --flavor gcp # also what the gcp flavor needs
   vast doctor -x local     # a specific kubeconfig context

Reads only, so it is safe at any time — which makes it usable both as the first step
of an install and as the first step of debugging one. Every failure names its remedy.
It checks the Python version, ``kubectl``/``helm``/``gcloud``, that the kubeconfig
resolves and the API server answers, that the caller may create ClusterRoles (setup
does), and that one node is large enough for the Kueue controller's 4 CPU / 16 GiB —
a cluster with plenty of *total* capacity but no node big enough leaves that
controller Pending and admits no campaign at all.
