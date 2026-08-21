.. _client:

=================
Using the client
=================

RoboVAST runs simulation campaigns: a scenario, a sweep of configurations, repeated runs,
recorded provenance. That work happens **on a service** — on a Docker host or a Kubernetes
cluster. If you are the person *driving* it rather than the person *hosting* it, the client
is all you need to install.

.. code-block:: bash

   pip install robovast-client
   vast login https://robovast.example.org

Three dependencies (``pydantic``, ``click``, ``requests``), 13 packages, about 30 MB. The
full ``robovast`` distribution is 88 packages and about 290 MB, because it can *execute*
campaigns itself — pandas and numpy alone are 152 MB of that, and none of it is needed to
push a project, launch a campaign or wait for one.

Nothing here is a reduced version of a command that exists elsewhere. Every verb the
client offers only *drives* a service, so a client install is a complete install rather
than a truncated one.


What you can do with it
=======================

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Command
     - Does
   * - ``vast login <url>`` / ``vast logout``
     - Store or forget the service credentials, verified before saving.
   * - ``vast exec cluster run``
     - **Launch a campaign.** Pushes the project and starts it on the service's lane.
       Add ``--wait-and-download`` to block until it finishes and pull the results down.
   * - ``vast exec cluster stop|stop-job|log``
     - Stop a campaign, kill one wedged job, read a campaign's infrastructure log.
   * - ``vast workspace init|update|list|delete``
     - Push a project directory to the service; re-sync it after edits.
   * - ``vast files ls|cat|get|put|rm``
     - Read and write single files by address.
   * - ``vast image build|wait|status|log``
     - Have the service build the derived images a project's containers declare.
   * - ``vast wait <campaign-id>``
     - Block until a campaign is genuinely over.
   * - ``vast image wait <build-id>…``
     - Block until every named image build is done. A build whose pod cannot start (image
       pull, capacity) fails within a minute rather than being waited out.
   * - ``vast exec cluster download-cleanup``
     - Remove result buckets from the service's object store.
   * - ``vast doctor``
     - Check the login, the service, and that ``vast`` is on your PATH.

Authoring, validating and querying results are available to an **agent** through the
service's MCP endpoint, which needs nothing installed at all — see :ref:`mcp`.
``vast login`` prints the ``claude mcp add`` line that registers it.

.. _client-partial-surface:

What is absent, and what is only partly here
============================================

**Absent:** ``vast serve``, ``vast init``, ``vast config``, ``vast results``. They are not
hidden or disabled — the distribution does not register them, so ``vast --help`` on a
client install lists exactly what it can run. That is the point of installing it alone.

**Partly here:** ``vast exec``. The rule is the same one, applied a level down — a
subcommand exists exactly when something that can perform it is installed:

* ``vast exec cluster run|stop|stop-job|log|download-cleanup`` ship with the client. Every
  one of them only *drives* a service, so a client install runs them completely.
* ``vast exec cluster setup|cleanup|upgrade|token|run-cleanup|monitor`` arrive with
  ``robovast-cluster``. They need a kubeconfig, an API server or a cluster Secret.
* ``vast exec local`` arrives with ``robovast``. It needs Docker.

So ``vast exec --help`` on a client install lists ``cluster`` and not ``local``, and
``vast exec cluster --help`` lists ``run`` and not ``setup``. Nothing is stubbed and
nothing fails on use.

``monitor`` is the one worth a word, because ``run`` used to point at it. It is a live,
job-level dashboard over *every* campaign, and its fallback view reads the Jobs from your
kubeconfig — which is what keeps it on the operator's side. A client install watches a
campaign with ``vast wait`` (one campaign, phase by phase, with an exit code) and the web
UI, which between them show everything monitor's service view did.

.. note::

   ``vast init`` is a core verb, so on a client-only install the way to name a project is
   the global ``-V`` flag::

      vast -V my-experiment/my.vast exec cluster run --description "pilot"

   Every command that needs a ``.vast`` accepts it, and it needs no ``.robovast_project``.


Pushing a project
=================

A service cannot read your disk, so getting a project to it is a **shell** step rather
than something an agent tool can do:

.. code-block:: bash

   cd my-experiment
   vast workspace init . --name my-experiment     # first time
   vast workspace update my-experiment .          # after edits

Prefer this over writing files one at a time through the API: the bytes never enter an
agent's context, and one command keeps the workspace and your directory in agreement.

Files under a name starting with ``.`` are skipped, as is ``results/`` — the same rule the
service applies when it lists a workspace, so a push and a listing cannot disagree.


Waiting for a campaign
======================

A campaign can run for days, so nothing blocks a request on one. ``vast wait`` polls the
service and exits when the campaign is genuinely finished — past postprocessing, not
merely past its last run:

.. code-block:: bash

   vast wait basic-nav-2026-08-16-101500

**The exit code is the answer:**

.. list-table::
   :header-rows: 1
   :widths: 12 88

   * - Code
     - Means
   * - ``0``
     - Finished.
   * - ``1``
     - Failed, or stopped.
   * - ``2``
     - ``--timeout`` elapsed. The campaign is unaffected and can be waited on again.
   * - ``3``
     - No such campaign — the service knows no phase for that id. A typo, or a campaign
       that died before recording one. Distinct from ``1`` on purpose: those send you
       looking for different things.
   * - ``4``
     - **Stalled**: nothing has completed for longer than one run may take. The campaign is
       *still running* and nothing is waiting on it now — the message says so, and names
       both ways out (re-run this command, or ``stop_campaign``).
   * - ``5``
     - A running job's **simulator** reported something wrong about itself
       (:ref:`mcp-health-findings`). Same shape as ``4``: the campaign is still running, the
       run was **not** touched, and nothing is waiting on it.

Codes ``4`` and ``5`` are why this command exists in the form it does. A stalled or wedged
campaign never reaches a terminal phase — it holds ``running`` for its whole life — so a
waiter that stopped only on a terminal phase never returned, and nobody was ever told. Only a
**new** stall or finding ends the wait: whatever was already true when this command started
is what the caller was just told about, and exiting on it would make "re-run this after
diagnosing" an infinite loop rather than the way back.

Run it as the **whole** command, unwrapped and unchained. Anything appended makes the
shell report the wrapper's status instead, which turns a failed campaign into a reported
success — a real incident, not a hypothetical one.


When something is wrong
=======================

.. code-block:: bash

   vast doctor

On a client install it checks what a client has: a stored login, that the service answers,
that ``vast`` resolves in a fresh login shell, and that the symlink points at a live
interpreter. Checks belonging to capabilities you have not installed are reported as
*not installed* rather than as failures — a client install lacking ``kubectl`` is not a
broken one.

``vast login`` symlinks ``vast`` into a directory already on your login shell's PATH, so a
shell that activated no virtualenv — a new terminal, or an agent's — can run it. That is
what makes ``vast wait`` reachable from an agent harness. Pass ``--no-link`` to manage
PATH yourself; ``vast doctor`` will tell you if the link later goes stale.


Where to go next
================

* :ref:`quickstart` — the three ways of working, and which one you are in.
* :ref:`mcp` — driving the same service from an agent, with no install at all.
* :ref:`setup` — installing the full product, to run campaigns yourself.
