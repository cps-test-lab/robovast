Container images
================

RoboVAST publishes four container images, and a campaign never names one of them. Which
image a container needs follows from its *role* and the campaign's mode, so the only thing
left to configure is **where** the images come from — one variable.

.. code-block:: bash

   ROBOVAST_PROJECT=ghcr.io/cps-test-lab        # the registry/namespace serving the family
   ROBOVAST_PROJECT_TAG=2026-08-17    # optional; defaults to `latest`

That is the whole configuration, for a laptop and for a cluster.

The four images
---------------

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Member
     - What it is
   * - ``robovast``
     - The framework image: ROS, nav2, scenario-execution, the VNC stack, ``/out``, the
       compat marker. What a campaign runs in when no simulator adds to it, and the
       ``FROM`` every experiment image builds on. Carries no ``robovast`` Python package —
       a run is driven from outside.
   * - ``robovast-roqsim``
     - ``robovast`` plus roqsim and MuJoCo, used by **both** roqsim shapes. It is the only
       image carrying roqsim *and* the RoboVAST contract, so the ROS shape runs its own
       simulator container from this image too.
   * - ``robovast-controller``
     - The service: ``vast serve``, the REST API and the web UI. ``python:3.12-slim``, with
       no ROS and no GL — deliberately, so the long-lived Deployment stays small.
   * - ``robovast-sidecar``
     - An alpine helper (``mc`` + ``boto3``) for object-store init containers and the
       postprocessing Job. Also the init container of an **experiment-image build** Job,
       which is why that Job carries the deployment's registry pull Secret: on a private
       registry a credential-less build pod cannot fetch its own helper, and the build
       fails before it has read a line of the project.

They are four rather than fewer because their contents barely overlap. ``robovast`` and
``robovast-controller`` share only ``mc``, ``curl`` and the compat marker; merging them
would put a ROS desktop image into the service pod and the scientific stack into every job
pod, and would tie two independent release cadences together.
``robovast-roqsim`` is ``FROM robovast``, so the pair is one layer chain rather than a
duplicate: pulling both costs the base plus the delta.

How a container's image is decided
----------------------------------

Two rules, and there is no third.

**1. Family images are named by role, not by you.** Internally they are referred to
symbolically as ``family:<member>``, which core resolves to
``<project>/<member>:<tag>`` when a campaign is composed. A simulator backend contributes
one; so does the default for the scenario container. Nothing about it needs to appear in a
``.vast``.

**2. An ``image:`` you write in a ``.vast`` is used exactly as written.** That field is for
the campaign's *own* images — a ``sut`` container running some vendor's nav2 build. It is
never rewritten, re-tagged, or moved to another project, digest included, because redirecting
it would launch something you did not choose.

So the way to run a campaign against a different project is **not** to edit the file:

.. code-block:: bash

   vast exec cluster run --image-project ghcr.io/cps-test-lab campaign.vast

and the way to stop that working is to write a fixed ``image:``. If a ``.vast`` of yours
names a family image, delete the line.

