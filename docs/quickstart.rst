.. _quickstart:

==========
Quickstart
==========

There are three ways to work with RoboVAST, and only one of them touches Kubernetes.
Find yours before installing anything — they differ in what you install, not just in what
you do.

.. list-table::
   :header-rows: 1
   :widths: 22 24 54

   * - Way of working
     - Installs
     - For
   * - **User** — a client against a service
     - ``robovast-client``
     - Driving a service someone else runs, or your own. Push a project, launch a
       campaign, wait for it, fetch results. No simulator, no Docker, no kubeconfig.
       See :ref:`client`.
   * - **Agent** — MCP, no install
     - nothing
     - An LLM authoring, validating, launching and querying campaigns over the service's
       own ``/mcp`` endpoint. Three steps still need a shell, and the client provides
       them. See :ref:`mcp`.
   * - **Operator / developer**
     - the whole product
     - Running a service — locally over Docker, or deployed on a cluster for a team — and
       changing RoboVAST itself. The only role that needs a kubeconfig, and only for the
       cluster half. See :ref:`setup`.

The service is the same in every case, and so is the contract: the web UI, the REST API,
the ``vast`` CLI and the MCP tools are four clients of one interface. What changes is
which of them you use and what you had to install to get there.

If you are evaluating RoboVAST on your own machine, you are all three — but you can stop
after :ref:`quickstart-local` and ignore the cluster entirely.


.. _quickstart-local:

On this machine
===============

.. code-block:: bash

   git clone https://github.com/cps-test-lab/robovast.git && cd robovast
   make venv && source venv/bin/activate
   vast serve

``vast serve`` prints what each of its two clients needs:

.. code-block:: text

     RoboVAST: http://127.0.0.1:8800/login?token=a7f3…

     For an agent:
       claude mcp add --transport http robovast http://127.0.0.1:8800/mcp \
         --header 'Authorization: Bearer a7f3…'

     (no ROBOVAST_AUTH_TOKEN configured, so this token is temporary and changes on restart;
      set it in .env to keep a browser login and an agent registration working across restarts)

Click the first; paste the second. There is no unauthenticated mode: the service always
requires the token, so a development instance and a deployed one behave the same way.

Heed the last line if you are using an agent. A registration carrying a temporary token
authenticates nothing after the next restart, and the failure looks like the service being
down rather than the token having changed. Setting ``ROBOVAST_AUTH_TOKEN`` in ``.env``
makes both the login and the registration durable.

The web UI, the REST API and the MCP server are all on that one port.

**A local service needs no hostname, no domain and no certificate.** It binds
``127.0.0.1:8800`` over plain HTTP, and every part of RoboVAST that would demand
otherwise is confined to the cluster half: the session cookie sets ``Secure`` only when
the request arrived over HTTPS, so a browser login works on ``http://127.0.0.1``, and the
refusals that reject an unencrypted or tokenless deployment live in ``vast exec cluster
setup``, which a local service never runs. There is nothing to register, nothing to put in
``/etc/hosts``, and no certificate to trust — a DNS name and TLS become necessary only
when you publish the service to other machines, which is :ref:`the operator's step
<quickstart-operator>`.

Bind somewhere other than ``127.0.0.1`` only behind a tunnel or a TLS-terminating proxy.
The service always requires its access token, and over plain HTTP on a shared interface
that token crosses the network in clear text.


.. _quickstart-operator:

Operator: deploy it for a team
==============================

**1. Check the prerequisites.**

.. code-block:: bash

   vast doctor            # add --flavor gcp for a GKE cluster

Reads only, so it is safe at any time. It checks Python, ``kubectl``, ``helm``, the
kubeconfig, whether you may create ClusterRoles (setup does), and whether the nodes report
any capacity at all. Every failure names its remedy.

**2. Pick a hostname and a certificate.**

.. code-block:: bash

   python tools/setup_ingress_tls.py

It detects the IngressClass, the node addresses and whether cert-manager is installed, then
asks only what it cannot work out. The hostname defaults to
``robovast.<node-ip>.sslip.io`` — a real name needing no DNS administration, which is
usually right on a LAN.

