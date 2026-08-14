.. _quickstart:

==========
Quickstart
==========

There are two roles, and only one of them touches Kubernetes.

.. list-table::
   :header-rows: 1
   :widths: 20 30 50

   * - Role
     - Needs
     - Does
   * - **Operator**
     - a kubeconfig, once
     - Deploys and publishes RoboVAST. Three commands.
   * - **User**
     - a URL and an access token
     - Opens it. No kubeconfig, no kubectl, no venv for the web UI.

If you are evaluating RoboVAST on your own machine, you are both — but you can stop
after :ref:`quickstart-local` and ignore the cluster entirely.


.. _quickstart-local:

On this machine
===============

.. code-block:: bash

   git clone https://github.com/cps-test-lab/robovast.git && cd robovast
   make venv && source venv/bin/activate
   vast serve

``vast serve`` prints a URL carrying a temporary access token:

.. code-block:: text

     RoboVAST: http://127.0.0.1:8800/login?token=a7f3…

Click it. That token is generated because none was configured, and it changes on every
restart — set ``ROBOVAST_AUTH_TOKEN`` in ``.env`` to fix it. There is no unauthenticated
mode: the service always requires the token, so a development instance and a deployed one
behave the same way.

The web UI, the REST API and the MCP server are all on that one port.


Operator: deploy it for a team
==============================

**1. Check the prerequisites.**

.. code-block:: bash

   vast doctor            # add --flavor gcp for a GKE cluster

Reads only, so it is safe at any time. It checks Python, ``kubectl``, ``helm``, the
kubeconfig, whether you may create ClusterRoles (setup does), and whether a node is large
enough for Kueue's controller. Every failure names its remedy.

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

   pip install robovast
   vast login https://robovast.example.org

It stores the URL, token and name in ``~/.config/robovast/config.json`` (mode ``0600``) and
verifies them before saving, so a typo fails here rather than as a 401 from some later
command. Every ``vast`` command then targets that service.

**As an MCP server**, for an agent:

.. code-block:: bash

   claude mcp add --transport http robovast \
       https://robovast.example.org/mcp \
       --header "Authorization: Bearer <token>"

Nothing to install: ``vast serve`` mounts MCP on the same port the UI is on. (A local
``vast mcp serve`` over stdio also works and picks up ``vast login``'s credentials.)


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