Basing your own container on a family image
```````````````````````````````````````````

A ``sut`` has no default — inventing an image for the thing under test would run something
nobody chose — so it must name one. To base it on the framework image while still following
``ROBOVAST_PROJECT``, name the *member*:

.. code-block:: yaml

   execution:
     containers:
       sut:
         image: family:robovast
         system_packages: [ros-jazzy-nav2-minimal-tb4-description]

A concrete ref in the same place is the right answer for a genuine third-party image.

Three uses, three layers
------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - What you want
     - Where it goes
     - Lifetime
   * - "this cluster pulls from our registry"
     - ``ROBOVAST_PROJECT`` in the service pod env, put there by
       ``vast exec cluster upgrade``
     - until changed
   * - "run *this* campaign against my dev build"
     - ``--image-project`` on the run, or ``ROBOVAST_PROJECT`` in the client's environment
     - one campaign
   * - "which images did that published result run on?"
     - the digest recorded per run
     - immutable

The third one is the reason no digest belongs in a ``.vast``. RoboVAST records the digest
each pod actually pulled and can replay a campaign against exactly those images
(``start_campaign(from_campaign=...)``), so reproducibility comes from what the run
recorded rather than from a ref someone pasted in beforehand — which describes an
intention, not a fact.

Where the settings are read
---------------------------

Highest precedence first:

1. ``--image-project`` / ``--image-project-tag`` on ``vast exec cluster run`` — this run only;
2. a real environment variable (``export ROBOVAST_PROJECT=...``);
3. ``./.env`` — **the current directory only**, so this is the *project's* setting;
4. ``~/.config/robovast/env`` — the *user's* setting, read whatever directory ``vast`` runs
   in. Same directory ``vast login`` keeps its config in; override the path with
   ``ROBOVAST_ENV_FILE`` or move the directory with ``XDG_CONFIG_HOME``.

The two files merge key by key, so a project may override one value without restating the
rest. Put anything that describes *your machine* rather than one project in the user file —
a ``./.env`` is silently out of scope the moment you run ``vast`` from elsewhere, which is
easy to miss because everything else keeps working.

On a cluster the images are resolved **inside** the service, so a client's environment
cannot reach them; that is what the per-campaign request fields are for, and why moving a
cluster's default needs an ``upgrade``.

Publishing your own set
-----------------------

.. code-block:: bash

   make release-images PROJECT=docker.io/<ns>                        # asks before publishing
   make release-images PROJECT=docker.io/<ns> TAG=$(date +%F)        # immutable, asks
   make release-images PROJECT=docker.io/<ns> PUSH=1                 # answers yes up front

Publishing is asked on the terminal, naming the destination, **before the first build** —
``buildx --push`` builds and publishes in one pass, so there is no later moment where the
images exist unpublished. Answering no still builds them locally. ``PUSH=1`` answers yes
up front, which is also what a non-interactive caller needs: with no terminal to ask on,
nothing is published.

The same question, from the same place (``container/ask_push.sh``), guards
``release-image-controller`` and ``release-image-roqsim`` below — anything here that can
publish asks first, so there is no command where a forgotten flag is the difference.

Either way all four members move together — a partial set is not usable, because
``ROBOVAST_PROJECT`` moves all four at once. The command prints the two lines that
configure them and offers to write them to ``~/.config/robovast/env``.

To check a project before pointing a cluster at it:

.. code-block:: bash

   make image-digests PROJECT=docker.io/<ns> [TAG=<tag>]

It reads the registry (building nothing, so it works on a fresh checkout), reports what
each member currently resolves to, and fails if any member is missing.

Pinning a deployment
````````````````````

``latest`` floats: what it points at changes under you. To pin, publish an immutable tag
and name it in ``ROBOVAST_PROJECT_TAG``. A *tag* rather than digests, because one tag
covers the whole family and so cannot be four different digests — and a family that could
be pinned member by member is the five-variable configuration this replaced.

Resolving to a floating tag logs a warning naming the image, so an unpinned deployment
says so rather than looking identical to a pinned one.

Which sources an image bakes
````````````````````````````

``release-images`` builds what is **committed**, not what is checked out beside it. The
framework image clones scenario-execution and the scenario-execution-server, and the
simulator image clones roqsim, each at a commit pinned as an ``ARG`` in its Dockerfile.
Updating a checkout therefore does not change the image; moving the pin does:

.. code-block:: bash

   make release-images-update-versions           # report which pins are behind, then offer to apply it
   make release-images-update-versions WRITE=1   # apply without asking, then commit the diff

The report is the question, so it is asked after it: the diff has to be in front of the
decision, which a flag decided in advance cannot be. Without a terminal to ask on it
degrades to the report — a build script that inherited the command must not move a pin
because nobody was there to say no.

Each pin is re-resolved with ``git ls-remote`` against the branch (``BRANCH=`` to use one
other than ``main``), so what lands is by construction a commit on a durable ref — a pin
taken from a feature branch stops resolving the moment that branch is deleted, and every
clean build then fails with ``fatal: reference is not a tree``.

Named after ``release-images`` because that is the flow it belongs to: these pins are what
decide which sources that command bakes. Distinct from ``make refresh-build-pins``, which
re-resolves the third-party ground an image starts from (base-image digests, the dated apt archives; see
``container/pins/README.md``). Moving one of those takes what upstream published; moving a
source pin changes what our own code does, so it is a release decision and gets its own
diff.

The escape hatches, for iterating on a commit that is not pushed yet: ``--roqsim-src`` /
``--scenario-execution-src`` build from a checkout on disk instead of cloning. Only
``--roqsim-src`` is reachable from ``release-images`` (as ``ROQSIM_SRC``); the others need
``container/robovast/build.sh`` directly. An image built that way carries a commit no repo
records, which is why the pin — not the hatch — is the release path.

Moving a cluster's images
-------------------------

.. code-block:: bash

   ROBOVAST_PROJECT=ghcr.io/cps-test-lab vast exec cluster upgrade

``upgrade`` is the command for this, not ``setup --force``. It recovers the cluster's own
configuration and ingress host *from the cluster*, then touches only the Deployment's
image, RBAC and the credential Secrets, and always restarts the pod — which is the only way
``envFrom`` Secrets are re-read. ``setup`` **provisions**: it re-runs the Kueue install, the
object store and the registry storage, and it takes its options as arguments, so a re-run
without the original flags re-provisions with different ones.


What an image records about itself
----------------------------------

Every image RoboVAST builds carries two kinds of self-description, so that "what is in this
image?" and "what was it built from?" can be answered without the build that produced it.

**Labels** — read with ``docker inspect``, or ``docker buildx imagetools inspect`` for a remote
image without pulling it:

``org.opencontainers.image.revision`` / ``.source``
   the RoboVAST commit and repository the image was built from.
``org.robovast.roqsim-ref`` / ``.scenario-execution-ref`` / ``.scenario-execution-server-ref``
   the upstream commits baked in. These are build ``ARG``\ s and therefore invisible from
   outside the build, so without the labels the only way to answer is to read the Dockerfile at
   the recorded commit — and hope the ref was not overridden at build time.
``org.robovast.compat-version``
   the host↔container protocol version. Also written as
   ``/etc/robovast_compat_version`` for images built before the label existed.

**The build lock** — ``/etc/robovast/build-manifest/``, holding ``apt.txt`` (every package with
its version), ``pip.txt`` (``pip freeze`` output) and, when a spec named a moving git ref,
``vcs.txt`` mapping each requested ref to the commit it resolved to.

This is what makes a rebuild reproduce rather than approximate. A campaign author writes
*intent* — ``tree``, ``numpy<=1.13``, a branch — and should keep doing so, because a fresh
campaign generally *should* pick up the current patch release. What must not be re-resolved is a
**re-run**: asking for ``numpy<=1.13`` again a year later gets a different version, silently. The
lock records what actually ran, so those specs can be replaced by exactly those versions.

``vast exec check-retrigger`` reports which recorded images carry a lock, because that is what
decides whether a rebuild would install the same software or merely something compatible.

A campaign's own ``_execution/image_build_refs`` records the same facts per container, read from
the labels at composition time — plus, for a user-supplied image, the ``provenance:`` block its
author declared. Those survive the image being deleted, which the labels do not.

Which revision a deployment is running
``````````````````````````````````````

The controller image carries its revision a second time, as an environment variable
(``ROBOVAST_GIT_REVISION``), because a *running* service is asked something a label cannot
answer: **is the change I just made loaded?** A service loads RoboVAST once, at startup, so
after an edit a perfectly reachable one may still be serving the old code — and every symptom
of that looks like a bug in the change. It reports the baked value as ``code_revision``:

.. code-block:: bash

   vast doctor      # 'service revision': what the service runs, against this checkout
   vast --version   # what this client runs

Nothing has to be passed to get it. Both the environment variable and the revision label are
derived at build time from the checkout the build scripts live in
(``container/git_revision.sh``), so ``make release-images`` bakes them with no extra flag —
there is deliberately no option for it, because an option is something to forget, and a
forgotten one produced exactly this gap. A dirty tree bakes ``<sha>+dirty`` and the build says
so: such an image corresponds to no commit anyone can check out, and a campaign run against it
records that rather than looking reproducible.

A build outside a git checkout bakes nothing, and ``code_revision`` is then **absent** rather
than filled with something else. That is a deliberate answer — *this deployment cannot tell
you* — and it has to stay distinguishable from "a revision that differs", which would send
someone re-releasing over a service that is already current. Absence is also what an image
built before the revision was baked in reports; re-release the family and ``upgrade`` to get
the answer back. The package version is no substitute: it stays ``2.0.0`` across every edit, so
a caller comparing it reads "same code" where the truth is "no information".
