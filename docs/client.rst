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
   vast login robovast.example.org

The scheme is optional: a bare host is filled in as ``https``, or as ``http`` for
loopback, so the address as the operator hands it to you is the address you can type.

Three dependencies (``pydantic``, ``click``, ``requests``), 13 packages, about 30 MB. The
full ``robovast`` distribution is 88 packages and about 290 MB, because it can *execute*
campaigns itself — pandas and numpy alone are 152 MB of that, and none of it is needed to
push a project, launch a campaign or wait for one.

Nothing here is a reduced version of a command that exists elsewhere. Every verb the
client offers only *drives* a service, so a client install is a complete install rather
than a truncated one.


What you can do with it
=======================

Every group is named after what it acts on, so the group tells you what you are touching.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Command
     - Does
   * - ``vast login <url>`` / ``vast logout``
     - Store or forget the service credentials, verified before saving.
   * - ``vast workspace init|update|download|list|delete``
     - Move a directory into a service workspace, and back out again.
   * - ``vast workspace validate|preview``
     - Check a project, and see what its sweep expands to — both before spending compute.
   * - ``vast workspace run <ws> [vast]``
     - **Launch a campaign.** The one way to run a ``.vast``. ``--push DIR`` pushes and
       launches in one step; ``--wait-and-download`` blocks and pulls the results down.
   * - ``vast campaign list|status``
     - What has run, and where one campaign has got to.
   * - ``vast campaign wait <id>``
     - Block until a campaign is genuinely over. The exit code is the answer.
   * - ``vast campaign stop|stop-job|log``
     - Stop a campaign, kill one wedged job, read its infrastructure log.
   * - ``vast campaign rerun <id>``
     - Launch a new campaign from what a past one recorded. ``--check`` reports whether it
       can be, and costs nothing.
   * - ``vast campaign download <id>``
     - Pull a campaign's archive down as a ``.tar.gz``.
   * - ``vast service info|resources``
     - Which service is answering and which code it runs; whether the lane has room.
   * - ``vast service log``
     - What the *service itself* has been doing. ``-f`` follows.
   * - ``vast service restart``
     - Roll the deployed service onto the newest image at its tag, through its own API —
       no kubeconfig needed. Reconciles nothing else; see :doc:`deployment`.
   * - ``vast cluster store-cleanup``
     - Remove result buckets from the service's object store.
   * - ``vast container exec|stop``
     - Run a command in the experiment image, to test a container before a campaign does.
   * - ``vast files ls|cat|get|put|rm``
     - Read and write single files by address.
   * - ``vast image build|wait|status|log``
     - Have the service build the derived images a project's containers declare.
   * - ``vast doctor``
     - Check the login, the service, and that ``vast`` is on your PATH.

That is the whole loop — validate, preview, launch, wait, fetch — and none of it needs the
core. What the core adds is *local analysis*: ``vast config`` reads and expands a ``.vast``
on your disk, ``vast results`` computes over a results tree. Neither drives a service, which
is why neither is here.

An **agent** reaches the same service through its MCP endpoint, which needs nothing
installed at all — see :ref:`mcp`. ``vast login`` prints the ``claude mcp add`` line that
registers it. The two are not alternatives with a gap between them: the control verbs are
deliberately on both sides, and each side additionally owns what only it can do — bulk bytes
and long waits here, results queries and diff-based authoring there.


.. _client-partial-surface:

What is absent, and what is only partly here
============================================

**Absent:** ``vast serve``, ``vast config``, ``vast results``, ``vast ui``. They are not
hidden or disabled — the distribution does not register them, so ``vast --help`` on a
client install lists exactly what it can run. That is the point of installing it alone.

**Partly here:** ``vast cluster`` and ``vast service``. The rule is the same one, applied a
level down — a subcommand exists exactly when something that can perform it is installed:

* ``vast cluster store-cleanup`` and ``vast service log|info|resources|restart`` ship with
  the client. Every one of them only *drives* a service, so a client install runs them
  completely.
* ``vast cluster setup|cleanup|jobs-cleanup|monitor`` and ``vast service upgrade|token``
  arrive with ``robovast-cluster``. They need a kubeconfig, an API server or a cluster
  Secret.

So ``vast cluster --help`` on a client install lists ``store-cleanup`` and not ``setup``.
Nothing is stubbed and nothing fails on use.

