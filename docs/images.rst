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

   vast workspace run my-experiment campaign.vast \
       --image-project ghcr.io/cps-test-lab

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

.. _ros-packages:

ROS packages built from source
``````````````````````````````

``system_packages`` covers what apt has and ``python_packages`` covers what pip has. Some ROS
packages are in neither: a package with a ``source:`` entry and no ``release:`` block in
``ros/rosdistro`` has no Debian on **any** distro, and is not on PyPI either. ``px4_msgs`` is
one; vendor driver and message packages routinely are. Before this key the only way in was to
bake such a package into a shared family image, which makes every unrelated campaign pay for it.

``ros_packages`` closes that: each entry is a git repository, cloned at a pinned ref and
colcon-built into the container's ``/ws`` overlay.

.. code-block:: yaml

   execution:
     containers:
       scenario:
         ros_packages:
           - git: https://github.com/PX4/px4_msgs.git
             ref: 598c7aad7b2386f9406ebd2a2f841619fddc3c78
           - git: https://github.com/example/some_repo.git
             ref: v2.1.0
             packages: [only_this_one, and_this_one]   # optional

**One workspace, one build.** Every entry is cloned into ``src/`` of the *same* workspace and
built in a single ``colcon build``. That is the entire reason a workspace is involved: a
package's dependency on a sibling — in the same repo or in another entry — resolves against the
sibling being built beside it, rather than against a released Debian which, for a source-only
package, does not exist. Repo order in the YAML therefore decides nothing and does not affect
the image hash; colcon derives the build order from the packages themselves.

**Not restricted to ROS packages, because colcon is not.** A repository goes into ``src/`` and
colcon decides what it contains: ``colcon-cmake`` discovers a plain CMake project by its
``CMakeLists.txt``, and such a project may ship a ``colcon.pkg`` giving its name, type and cmake
arguments with no ``package.xml`` anywhere. The Micro XRCE-DDS Agent is exactly this shape and
builds in the same pass as ``px4_msgs``. The key's name follows the ROS-workspace idiom; it is
not a statement about what may go in it. cmake arguments are likewise not a ``.vast`` key: a
project's ``colcon.pkg`` already states them, and a second place to state one thing is a second
place for them to disagree.

**``ref:`` is required and must pin** — a commit sha, or a release tag. A layer's cache key is
its command text, so a branch name would be read on the first build and served from then on,
with nothing recording which commit that was. A ref that reads like a branch is refused at
validation.

**``packages:`` is optional, and omitting it is the normal case.** Left out, every package the
repository contains is built: colcon discovers them at build time, so a repo with one package
and a repo with forty are the same declaration and the Dockerfile text stays identical either
way. Name packages only to take part of a monorepo; the build is then restricted to those, plus
their dependencies (``--packages-up-to``), plus everything found in the entries that named none.

Declaring ``ros_packages`` is enough on its own to make RoboVAST build a derived image — the
container needs no ``system_packages`` or ``python_packages`` beside it — and the ref of each
repository is recorded in the image's build manifest (``vcs.txt``, see
:ref:`what an image records <image-records>`), which for a source-built package is the only
statement anywhere of what code the overlay holds. The overlay is ``/ws``, the workspace the
framework image already builds into and the entrypoint already sources, so the packages are on
the environment of every process a run starts, by the mechanism that was already there.

A build that clones nothing, or that ends up selecting no packages, fails the image build with a
message naming the repository, rather than producing an image that quietly lacks them. The clone
carries no credential — the token a private ``python_packages`` git spec uses is mounted for the
pip layer alone — so a repository that needs one fails there, at the clone, rather than later.

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
       ``vast service upgrade``
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

1. ``--image-project`` / ``--image-project-tag`` on ``vast workspace run`` — this run only;
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
configure them; it does not change any configuration on its own. Pass ``CONFIG_WRITE=1``
(or set ``ROBOVAST_RELEASE_CONFIG_WRITE=1`` in your shell) to have it write those two lines
into ``~/.config/robovast/env`` — that switches every ``vast`` on the machine to this image
set, which is why it is opt-in.

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

   ROBOVAST_PROJECT=ghcr.io/cps-test-lab vast service upgrade

``upgrade`` is the command for this, not ``setup --force``. It recovers the cluster's own
configuration and ingress host *from the cluster*, then touches only the Deployment's
image, RBAC and the credential Secrets, and always restarts the pod — which is the only way
``envFrom`` Secrets are re-read. ``setup`` **provisions**: it re-runs the GPU device-plugin
install, the object store and the registry storage, and it takes its options as arguments, so a re-run
without the original flags re-provisions with different ones.


.. _image-records:

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
   the host↔container protocol version — the only marker for it. A file inside the image once
   carried the same value; it could not be read without starting a container, and could not be
   read remotely at all, which is the case that matters.
``org.robovast.base-image``
   the base this was built ``FROM``, as a whole digest-pinned ref so it can be used as-is.