For the certificate, prefer **Let's Encrypt DNS-01** if you control a domain: it is
publicly trusted, so there is nothing for users to install, and Let's Encrypt validates by
reading a DNS record — it never connects to your host, which may keep a private address. A
**self-signed CA** works fully offline, but has to be trusted *per browser, not per
machine*: macOS Keychain and the Windows store do not cover Firefox, and on Linux neither
Firefox nor Chrome reads the system store. With a handful of people on mixed machines that
is a recurring support cost.

**3. Deploy and publish.**

.. code-block:: bash

   vast exec cluster setup rke2 \
       --ingress-host robovast.example.org \
       --ingress-class nginx --issuer robovast-ca

Setup prints the access token **once**, and waits for the pod to be Ready before reporting
success — so an image that cannot be pulled is reported here, with the pod's own reason,
rather than as a puzzling connection failure from the next command.

Two configurations are refused rather than warned about: an Ingress with no access token
(it would publish an unauthenticated RoboVAST, and a campaign names its own container
image, so reaching the URL is enough to run containers in your cluster), and an Ingress
over plain HTTP (the token would cross the network in clear text, and the session cookie
is ``Secure``, so the login would not work at all).

Then hand out the URL and the token. ``vast exec cluster token`` prints both, together
with the three ways to connect, so onboarding someone is one copy-paste:

.. code-block:: bash

   vast exec cluster token          # URL + token + how to connect
   vast exec cluster token -q       # the token alone, for a script

The token is **per cluster** — each instance mints its own. One instance's token at
another's URL fails with "That token was not accepted", which looks exactly like a typo,
which is why the command prints the URL the token belongs to next to it.

``tools/setup_ingress_tls.py`` writes the same kind of block to ``robovast-users.txt``
when it sets up the certificate.


User: get access
================

**In a browser** — open ``https://robovast.example.org``, enter the token, and optionally
your name. The name is shown on campaigns you start; it is self-declared, since a shared
secret cannot prove who anyone is. Leave it blank and your campaigns are recorded as
unattributed rather than as somebody invented.

**On the command line:**

.. code-block:: bash

   pip install robovast-client
   vast login https://robovast.example.org

It stores the URL, token and name in ``~/.config/robovast/config.json`` (mode ``0600``) and
verifies them before saving, so a typo fails here rather than as a 401 from some later
command. Every ``vast`` command then targets that service. It also prints the ready-made
registration below, with your token and name already in it.

It then symlinks ``vast`` into a directory already on your login shell's PATH, so a shell
that activated no venv — a new terminal, or an agent's — can run it. That is what makes
the wait command in :ref:`mcp` reachable; pass ``--no-link`` to manage PATH yourself.
``vast doctor`` reports whether it resolves, and says how to fix it if not.

**As an MCP server**, for an agent:

.. code-block:: bash

   claude mcp add --transport http robovast \
       https://robovast.example.org/mcp \
       --header "Authorization: Bearer <token>" \
       --header "X-Robovast-User: <your name>"

The name header is the HTTP equivalent of the one ``vast login`` stores: an agent reads no
config file, so without it every campaign the agent starts is unattributed while your own
CLI runs are labeled. Drop the line to stay unattributed on purpose.

The service mounts MCP on the same port the web UI is on, so this is the whole setup —
there is no separate MCP process to run, here or on the deployed instance.


Keeping it running
==================

.. code-block:: bash

   vast exec cluster upgrade          # new version: image + RBAC, nobody logged out
   vast exec cluster setup --force    # rotate credentials from .env (logs everyone out)
   vast exec cluster cleanup          # remove it; campaign data survives in the object store

If the Ingress itself breaks, ``kubectl port-forward svc/robovast-service 8800:8800`` puts
the service back on the conventional local port and every client finds it there.

See :doc:`deployment` for the modes and the access model, :doc:`cluster_execution` for the
cluster flavors and their options, and ``.env.example`` for every configuration variable.
