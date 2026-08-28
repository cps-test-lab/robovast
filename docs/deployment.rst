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

* **Local service** — ``vast serve``. Persistent local service on its Docker lane; web UI
  + CLI + MCP share its state (mode 1). ``vast ui`` opens it, and ``vast workspace run``
  launches through it exactly as it would through a remote one.
* **Cluster service** — ``vast cluster setup --ingress-host …`` deploys and
  publishes it (mode 2); users then reach it in a browser, or with ``vast login
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
   carrying it, and ``vast cluster setup`` generates one and preserves it across
   re-runs (``--rotate-token`` issues a new one, logging everyone out).

   Browsers authenticate with a cookie obtained at ``/login``; the CLI and MCP send
   ``Authorization: Bearer``. The cookie is not a preference — ``EventSource`` cannot
   set headers, so it is what keeps the live streams in the web UI working.

   Publishing the service insists on TLS, and on a token being configured. Both
   refusals are deliberate: a campaign names its own container image, so an open
   Ingress lets anyone who finds the URL run containers in the cluster.

The three modes
---------------

**Mode 1 — single-host service** (``vast serve``)
    The same FastAPI app running persistently with the Docker backend and the
    **local filesystem** as its durable home. Campaigns survive client exit, and
    the CLI, MCP server, and web UI share one workspace/campaign state. Runs on
    your machine or a **remote VM**.

    .. code-block:: bash

       vast serve --host 127.0.0.1 --port 8800   # OpenAPI at /docs

**Mode 2 — cluster service** (in-cluster Deployment)
    ``vast cluster setup`` deploys ``robovast-service`` as a Deployment +
    ClusterIP Service. It **drives each campaign in-process** (one worker thread
    per campaign) over the Kubernetes backend, creating the scenario Jobs itself,
    and stores results in the **object store**. There is no per-campaign controller
    pod. In-pod, ``vast serve`` auto-detects the cluster backend
    (``--backend auto`` → ``cluster`` when ``KUBERNETES_SERVICE_HOST`` is set).

Choosing a mode: mode 1 for a local or single-VM service with no Kubernetes; mode 2
for scaled, parallel execution.

There is no serviceless mode. There used to be a third, in-process one -- the CLI calling
the interface directly, with no service, no workspace and no ``CampaignOrigin`` -- and it is
gone: a campaign runs a *workspace's* project through a service, and a local service on its
Docker lane is the same path as a remote one, differing only in which service answers.

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
       ``vast cluster setup --ingress-host`` created. A browser logs in at
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
   vast workspace run                             # no flags — auto-detected
   # ...author, run, and query campaigns on the VM.

That one tunnel also reaches MCP, since ``vast serve`` mounts it at ``/mcp`` on
the same port by default: a client points at ``http://127.0.0.1:8800/mcp``
through the tunnel, with no second port to forward.

Walkthrough — the in-cluster service
------------------------------------

The operator, once, with a kubeconfig:

.. code-block:: bash

   python tools/setup_ingress_tls.py                # hostname + certificate
   vast cluster setup rke2 \
       --ingress-host robovast.example.org \
       --ingress-class nginx --issuer robovast-ca   # deploys and publishes it

Setup prints the access token once, and waits for the pod to be Ready before
reporting success — so an image that cannot be pulled is reported *here*, with the
pod's own reason, rather than as a connection failure from the next command.

To read it back later, ``vast service token`` prints the URL, the token and the
three ways to connect (``-q`` for the token alone). Each cluster mints its own token, so
the URL it names is the only one that token opens.

Setup also stamps the **timezone of the host it ran on** into the service pod's ``TZ``,
read from ``/etc/localtime``'s symlink target (or ``/etc/timezone``) and validated against
that host's tz database first. Campaign ids are minted from wall-clock time in the process
that names the campaign — the service pod, for a cluster campaign — so without this every
campaign directory is named in UTC while the people reading those names are not. An
``upgrade`` re-stamps it from the machine running the upgrade; a host whose zone cannot be
determined logs a warning and leaves the pod in UTC, which is the pre-existing behaviour.
Only this pod is affected: campaign Jobs get an env list built explicitly for them and
inherit nothing from it, so their logs stay UTC, as do recorded timestamps everywhere
(``store`` keeps epoch seconds). A mode-1/2 ``vast serve`` already runs in its host's zone
and needs none of this.

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

   vast service upgrade

That rolls the Deployment onto the resolved image, reconciles RBAC, and waits for
the pod to be Ready before saying anything. **The access token is preserved**, so
nobody is logged out by a version bump.