``org.robovast.ubuntu-snapshot`` / ``.ros-snapshot``
   the dated apt archives it installed from. Together with the base, this is what a rebuild
   needs in order to *reproduce* rather than approximate: pinning package versions alone does
   not survive, because an archive drops a superseded version and ``apt-get install pkg=1.2.3``
   then fails. Like the source refs above these are build ``ARG``\ s, so the labels are the only
   way out of the build — see ``container/pins/``.

All of them reach a campaign's ``_execution/execution.yaml`` as ``image_build_refs``. On the
cluster lane that block used to be empty for every campaign: it read labels with ``docker
inspect``, and the controller pod that writes the file has no docker CLI. It now uses the labels
the protocol check already read from the registry.

**The recipe is only worth what it still names.** A dated archive that has been pruned fails at
``apt-get update`` inside a rebuild nobody runs until the year-old campaign someone actually
needs — so ``make check-recipe`` asks the two questions that can be asked cheaply, and CI asks
them on every pull request:

*Is it complete?* Every pin the Dockerfile makes has to reach the image as a label. An **empty**
label is the failure worth naming, because a forgotten ``--build-arg`` produces one and it looks
present to anything checking only for the key — which is why the build refuses to start without
the value rather than publishing an image labelled ``""``.

*Do its archives still serve?* One request each against the snapshots the recipe names. The
pull-request job asks this from the Dockerfile's own pins, needing no image at all; the image
build then asserts the pushed bytes actually carry what was declared, which is the half only a
real build can answer.

Neither of those rebuilds, and rebuilding is the only thing that proves a recipe *sufficient* --
that the pins it names are the **complete** set of inputs. Something reaching the network
unpinned passes both cheap checks and fails only in a rebuild.

So ``tools/rebuild_from_recipe.py`` rebuilds an image from its own recorded recipe and compares
the **software**, weekly (``.github/workflows/recipe_rebuild.yml``) rather than per pull request,
because it costs a full image build.

The comparison is the build lock, not the digest: Docker builds are not bit-reproducible, so a
perfect rebuild still differs by timestamps and layer ordering. The lock is ``dpkg-query``
output, ``pip freeze``, and the commit each floating git ref resolved to — the software, which
is what an experiment depends on.

Two things it needs that are easy to miss. The **Dockerfile is an input**, and the recipe records
the commit it came from rather than the file, so the rebuild checks that revision out first and
refuses if it cannot; building today's Dockerfile with an old image's pins tests a combination
that never existed. And the recipe and the lock must come from the **same** image — a tag means
different bytes locally and remotely the moment the registry moves ahead, so a real comparison
insists on one image and only ``--plan-only`` may ask the registry.

It proves sufficiency at the moment it runs, not forever: a rot detector on a longer timescale
than the checks above, and not a standing guarantee.

**The build lock** — ``/etc/robovast/build-manifest/``, holding ``apt.txt`` (every package with
its version), ``pip.txt`` (``pip freeze`` output) and, when a spec named a moving git ref,
``vcs.txt`` mapping each requested ref to the commit it resolved to.

This is what makes a rebuild reproduce rather than approximate. A campaign author writes
*intent* — ``tree``, ``numpy<=1.13``, a branch — and should keep doing so, because a fresh
campaign generally *should* pick up the current patch release. What must not be re-resolved is a
**re-run**: asking for ``numpy<=1.13`` again a year later gets a different version, silently. The
lock records what actually ran, so those specs can be replaced by exactly those versions.

``vast campaign rerun --check`` reports which recorded images carry a lock, because that is what
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
(``container/image_stamp.sh``), so ``make release-images`` bakes them with no extra flag —
there is deliberately no option for it, because an option is something to forget, and a
forgotten one produced exactly this gap. A dirty tree bakes ``<sha>+dirty`` and the build says
so: such an image corresponds to no commit anyone can check out, and a campaign run against it
records that rather than looking reproducible.

The same helper bakes ``ROBOVAST_BUILD_DATE`` into the service image, reported as ``built_at``
and printed by ``vast service info`` as ``built``. It answers the question a revision cannot —
*how old is what is deployed?* — and it is baked rather than read from
``org.opencontainers.image.created`` because a container cannot read its own labels. Unlike the
revision it changes on every build, so its ``ARG`` sits last in the Dockerfile, where only an
``ENV`` and two ``LABEL``\ s follow it.

A build outside a git checkout bakes nothing, and ``code_revision`` is then **absent** rather
than filled with something else. That is a deliberate answer — *this deployment cannot tell
you* — and it has to stay distinguishable from "a revision that differs", which would send
someone re-releasing over a service that is already current. Absence is also what an image
built before the revision was baked in reports; re-release the family and ``upgrade`` to get
the answer back. The package version is no substitute: it stays ``2.0.0`` across every edit, so
a caller comparing it reads "same code" where the truth is "no information".