Both groups spanning two distributions is the design, not an accident: a group is named
after the **object** it acts on, not after what you installed. ``vast service restart`` and
``vast service upgrade`` both act on the deployed service; one needs a URL and a token, the
other an API server. Grouping them by what they touch keeps the name honest and lets
``--help`` differ per install.

``monitor`` is the one worth a word. It is a live, job-level dashboard over *every*
campaign, and its fallback view reads the Jobs from your kubeconfig — which is what keeps it
on the operator's side. A client install watches a campaign with ``vast campaign wait`` (one
campaign, phase by phase, with an exit code), asks ``vast campaign list|status``, and has
the web UI; between them they show everything monitor's service view did.


Running a campaign
==================

Three words are worth keeping apart, because the commands do:

**workspace**
   a directory on the service holding files and possibly several ``.vast`` files.

**project**
   one ``.vast`` and the files it references. A workspace holds as many as you like.

**campaign**
   an instance created from a ``.vast``.

So a campaign runs a *workspace's project*, and the pair (workspace, path) names it. That
is the only project binding the service accepts, and ``vast workspace run`` takes exactly
that pair — as does the MCP tool, argument for argument.

A service cannot read your disk, so getting the files to it is a **shell** step rather than
something an agent tool can do:

.. code-block:: bash

   cd my-experiment
   vast workspace init . --name my-experiment     # first time
   vast workspace update my-experiment .          # after edits

Prefer this over writing files one at a time through the API: the bytes never enter an
agent's context, and one command keeps the workspace and your directory in agreement.

Files under a name starting with ``.`` are skipped, as is ``results/`` — the same rule the
service applies when it lists a workspace, so a push and a listing cannot disagree.

Then check it, and run it:

.. code-block:: bash

   vast workspace validate my-experiment my.vast   # every problem at once
   vast workspace preview  my-experiment my.vast   # how many configurations is that?
   vast workspace run my-experiment my.vast --description "pilot: new inflation radius"

Omit the path when the workspace holds exactly one ``.vast`` and the service will resolve
it, naming the candidates if there are several. ``--push DIR`` does the push and the launch
in one command, creating the workspace if the name is free — the two-step form above is the
same thing spelled out.

**Set** ``--description``. It is one line saying what the run is *for*, and it is what tells
two same-day ``<name>-<timestamp>`` campaigns apart in ``vast campaign list`` and the web UI.

Nothing here needs a project file or a current directory. There is no ambient project at
all: every command names its own input, so nothing in a parent directory of your CWD decides
which ``.vast`` runs.


Waiting for a campaign
======================

A campaign can run for days, so nothing blocks a request on one. ``vast campaign wait`` polls the
service and exits when the campaign is genuinely finished — past postprocessing, not
merely past its last run:

.. code-block:: bash

   vast campaign wait basic-nav-2026-08-16-101500

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
interpreter. A capability you have not installed is reported once, as advisory — you get
``cluster support: not installed``, and not the ``kubectl`` and ``helm`` that only
``vast cluster setup`` shells out to, because there is no ``setup`` here to run them.
A client install lacking them is not a broken one, so they cannot fail the command.

When the failure is the *service's* and not yours, its own log is now readable:

.. code-block:: bash

   vast service log -f

Not a campaign's log — this is the service process, and several failures in RoboVAST are
diagnosable only from it (a build whose reason "lived only in the service log", a scene
cache retrying forever). It reads whichever service the CLI resolves — a local
``vast serve`` on the conventional port, else the ``vast login`` record — and prints which
one answered, so it is never ambiguous which service you are reading.

It covers the last few hundred kilobytes that process logged, kept in memory: enough for
what it is doing now, not its whole life, and cleared by a restart. A container that has
already died is only in ``kubectl logs -p deploy/robovast-service`` — a buffer inside a
process cannot outlive the process.

``vast login`` symlinks ``vast`` into a directory already on your login shell's PATH, so a
shell that activated no virtualenv — a new terminal, or an agent's — can run it. That is
what makes ``vast campaign wait`` reachable from an agent harness. Pass ``--no-link`` to manage
PATH yourself; ``vast doctor`` will tell you if the link later goes stale.


Where to go next
================

* :ref:`quickstart` — the three ways of working, and which one you are in.
* :ref:`mcp` — driving the same service from an agent, with no install at all.
* :ref:`setup` — installing the full product, to run campaigns yourself.