It reads the environment like every ``vast`` command — ``./.env`` from the current
directory, then ``~/.config/robovast/env`` — and that is also how the image is chosen:
``ROBOVAST_PROJECT`` and ``ROBOVAST_PROJECT_TAG`` (:doc:`images`). There is no ``--image``
flag, because a one-shot override is just
``ROBOVAST_PROJECT=ghcr.io/cps-test-lab vast service upgrade`` (a real environment variable
beats both files), and a setting that should last belongs in a file.

**This is the command that moves a cluster's images**, not ``setup --force``. Both values
are baked into the service pod as the *site default*; a single campaign can still override
them per launch with ``vast workspace run --image-project``, which needs no upgrade.

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

Before rolling, it asks the service which campaigns are live and names them, because the
pod being replaced is where their controller runs — the same reason ``--no-restart``
exists. ``--yes`` skips the question; without it a non-interactive run aborts rather than
rolling silently. A service that cannot be reached is reported and the roll proceeds, since
a wedged service is a reason to upgrade rather than a reason to refuse, but it says so —
a silent roll must never be read as "nothing was running".

There is a smaller verb for the common case, and it needs no kubeconfig:

.. code-block:: bash

   vast service restart

That asks the service to roll *itself* — the Deployment's restart annotation, and nothing
else. It is the same thing the web UI's Admin page button does (:ref:`web-ui-admin`), and
it exists because ``upgrade`` needs cluster access, so somebody who reached the deployment
through ``vast login`` had a button in the browser and no command at all. It carries the
same live-campaign guard and the same ``--yes``.

**It reconciles nothing.** RBAC, the registry ingress route, the
credential Secrets and the build daemon are all untouched, so a version needing a new
permission will deploy and then 403 at runtime. Use it for "new bytes are published and
nothing else changed"; use ``upgrade`` for a version bump, a missed RBAC migration, a
rotated Secret, or a registry move. The Secrets cannot be done any other way: they are
rebuilt from the operator's environment, which the pod does not have.

The lifecycle verbs are deliberately distinct:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Command
     - What it changes
   * - ``vast service upgrade``
     - The image, RBAC, and the credential Secrets it can rebuild from ``.env``
       (git, share, ntfy, registry) — which is how a registry move or a rotated
       password reaches the cluster. The **access token is preserved**, so nobody is
       logged out.
   * - ``vast cluster setup --force``
     - The same, plus it will re-mint the access token when asked
       (``--rotate-token``) — which does log everyone out, so it is not what you
       want for a version bump.
   * - ``vast service restart``
     - The image only, through the service's own API — no kubeconfig needed. Reconciles
       nothing else, so it is for "new bytes are published" and not for a migration.
   * - ``vast cluster cleanup``
     - Removes the deployment entirely.

Campaign data lives in the object store and survives all three. Plain ``setup`` over
a live service is refused (``Cluster is already set up``).

Checking a deployment
---------------------

.. code-block:: bash

   vast doctor              # prerequisites, capacity, permissions
   vast doctor --flavor gcp # also what the gcp flavor needs
   vast doctor -x local     # a specific kubeconfig context
   vast doctor -n robovast  # a deployment in another namespace

Reads only, so it is safe at any time — which makes it usable both as the first step
of an install and as the first step of debugging one. Every failure names its remedy.
It checks the Python version, ``kubectl``/``helm``/``gcloud``, that the kubeconfig
resolves and the API server answers, that the caller may create ClusterRoles (setup
does), and that the nodes report allocatable capacity at all. It reports the largest
node rather than judging against a threshold: a campaign's pod is whatever its ``.vast``
asks for, and a request no node can hold is refused when the campaign launches, naming
both the request and each node's allocatable.

**It also checks whether the deployment can build experiment images**, which is the one
prerequisite that used to surface only when a campaign was submitted and refused:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Check
     - Says
   * - ``build registry``
     - Whether a push target is configured. A published service with no prefix wants
       ``vast service upgrade``; one that was never published wants ``setup`` with
       ``--ingress-host``. Reading the Ingress is what lets it name *which* — the in-pod
       service cannot tell those apart and has to offer both.
   * - ``registry route``
     - Whether that target is actually reachable: the ``/v2`` rule **and** the upload-size
       annotation. Both matter and fail differently — without the annotation every layer
       push dies on nginx's 1 MiB default with a 413, while ``GET /v2/`` answers 200.

Both are advisory: a deployment that cannot build is not a broken one. The namespace
matters, so pass ``-n`` when the service is not in ``default``.

From a machine with **no kubeconfig**, ``vast doctor`` still reports an ``image builds``
line — read from the service's own handshake rather than the cluster. It is absent when the
service is older than that field: absent is "did not say", not "no".
