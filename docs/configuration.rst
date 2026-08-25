.. _configuration:

Configuration
=============

This page documents all available parameters in the ``.vast`` configuration file format. The configuration file is written in YAML and defines all aspects of the RoboVAST workflow.

File Structure
--------------

A ``.vast`` configuration file has the following top-level structure:

.. code-block:: yaml

   version: 3
   metadata:
     title: "Project Title"
     description: "Project description"
     ...
   plugins:
     - my_plugin==1.2.3
   configuration:
     - name: scenario1
       ...
   execution:
     ...
   analysis:
     ...

Version
-------

**Type:** Integer

**Required:** Yes

Specifies the version of the configuration file format. The current version is ``3``, and it
is the only one a file you are authoring may declare.

.. code-block:: yaml

   version: 3

An **older** version is migrated forward rather than refused:

* a campaign's archived ``_config/*.vast`` is upgraded in memory whenever it is read, so an
  old campaign can still be displayed, imported and re-run. That file is never rewritten --
  it is the record of what its author wrote.
* a file you are authoring is upgraded in place by ``vast configuration upgrade``, which
  preserves comments.

A **newer** version is refused: a format from a later robovast cannot be migrated backwards,
so the answer is to upgrade robovast. See ``src/robovast/common/migrations/README.md`` for
the ladder itself and for when the version is bumped at all.


Metadata Section
----------------

**Type:** Dictionary

**Required:** No

The ``metadata`` section allows you to provide structured information about the run configuration. This section can contain arbitrary key-value pairs and nested structures. If present, the metadata will be included in the generated ``configurations.yaml`` file.

.. code-block:: yaml

   metadata:
     title: "Robot Navigation Results"
     description: "Autonomous navigation performance evaluation"
     creator: "Your Name"
     keywords: ["robotics", "navigation", "ROS2"]
     license: "CC-BY-4.0"
     custom_fields:
       nested_data: "value"

All fields within ``metadata`` are optional and can be customized according to your needs.

.. note::

   **Campaign directory naming:** if ``metadata.name`` is set, it is used as
   the prefix for the campaign output directory:
   ``<name>-YYYY-MM-DD-HHMMSS`` (e.g. ``navigation-evaluation-2026-03-10-142530``).
   When ``metadata.name`` is omitted the prefix defaults to ``campaign``
   (e.g. ``campaign-2026-03-10-142530``).


Plugins Section
---------------

**Type:** List of strings

**Required:** No

The ``plugins`` section declares the **Python plugin packages** this campaign needs.
Two kinds of plugin resolve from it: **variation types** not built into ``robovast`` /
``robovast-nav`` (for example the ``scenario_mt`` metamorphic types), and
**postprocessing** plugins — an entry-point postprocessing command, or the
third-party dependencies a local ``./file.py:Class`` postprocessing plugin imports.
Each entry is a `pip requirement specifier
<https://pip.pypa.io/en/stable/reference/requirement-specifiers/>`_, in one of three
forms:

.. code-block:: yaml

   plugins:
     # 1. published on a package index — pin the version
     - my_plugin==1.2.3
     # 2. a git repository — pin a ref (branch, tag, or commit)
     - scenario_mt @ git+https://github.com/org/scenario_mt@main
     # 3. a wheel you uploaded into this project — a workspace-relative path
     - ./plugins/my_plugin-1.0.0-py3-none-any.whl

The packages are installed into a virtual environment under ``.robovast_plugins/``
(next to your ``.vast`` for a workspace, or inside the campaign for a re-run), together
with their dependencies, and put on ``sys.path`` before the variations are composed — so
the variation type names resolve — and again before postprocessing runs, so postprocessing
plugins and their dependencies resolve (including on a re-run in a fresh process after a
service restart).

The install is **resolved against robovast's own environment**, so anything the host
already provides is satisfied rather than reinstalled, and only what is genuinely missing
lands in the workspace. One consequence is worth stating: **your plugin should not declare
a dependency on robovast**. A plugin is loaded *into* robovast's process, not installed
beside it, so the host always provides it — and an installer that ignored what was already
present would give the workspace a second copy of robovast, whose entry points would then
be the only ones the process could see. ``validate_project`` reports the declaration if it
finds it.

The directory does not travel: it is excluded from workspace pushes and from image build
contexts, and it is rebuilt wherever a campaign is composed.

.. note::

   **Private git repositories.** A ``git+https`` URL to a private repository needs
   credentials. Provide a GitHub token at deployment time — set ``ROBOVAST_GIT_TOKEN``
   (or ``GITHUB_TOKEN`` / ``GH_TOKEN``) in your project's ``.env`` file (or the
   environment) when you run ``vast exec cluster setup`` **or**
   ``vast exec cluster upgrade``; it is stored as a Kubernetes Secret, and removing
   the variable and upgrading again deletes it.

   That one token serves **both** places a private repository is reached, so there is
   nothing per-project to configure: the service uses it to clone a top-level
   ``plugins:`` entry, and a container's ``python_packages`` git spec installs with it
   inside the image build (as a BuildKit secret — mounted for the install step only,
   so it is in no image layer and no build history, and no ``.vast`` ever carries a
   credential).

   One token covers every repository its account can read; nothing has to be declared
   per repository. It is reachable by any build this deployment runs, so prefer a
   read-only, org-scoped fine-grained PAT over a personal classic one:

   #. go to GitHub → Settings → Developer settings → **Fine-grained tokens**;
   #. **Generate new token**;
   #. set **Resource owner** to your organization — not your personal account, or the
      token cannot see the organization's repositories;
   #. choose **All repositories**, or select the ones the campaigns install from;
   #. set **Contents** to **Read-only** — nothing here writes;
   #. generate it and store it securely; put it in ``.env`` as above.

   Check it before deploying, because the failure mode is misleading — a fine-grained
   token returns **404 Not Found** for a private repository it was not granted, and
   ``git`` reports ``Write access to repository not granted`` for what is only a *read*
   it may not do::

      GH_TOKEN=<token> gh api repos/<org>/<repo> -q .full_name

   A missing token fails **before** the build, when the ref is resolved to a commit:
   ``cannot resolve … the repository needs credentials this deployment does not have``.
   Alternatively, upload a pre-built wheel into the project and reference it by its
   workspace-relative path (form 3 above) — this needs no credentials at all.

.. tip::

   For a single, dependency-free custom variation you do not need to package it: put
   the ``.py`` file in your project and reference the class directly from a
   ``variations`` entry as ``relative/path.py:ClassName`` (see :ref:`variation-points`).


Configuration Section
---------------------

The ``configuration`` section defines which runs are to be executed. It is a list where each entry represents a scenario with its parameters and variations.

Scenario Definition
^^^^^^^^^^^^^^^^^^^

Each scenario in the configuration list has the following structure:

name
""""

**Type:** String

**Required:** Yes

A unique identifier for the scenario. This name will be used as the directory name for results.
It must be lowercase and must not contain underscores, spaces, or periods (use hyphens instead).

.. code-block:: yaml

   configuration:
   - name: test-scenario-1

parameters
""""""""""

**Type:** List of dictionaries

**Required:** No

Fixed parameter values that apply to all runs of this scenario. Each list item should be a dictionary with a single parameter name-value pair.

This is useful when you want to define a single configuration with specific values without variations.

.. code-block:: yaml

   configuration:
   - name: test-fixed
     parameters:
     - growth_rate: 0.07
     - initial_population: 123
     - goal_pose:
         position:
           x: 10.0
           y: 5.0

variations
""""""""""

**Type:** List of variation definitions

**Required:** No

Defines parameter variations to create multiple run configurations. Each variation uses a plugin-provided variation type. See :ref:`variation-points` for available variation types.

Multiple variations are combined using Cartesian product to generate all possible parameter combinations.

.. _config-variation-destination:

**Every factor names where it lands**, with exactly one of two keys — ``scenario:`` for a
parameter the scenario file declares, ``sim:`` for the simulator's own configuration:

.. code-block:: yaml

   configuration:
   - name: test-variations
     variations:
     - ParameterVariationList:
         scenario: speed
         values:
         - 1.0
         - 2.0
         - 3.0
     - ParameterVariationList:
         sim: components.floorplan.floor.friction
         values:
         - 0.6
         - 1.4

This example creates 3 × 2 = 6 run configurations.

The two keys sit at the same level and neither is a default, so a factor's destination is
readable from the line it is written on — and each is checked against the right schema: a
``scenario:`` name against the scenario file's declared parameters, a ``sim:`` key against
the simulator backend's. See :ref:`the simulation channel <sim-channel>` for what can go
on the second one.

.. note::

   ``name:`` is no longer a destination and is refused, naming the two keys that are. It
   used to mean ``scenario:``; one spelling per destination is what keeps the commonest
   line in a ``.vast`` from having two.

.. _config-variation-slots:

Variations that produce several values
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A generator does not produce *a* value — ``PathVariationRandom`` produces a start pose and
goal poses, ``FloorplanGeneration`` a map and a 3D mesh. Those plugins declare **output
slots**, and the same two keys take a *slot to destination* mapping:

.. code-block:: yaml

   - PathVariationRandom:
       scenario: {start: start_pose, goal: goal_poses}
       path_length: 15.0
       num_paths: 1

   - FloorplanGeneration:
       floorplans: [environments/secorolab/secorolab.fpm]
       scenario: {map: map_file}
       sim:      {mesh: plugins.floorplan.mesh}

The floorplan case is why a plugin may use **both** keys at once: nav2 reads the occupancy
map at run time, while the simulator has to compile the mesh into its model — the two
artifacts sit on opposite sides of :ref:`the compile boundary <sim-channel>`, and no single
key could say so.

Every declared slot must be bound, each to exactly one channel; an unknown slot is refused
naming the ones that exist. A plugin may also declare *optional* outputs — obstacle geometry
for a simulator to compile is one — which are simply not produced when left unbound.

Each plugin's slots, and whether they are required, are listed with it under
:ref:`variation-points`.

.. note::

   The retired positional form (``name: [map_file, mesh_file]``) is refused. Which output a
   list entry meant depended on remembering its position, and the plugins whose destination
   names are chosen per campaign could not be described by it at all.

.. note::

   You cannot specify both ``parameters`` and ``variations`` for the same scenario. Use ``parameters`` for fixed values or ``variations`` for parameter sweeps.


sim
"""

**Type:** Dictionary

**Required:** No

Fixed values for the **simulator this configuration runs in** — the sibling of
``parameters`` for the other channel. ``parameters`` is what the trial does; ``sim`` is what
it runs in:

.. code-block:: yaml

   configuration:
   - name: roofless
     parameters:
     - goal_pose: {position: {x: 10.0, y: 5.0}}
     sim:
       overrides:
         plugins: {ceiling: {enabled: false}}

A nested mapping against the backend's own schema, merged over
:ref:`execution.containers.simulation <config-containers>` — which stays the campaign-wide
*default*. A world belongs to a configuration, never to a campaign and never to a single
run.


Execution Section
-----------------

The ``execution`` section specifies how and where tests are executed.

.. _config-containers:

containers
^^^^^^^^^^

**Type:** Mapping of container name → container block

**Required:** Yes

Every container this campaign runs. One namespace, shared by the schema, by
``exec_in_container(container=...)`` and by a scenario's ``remote("ipc:///ipc/<name>")``,
so a name means the same thing everywhere.

**Three names have a defined meaning**, and how many *actual* containers back them
depends on the campaign — a caller never has to know which:

``scenario``
   Runs scenario-execution. Every campaign has one.
``simulation``
   The simulator. May be its own container, the *same* container as ``scenario`` (a
   simulator stepped in-process by scenario-execution), or the same one as ``sut`` (a
   stack that bundles its own simulator).
``sut``
   The system under test.

Any other key is an ad-hoc container and must state its own ``image`` (and normally
``command``) — only the known roles have a default.

.. code-block:: yaml

   execution:
     containers:
       scenario:
         resources: {cpu: 8}
       sut:
         image: family:robovast
         system_packages: [ros-jazzy-navigation2]

Note what is *not* there: the scenario container names no image. Which RoboVAST image a
container needs follows from its role, and which registry it comes from belongs to the
deployment — see :doc:`images`.

Every block takes the same keys:

``image``
   **What the container starts from.** With no package keys that is also what it runs;
   with them, a derived image is built on top. There is no separate ``base_image`` and no
   author-chosen tag — the tag is the container's name — so a campaign states what a
   container *adds*, never what it adds to.

   **This field is for images of your own**, and is used exactly as written — never
   reprojected or re-tagged. Omit it on ``scenario`` to get the RoboVAST framework image,
   which is the normal case; write ``family:<member>`` to base a container of yours on a
   RoboVAST image while still following the deployment's project. :doc:`images` has the
   whole story.
``provenance``
   **Required when ``image`` is one RoboVAST neither built nor publishes.** Two fields,
   ``source`` (the repository or path holding the image's build definition) and ``revision``
   (the commit that built it), plus an optional ``build_recipe`` for a build that is not the
   obvious one.

   .. code-block:: yaml

      containers:
        sut:
          image: my-registry/experiment-sut:humble
          provenance:
            source: https://github.com/org/experiment-images
            revision: 4f2a1c9e8b7d6a5f4e3c2b1a0f9e8d7c6b5a4938
            build_recipe: cd compose && docker compose build

   Without it, **validation and launch both refuse the campaign.** That one field otherwise
   makes the whole campaign untraceable: nothing in its results could say what the image was,
   so it could never be re-run or audited — and the gap only surfaces once it is too late to
   ask. RoboVAST cannot derive this, which is why the author has to state it.

   Not needed for anything RoboVAST can identify itself: an image it builds (you declared
   ``system_packages`` or ``python_packages``), a ``family:`` reference, a ``build:`` reference,
   a container with no ``image`` at all, or a concrete reference to a published family member
   such as ``ghcr.io/<project>/robovast-roqsim:latest``.

   If you would rather not track this, declaring ``system_packages``/``python_packages`` and
   dropping ``image`` lets RoboVAST build the container and record everything automatically.
   ``--allow-opaque-image`` launches anyway and records the exemption on the campaign.
``system_packages``
   apt packages (``apt-get install -y``).

   Rendered as a single ``RUN`` **above every** ``python_packages`` group, because a pip
   install may need what apt provides. So adding one entry here invalidates every python
   layer below it — including the large, stable group an author put first precisely to
   protect it. Worth knowing before the change rather than after: on one campaign here,
   adding a single ROS package cost ~350 s of rebuilding and re-exporting a torch layer
   that was otherwise cached. Settle this list early; it is not a place to iterate.
``python_packages``
   Python packages, as **install groups**. Same vocabulary as the top-level ``plugins:``
   field — an index pin (``shapely>=2.0``), a git URL
   (``pkg @ git+https://host/repo@ref``), an uploaded workspace wheel
   (``./plugins/foo.whl``) — plus a source directory relative to this ``.vast``
   (``packages/my_pkg``), which works here because the build copies the project dir into
   the image build context.

   Each element is either a spec (a group of one) or a **list** of specs installed
   together in one pip pass, which is one image layer. If no element is a list the whole
   list is a single group — the common case, and the one where order does not matter at
   all, because pip sees every local wheel at once and resolves an inter-package
   dependency against its sibling instead of against PyPI. Nest as soon as you want to
   choose the layer boundaries: order *of* groups is install order, and is what the layer
   cache keys on. Put the large and stable first, the small and frequently-bumped last —
   a group that changes invalidates every group after it, so the ordering is the whole
   difference between a five-second rebuild and a five-minute one.

   The grouping only pays off if the cached layers are still there when the next build
   asks for them, which is what the cache *scope* protects — see
   :ref:`Caching in-cluster <build-cache-scope>`.
``command``
   What the container runs. Omitted for the roles RoboVAST drives itself — the scenario
   runner, and a sidecar's scenario-execution server (which is what makes it drivable
   from a scenario with ``remote()``). Required for an ad-hoc container.
``resources``
   ``cpu`` / ``memory``, per container. See :ref:`resources <config-resources>`.
``backend``
   **``simulation`` only** — a simulator backend, which supplies the image, packages,
   environment and start command so the campaign does not restate them. See
   :doc:`simulators`.

This replaces four keys that version 1 had: ``execution.image``,
``execution.resources``, ``execution.secondary_containers`` and the top-level ``build:``
section. A version-1 file is refused with a message naming what each became.

The ``containers`` package keys are distinct from the top-level ``plugins:`` field:
``plugins:`` installs *variation-type* packages into the **composer** (before config
generation), while these bake code and apt into a **container** (before execution). They
share the ``python_packages`` vocabulary but scope different environments.

runs
^^^^

**Type:** Integer

**Required:** Yes (unless specified in CLI)

Number of times to execute each run configuration. Multiple runs allow for statistical analysis of results.

.. code-block:: yaml

   execution:
     runs: 20

behaviours and logs (no longer configurable)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Two former keys are gone as of config version 3.

``bt_log`` recorded how the scenario's behaviour tree progressed, as ``behaviors.jsonl`` in
each run directory, and could be turned off. **It is now always on.** A run whose tree state
was not recorded cannot be explained after the fact; the file is small — tens to a few
hundred KB, beside a rosbag measured in MB — and turning it off also cost the *live* answer,
since ``get_job_state`` reads it to say which action a wedged run is stuck in. There was no
campaign worth paying that for.

The file is ingested into the ``behaviors`` table of ``data.db`` — one row per behaviour
status change, plus a full snapshot of the tree at ``timestamp`` 0 so branches that never
executed are still present. Each row carries ``parent_id`` and ``child_index`` (structure),
``tip_id`` (which leaf determined an ancestor's status) and ``osc_file``/``osc_line``
(where in the scenario source the behaviour came from). The Run view's scenario-tree panel
reads this table. See the scenario-execution documentation for the file format.

``log_topics`` chose what the entrypoint's own recorder captured. It is now fixed at
``/rosout`` and ``/clock``, which is exactly what the merged :ref:`run_log <merged-run-log>`
needs: ``/rosout`` for the lines, ``/clock`` for the sim↔wall mapping (each message's
receive time is wall and its content is sim — see :ref:`clock-map`). What a run records
*beyond* that is the scenario's ``bag_record`` to say, where it sits beside the behaviour
producing it.

This is the *infrastructure* recording, deliberately separate from ``bag_record``: it starts
with the container, so it sees the stack coming up before any scenario does, and it runs on
the wall clock. The scenario's own bag is recorded with ``use_sim_time``, so both of its
axes are sim and it cannot carry that relation at any price. ROS images only; ignored where
``ros2`` is not on PATH.

.. note::

   The output directory keeps its historical name ``logs/rosout_bag``: it is an address the
   postprocessing map, the docs and every existing campaign already use.

   An execution image whose ``scenario_execution`` predates ``--bt-log`` ignores the flag
   rather than failing, so the run still succeeds — it simply produces no ``behaviors.jsonl``
   and no ``behaviors`` table.

runs_per_job
^^^^^^^^^^^^

**Type:** Integer

**Required:** No (default: ``1``)

How many *runs* are packed into a single job. A **run** is one configuration
executed at one run-number (one scenario execution); a **job** is one unit of
dispatch — one Kubernetes Job on the cluster, or one ``docker compose`` run
locally.

- ``1`` (default): each job runs exactly one run. Right for simulators where
  setup dominates and one job should be one scenario (e.g. Gazebo).
- ``> 1``: up to N runs are packed into one job and run sequentially inside a
  **single simulator setup**, with the simulator reset between them. This pays the
  simulator setup cost once per job instead of once per run — a big win for
  simulators with cheap per-run cost (e.g. MuJoCo). Runs are packed config-major,
  so a configuration's repeated runs stay together within a job.

Packing is invisible to results: every run's output is always written to
``<config>/<run>/`` regardless of how runs were grouped into jobs (see
:ref:`results-output-structure`). The number of jobs is
``ceil(num_configs * runs / runs_per_job)``.

.. code-block:: yaml

   execution:
     runs_per_job: 200   # pack up to 200 runs per job (one sim setup)

shm_size
^^^^^^^^

**Type:** String (e.g. ``1Gi``, ``512Mi``)

**Required:** No (default: ``512Mi``)

Size of the pod's shared ``/dev/shm``. One tmpfs is mounted into **every** container of
the run, which is what lets ROS 2's default Fast DDS use its shared-memory transport across
the ``scenario`` / ``sut`` / ``simulation`` boundary. (Unix sockets do not use it — those
are a separate, disk-backed volume — so this sizes DDS traffic and any other POSIX shared
memory the run maps.)

**Most campaigns should not declare it.** RoboVAST gives every run ``512Mi`` unless the
``.vast`` says otherwise, which is what makes one file mean the same thing on both lanes.
Left to the lanes the two disagree, and both defaults are traps: on the cluster ``/dev/shm``
is a memory-backed ``emptyDir`` with no size limit, so it is sized from the pod's memory
limits — or, when no container declares ``resources.memory``, from the whole node; locally
the sidecars share the main container's IPC namespace and inherit Docker's 64 MB. A
container that overruns shared memory dies of **SIGBUS** (``exit 135``), not a clean
``OOMKilled``, so the death arrives with no reason attached to it.

There is deliberately no way to ask for a lane's own default — it is the thing this default
exists to avoid. Declare a size only to raise or lower the reservation:

.. code-block:: yaml

   execution:
     shm_size: 2Gi        # only because this campaign measured a peak above the default

**Picking the number.** Do not guess it twice: every run records what the pool actually held,
so a campaign that has run once says what its successor should declare.

.. code-block:: sql

   SELECT MAX(shm_peak_bytes), MAX(shm_limit_bytes) FROM runs;

``shm_peak_bytes`` is the high-water mark over every tick of the run, bring-up included — a
participant allocates its segments as it starts, and a SIGBUS there loses the run just as
completely as one mid-trial. ``shm_limit_bytes`` is the size that was in force, which is how a
declaration is *checked* rather than assumed: it shows whether the size in force reached the
mount. ``NULL`` in either means unmeasured — a
campaign recorded before the monitor sampled the pool, or a runtime without ``/dev/shm`` — and
is not the same answer as "used none of it".

``get_campaign_summary`` turns the same two numbers into advice (``shm_under_reserved`` when
the peak outgrew the size in force, ``shm_over_reserved`` when the reservation is paying for
room nothing used), sized on the peak plus 25% headroom. A campaign that ran before
``shm_size`` had a default reports ``shm_not_declared`` instead, because it really was handed
whichever default its lane applied.

.. note::

   **Shared memory is not always used, and then there is nothing to tune.** A
   single-container run, a middleware that is not DDS, and nodes co-located in one process all
   touch almost none of it — and Fast DDS falls back to UDP where shared memory is unavailable.
   A peak that fits inside the 64 MiB the local lane hands out for free is left alone, so no
   advice is offered for it. The measurement is still recorded: a peak of nearly nothing
   is a real answer about the experiment.

timeout
^^^^^^^

**Type:** Integer (seconds)

**Required:** No

**Applies to:** Both execution lanes.

Maximum wall-clock time (in seconds) allowed for a single **job** — one unit of work,
which is one run unless ``runs_per_job`` packs several into it. The number is used exactly
as declared; it is not scaled.

A job is the granularity both lanes can actually enforce at, which is why the budget is
stated in it: Kubernetes caps a Job, and the local lane wraps a whole compose step. Neither
can stop an individual run inside a packed job.

- **Local (Docker Compose):** each compose step is wrapped in ``timeout``, which
  SIGTERMs ``docker compose`` — the same shutdown Ctrl+C triggers, so the scenario gets
  a chance to finish writing its results — and SIGKILLs it 30s later if it ignores that.
  A step killed this way tears its stack down and counts as a **failed** run, so a
  truncated batch cannot pass as a shorter successful one.
- **Cluster (Kubernetes):** sets ``activeDeadlineSeconds`` on the Job spec so Kubernetes
  force-terminates the Job (marking it ``DeadlineExceeded``) when the deadline expires.

If omitted (or ``null``), cluster runs fall back to a **backstop of 1 hour per run**
(``activeDeadlineSeconds = 3600 * runs_per_job``) so a hung Job is always eventually
killed rather than hanging the campaign indefinitely. The backstop is per-run and therefore
scales with packing, where a *declared* budget does not — deliberately: an hour is a number
chosen in ignorance of the campaign, so a job of 100 runs must not be killed after the
first few, while a declaration is a statement about the job and is taken at face value.
**Local runs have no such fallback**: with no ``timeout`` declared they remain unbounded,
because enforcing a limit the user set is a different decision from inventing one they did
not.

``stalled`` still needs a per-run figure, and derives one as ``timeout / runs_per_job``.

A Job hard-killed on its deadline is logged with ``HARD-KILLED by activeDeadlineSeconds`` in the service log for later analysis.

.. code-block:: yaml

   execution:
     timeout: 3600   # 1 hour for the job

scenario_file
^^^^^^^^^^^^^

**Type:** String (file path)

**Required:** Yes

Path to the OpenSCENARIO 2 scenario file (``.osc``), relative to the ``.vast`` configuration file. This defines the scenario to execute for all configurations.

.. code-block:: yaml

   execution:
     scenario_file: scenario.osc

simulation
^^^^^^^^^^

**Type:** String (``module:Class``)

**Required:** No

Simulation backend passed to scenario-execution as ``--simulation <module:Class>``. Required by scenarios that use ``wait_for_simulation_end()`` (e.g. MagBotSim); omit it for ROS/Gazebo scenarios that bring their own simulation.

.. code-block:: yaml

   execution:
     simulation: scenario_execution_magbotsim.push_box_simulation:PushBoxSimulation

A ``simulation`` backend can also be combined with the **ROS** runner (see ``mode`` below): when
``mode: ros2`` is set, the ROS runner ticks the ``SimulationInterface`` inside its spin loop, so a
step-based simulation runs alongside the ROS behaviours that drive it (a simulation that publishes
``/clock`` becomes the time source).

mode
^^^^

**Type:** String (``auto`` | ``ros2`` | ``base``)

**Required:** No (default ``auto``)

Selects which scenario-execution runner runs inside the container. ``auto`` keeps the entrypoint's
detection: the ROS runner (``scenario_execution_ros``) when ``ros2`` is on ``PATH``, otherwise the
non-ROS ``scenario_execution`` CLI. ``ros2`` forces the ROS runner. Forcing ``ros2`` is needed when a
``simulation`` (SimulationInterface) must run **alongside** ROS behaviours: only the ROS runner provides
rclpy for the ROS behaviours, and it also ticks the ``SimulationInterface`` in its spin loop.
``base`` forces the non-ROS ``scenario_execution`` CLI **even when** ``ros2`` is on ``PATH`` -- use it
for pure non-ROS scenarios (e.g. :repo_link:`configs/examples/growth_sim`) so they skip the unneeded
ROS runner in an image that ships ROS.

.. code-block:: yaml

   execution:
     mode: ros2
     simulation: my_pkg.my_module:MySimulation

run_as_user
^^^^^^^^^^^

**Type:** Integer

**Required:** No

The user ID (UID) to run the container as. Defaults to ``1000`` if not specified. If your container requires running as root, set this to ``0``.

.. code-block:: yaml

   execution:
     run_as_user: 1000

pre_command
^^^^^^^^^^^

**Type:** String (path to executable script)

**Required:** No

Path to an executable script that will be sourced before each run. The file is executed using ``source <pre_command>``, allowing environment variables to be set and made available to the scenario execution.

**Important constraints:**

- Must be a path to an existing executable file
- No command line parameters are allowed
- The file is sourced (not executed in a sub-shell), so environment variable changes persist

.. code-block:: yaml

   execution:
     pre_command: /config/files/pre_command.sh
     run_files:
     - "**/files/*.sh"

**Command execution context:**

- Runs before the scenario execution via ``source <pre_command>``
- Can modify the container environment
- Environment variables set by the script are available to the scenario
- If the script fails (exits with non-zero), the run fails

.. note::

   Custom scripts can be included in the container using ``run_files`` (see below) to make them available at the specified path.

post_command
^^^^^^^^^^^^

**Type:** String (path to executable)

**Required:** No

Path to an executable file that should be executed after the scenario completes. This is passed to the scenario execution as the ``--post-run`` parameter.

**Important constraints:**

- Must be a path to an existing executable file
- No command line parameters are allowed
- No shell commands or piping allowed
- The file must have executable permissions

.. code-block:: yaml

   execution:
     post_command: /config/files/post_command.sh
     run_files:
     - "**/files/*.sh"

The post command script is executed by the scenario execution framework after the scenario finishes, allowing for cleanup or post-processing tasks.

.. note::

   Custom scripts can be included in the container using ``run_files`` (see below) to make them available at the specified path.

run_files
^^^^^^^^^

**Type:** List of strings (glob patterns)

**Required:** No

List of glob patterns specifying which files from the scenario directory should be copied into the run container. This is useful for including run-specific files like scripts, models, or configuration files.

.. code-block:: yaml

   execution:
     run_files:
     - "**/files/*.py"
     - "**/models/*.sdf"
     - "**/maps/*"

.. note::

   Patterns are matched against the path *relative to the ``.vast``*, and ``*`` crosses
   ``/`` — so ``"files/*"`` also picks up ``files/scene/scene.json`` and
   ``files/__pycache__/*.pyc``. Since run files are content-hashed into the configuration
   identity, a stray ``.pyc`` changes that identity; prefer per-extension patterns over a
   blanket directory glob.

.. _execution-generate:

generate
^^^^^^^^

**Type:** List of generator entries

**Required:** No

Campaign inputs that are **derived** rather than authored — a navigation map compiled from
a floorplan, a browser scene descriptor compiled from a simulation world, a mesh converted
from CAD. Building those with a side script the user must remember to re-run is the failure
this replaces: the campaign starts happily against a **stale or absent** artifact, passes,
and only the results look wrong.

Each entry is ``- <generator>: {out: <dir>, ...}`` — the same single-key shorthand
:ref:`postprocessing <configuration>` uses. ``out`` is a directory relative to the ``.vast``:

.. code-block:: yaml

   execution:
     generate:
     - shell:
         out: files/scene
         inputs: ["../worlds/depot.yaml"]
         command: >-
           roqsim-export-web --world {inputs[0]} --out {out}
           --manifest {out}/.generated.json

Generation runs **once per campaign preparation, before ``run_files`` are collected**, and
the produced files are appended to ``run_files``. So a generated file behaves exactly like a
hand-written one: it is frozen into ``<campaign>/_config/``, bind-mounted into the run at
``/config/<path>``, and **content-hashed into the configuration identity** — a campaign run
against a changed world therefore gets a different identifier instead of silently reusing
the old one. Nothing needs listing twice; do *not* also add a ``run_files`` pattern for the
generated directory.

**Staleness.** A generator is skipped when nothing it read has changed. What it read is
reported by the generator itself, in a ``.generated.json`` manifest
(``{"inputs": ["<path>", ...]}``) written into its output directory — which is why the
example passes ``--manifest``. This matters more than it looks: the true dependency set of a
compiled world includes the worlds it inherits from and their meshes, and a hand-written
list goes stale the first time someone replaces one. Where a tool cannot report its inputs,
``shell`` falls back to the declared ``inputs``, and a generator that reports nothing at all
simply re-runs every time — staleness always fails towards doing the work.

**Auxiliary containers.** A generator whose tool is not installed alongside RoboVAST can run
in a container instead, so the *service's* environment stops mattering. With ``shell``, add
``image``; inputs are copied into the container's workspace and results copied back, so
``{out}`` and ``{inputs[i]}`` are valid paths inside it:

.. code-block:: yaml

     - shell:
         out: files/maps
         image: ghcr.io/secorolab/scenery_builder
         inputs: ["floorplans/rooms.fpm"]
         command: floorplan --input {inputs[0]} --output {out}

Locally this is an ephemeral ``docker run``; in-cluster a container in the campaign's
auxiliary pod.

.. note::

   ``command`` is expanded with Python's ``str.format``, so ``{out}`` and ``{inputs[i]}``
   are substituted and a **literal** brace must be doubled (``{{`` / ``}}``) — relevant when
   the command passes JSON or a shell parameter expansion.

   A bare command name is looked up **beside the interpreter composing the campaign
   first**, then on ``PATH``. So a tool installed into the same environment as RoboVAST is
   found with no ``PATH`` setup, and one that is missing is reported with both locations
   named rather than as a bare "no such file". Give an absolute path to bypass the search.

**Failures are loud, always.** An unknown generator, a missing declared input, a command
that exits non-zero, or one that reports success but writes nothing — each stops the
campaign with an error naming the entry, before any compute is spent. On failure the
previous contents of ``out`` are left untouched rather than half-overwritten, and
``vast config validate`` reports all of it without starting a run.

.. warning::

   **Inputs outside the project directory work in place, but not from a workspace.** Only
   the project directory is copied into a service workspace, so a generator reading a
   sibling checkout (``../other_repo/world.yaml``) composes fine when the campaign is run
   from the tree and fails when the same ``.vast`` is started from a workspace — which is
   how the cluster lane runs it. Validation emits a warning naming the path. Keep generator
   inputs under the ``.vast``'s own directory unless the campaign is only ever run in place.

``shell`` is built in. Other generators come from installed packages (the
``robovast.input_generators`` entry-point group) or from a ``./path.py:Class`` file
reference next to the ``.vast`` — see :ref:`extending-input-generation` for writing one.

env
^^^

**Type:** List of dictionaries

**Required:** No

Additional environment variables to set in the run container. Each list item should be a single key-value pair.

.. code-block:: yaml

   execution:
     env:
     - RMW_IMPLEMENTATION: rmw_cyclonedds_cpp
     - CUSTOM_VAR: custom_value
     - ENABLE_X11: "false"

.. _config-resources:

resources
^^^^^^^^^

**Type:** Dictionary, inside a :ref:`container block <config-containers>`

**Required:** No

**Applies to:** Local and cluster execution

CPU and memory limits, declared **per container** — Docker Compose enforces them locally,
Kubernetes on the cluster. The scenario container's values are also exposed as
``AVAILABLE_CPUS`` and ``AVAILABLE_MEM`` inside it.

.. code-block:: yaml

   execution:
     containers:
       scenario:
         image: ghcr.io/cps-test-lab/robovast:latest
         resources: {cpu: 2}
       sut:
         image: ghcr.io/cps-test-lab/robovast:latest
         resources: {cpu: 3, memory: 4Gi}
       simulation:
         image: ghcr.io/cps-test-lab/roqsim-ros:jazzy
         command: [roqsim, sim, worlds/depot.yaml, --ros, --headless]
         resources: {cpu: 5, memory: 8Gi, gpu: 1}

**Available fields:**

- ``cpu`` (Optional): Number of CPU cores — whole (``4``), fractional (``0.5``) or in the
  millicore spelling Kubernetes uses (``"500m"``) — or a per-cluster list
- ``memory`` (Optional): Memory limit (e.g. ``8Gi``, ``4096Mi``), or a per-cluster list
- ``gpu`` (Optional): Number of GPUs. **Rarely needed.** Omit it and the container running
  the simulator gets one wherever the cluster advertises GPUs, so the common case is to say
  nothing; ``gpu: 0`` opts out on a cluster that has them (worth doing for a camera-less
  world, which never renders). Setting it enables the NVIDIA runtime on both lanes. On the
  Kubernetes lane the GPU must also be schedulable, which ``vast execution cluster setup``
  arranges — see :ref:`cluster-gpu`, which also covers why the replica count caps
  concurrency without partitioning VRAM, and the comparability caveat for a campaign whose
  cells ran at different GPU concurrency.

Fractional cores are worth the trouble on the cluster, where a campaign's throughput is
``quota // pod_request``: rounding a sidecar that measures 0.3 cores up to a whole one is
paid on **every job of the sweep**. The Monitor's **Details** panel measures what each
container actually used and suggests the number to type here (see :doc:`web_ui`). Both
lanes take the fractional value — the local lane converts a millicore declaration to
Compose's decimal core count, since ``cpus: '500m'`` is not a form Compose accepts.

**Per-cluster resource values** are supported when multiple clusters need different
allocations. See :ref:`cluster-execution` for the full syntax.

.. code-block:: yaml

   execution:
     containers:
       scenario:
         resources:
           cpu:
             - gcp-c4: 4      # 4 CPUs on the gcp-c4 cluster
             - local:   8     # 8 CPUs when running locally

.. note::

   Every container other than ``scenario`` runs alongside it in the same pod (Kubernetes)
   or Compose stack (local), sharing its network and IPC namespaces — so they reach each
   other over localhost, and over ``/ipc``. One that declares no ``command`` runs
   ``secondary_entrypoint.sh``, i.e. a ``scenario_execution_server`` the scenario drives
   with ``remote("ipc:///ipc/<name>")``; one that declares a command runs that instead.

local
^^^^^

**Type:** Dictionary

**Required:** No

**Applies to:** Local execution only (ignored for cluster runs)

Configuration options that apply only when running tests locally (e.g. ``vast execution local run``).

local.parameter_overrides
""""""""""""""""""""""""""

**Type:** List of dictionaries (key-value pairs)

**Required:** No

Overrides for scenario parameters that are added to the generated ``scenario.config`` **only for local runs**. Each list item is a single key-value pair. Values override whatever was produced by configuration variations. Nested dictionaries are supported (values are replaced entirely).

Parameters are validated against the scenario file (``.osc``); only parameters defined in the scenario are allowed.

.. code-block:: yaml

   execution:
     scenario_file: scenario.osc
     local:
       parameter_overrides:
       - use_rviz: "True"

.. note::

   Parameter values must match the types expected by the scenario. If the scenario defines a parameter as a string (e.g. ``headless: string = "False"``), use quoted values.

local.gui.parameter_overrides
"""""""""""""""""""""""""""""

**Type:** List of dictionaries (key-value pairs)

**Required:** No

The same thing, for local runs that have a **host display** wired in — merged *after*
``local.parameter_overrides``, so it wins where both set a parameter. This is where a
parameter that only makes sense with a window belongs, ``headless`` above all: a local run
launched headless has no display for the scenario to draw on, and asking it to open a
window there fails or renders nowhere.

A run has a display when it was launched with one — ``vast execution local run`` (the
default; ``--no-gui`` opts out) or ``start_campaign(show_gui=True)`` / ``exec_in_container(show_gui=True)``
against a local ``vast serve``. Cluster runs never do, and never apply either block.

.. code-block:: yaml

   execution:
     scenario_file: scenario.osc
     env:
     # No in-container Xvfb: with a window the container draws on the *host* X server
     # through a mounted socket, which a virtual framebuffer would only shadow.
     - ENABLE_X11: "false"
     local:
       gui:
         parameter_overrides:
         - headless: "False"

.. note::

   The condition is in the config path rather than in the meaning of
   ``local.parameter_overrides``, which still applies to **every** local run. That way
   adding a window to a project cannot change what its headless runs do.

kubernetes
^^^^^^^^^^

**Type:** Dictionary

**Required:** No

**Applies to:** Cluster execution only (ignored for local runs)

Configuration options that apply only when running tests on a Kubernetes cluster (e.g. ``vast execution cluster run``).

kubernetes.jobs
"""""""""""""""

**Type:** Dictionary

**Required:** No

Settings applied to the Kubernetes ``Job`` objects that execute individual runs.

kubernetes.jobs.node_labels
''''''''''''''''''''''''''''

**Type:** Dictionary (label key-value pairs)

**Required:** No

Node selector labels added to the ``Job`` pod spec via ``nodeSelector``.
Only nodes whose labels match **all** specified key-value pairs will be
eligible to run the job pods.  Use this to pin simulation workloads to a
dedicated node pool (e.g. high-CPU nodes).

.. code-block:: yaml

   execution:
     kubernetes:
       jobs:
         node_labels:
           node-pool: primary

kubernetes.control
""""""""""""""""""

**Type:** Dictionary

**Required:** No

Settings applied to RoboVAST's own infrastructure pods, as opposed to the
campaign's job pods. (The name predates the current architecture: the
per-campaign controller pod it was written for no longer exists.)

kubernetes.control.node_labels
'''''''''''''''''''''''''''''''

**Type:** Dictionary (label key-value pairs)

**Required:** No

Node selector labels added to RoboVAST's own pods, so the orchestration
workload runs on a separate, lighter node pool and does not compete with
simulation jobs for resources.

**Narrows, rather than decides.** Which single node holds this deployment's
node-local data is decided at setup and recorded as a node label (see
:ref:`cluster-node-local-storage`); these labels are ANDed with it, bounding
the pool that choice may be made from. On their own they would still let the
pod float within the pool, which is the same problem at a smaller scale.

Read only from a ``.vast`` named explicitly with ``vast -V <file>``.

.. code-block:: yaml

   execution:
     kubernetes:
       control:
         node_labels:
           node-pool: extra

**Combined example** (pin jobs to ``primary`` nodes, control pod to ``extra``)

.. code-block:: yaml

   execution:
     kubernetes:
       jobs:
         node_labels:
           node-pool: primary
       control:
         node_labels:
           node-pool: extra


Results Processing Section
--------------------------

The ``results_processing`` section defines how run results should be processed after execution.

postprocessing
^^^^^^^^^^^^^^

**Type:** List of strings (plugin commands)

**Required:** No

Commands to run for postprocessing run results. These are executed before the evaluation GUI is launched and typically convert raw data files into more analysis-friendly formats.

**All postprocessing commands are plugins.** Each command is specified either as:
- A simple string (for commands without parameters)
- A dictionary with the plugin name as key and parameters as value

.. code-block:: yaml

   results_processing:
     postprocessing:
       - rosbags_tf_to_csv:
           frames: [base_link, turtlebot4_base_link_gt]
       - rosbags_to_webm:
           topic: /camera/image_raw/compressed
           fps: 30
       - rosbags_action_to_csv:
           action: navigate_to_pose
       - command:
           script: tools/custom_script.sh
           args: [--arg, value]

To list all available plugins and their descriptions:

.. code-block:: bash

   vast results postprocess-commands

**Built-in Postprocessing Plugins:**

- ``rosbags_tf_to_csv``: Convert ROS TF transformations to a ``poses`` CSV, map-relative, one row per frame per sample. ``frames`` is a list of child frame names, or ``all`` for every child frame that resolves against ``map``. ``all`` is for the web :ref:`Run view <run-view>`'s 3D panel, which animates one scene body per frame: a world with people or movable props has a frame per skeleton bone and per prop, and listing them is per-world busywork that adding a prop silently invalidates. ``require`` (list) names the frames that **must** be present — a required frame yielding nothing fails the step, which is how a ground-truth frame missing from one simulator's bags gets caught instead of silently analyzed as absent. An explicit ``frames`` list requires itself, so ``frames: all`` is normally paired with ``require``. Note that a frame latched once on ``/tf_static`` before the dynamic chain to ``map`` exists never resolves and so is not written (a body welded in a scene is already baked at that pose, so this costs a viewer nothing).
- ``rosbags_nav2bt_to_csv``: Convert nav2's behavior-tree log (``/behavior_tree_log``, ``nav2_msgs/msg/BehaviorTreeLog``) to a ``nav2_behavior_tree`` CSV — one row per status transition (``timestamp, node_name, uid, previous_status, current_status, event_timestamp``).
  ``uid`` is nav2's per-node id, the only column separating two nodes that share a ``node_name`` (an unnamed ``RecoveryNode`` appears once per instance in a default tree); it is empty for pre-Jazzy message definitions. ``timestamp`` is the bag receive time, i.e. the same clock as every other table (see :ref:`one clock per run <run-clock>`); nav2 stamps its own events from a wall clock even under ``use_sim_time``, so that stamp is kept separately as ``event_timestamp`` rather than used to key the table. The log has no tree topology; pair it with the ``robovast_nav`` plugin's ``nav2_bt_tree`` command (below) to reconstruct the tree. No parameters. Requires ``/behavior_tree_log`` in the scenario's ``bag_record(...)``.
- ``nav2_bt_tree`` (requires the ``robovast_nav`` package): Reconstruct nav2's behavior tree by parsing the BT XML nav2 ran and joining it with the ``nav2_behavior_tree`` transitions, writing a ``nav2_behaviors`` CSV in the same schema as the ``behaviors`` table (so the Run view's tree panel renders it). Required ``bt_xml`` parameter (path, relative to the config dir, to the BT XML — must match ``bt_navigator``'s ``default_nav_to_pose_bt_xml``). List it **after** ``rosbags_nav2bt_to_csv``.
- ``rosbags_to_csv``: Extract a specific set of ROS topics from rosbags to separate CSV files. Required ``topics`` parameter (list of topic names to extract). For each topic one CSV file per bag is written next to the bag, named ``<bag>_<topic>.csv``. (Not for occupancy grids — use ``rosbags_costmap_to_csv``, which stores grids compactly instead of one column per cell.)
- ``rosbags_costmap_to_csv``: Store ``nav_msgs/msg/OccupancyGrid`` frames (nav2 costmaps, the static map) compactly and losslessly for the web :ref:`Run view <run-view>`. Required ``topics`` parameter (list of grid topics, e.g. ``[/map, /global_costmap/costmap, /local_costmap/costmap]``). Writes one ``costmaps`` table row per message: the pose/geometry metadata (resolution, width, height, origin) plus the int8 cells zlib-compressed and base64-encoded. The map's extent in meters is ``width×resolution`` by ``height×resolution``.
- ``rosbags_to_webm``: Convert a ``sensor_msgs/msg/CompressedImage`` topic from ROS bags to WebM video files (VP9 codec), and register each in the run's :ref:`videos table <videos-table>` so the :ref:`camera panel <camera-panel>` and ``get_camera_frame`` can place it on the run's timeline. Optional ``topic`` parameter (compressed image topic name, default ``/camera/image_raw/compressed``) and ``fps`` parameter (fallback frame rate when timestamps are unavailable, default ``30``). The rate is otherwise derived from the frames' own stamps as ``(n-1)/duration``, so the first and last frames land exactly on their recorded moments and only mid-run jitter drifts.
- ``rosbags_action_to_csv``: Extract ROS2 action feedback and status messages to two CSV files (``<filename_prefix>_feedback.csv`` and ``<filename_prefix>_status.csv``). Reads ``/<action>/_action/feedback`` and ``/<action>/_action/status`` topics. Nested data is flattened to columns. Required ``action`` parameter (action name, e.g. ``navigate_to_pose``). Optional ``filename_prefix`` parameter (default: ``action_<action>``).
- ``rosbags_rosout_to_csv``: Extract ROS log messages from the ``/rosout`` topic in ROS bags to a CSV file. Optional ``skip_levels`` parameter (list of log levels to skip, e.g. ``[ERROR, FATAL]``).
- ``run_log``: Merge every container's stdout with ``/rosout`` into one ``run_log.csv`` per run, on the run's playback clock (see :ref:`merged-run-log`). Optional ``min_severity`` (``warn``/``error``; empty keeps everything, which is the default). **Auto-injected** — see the note below.
- ``resource_usage``: Slice the job's resource-monitor samples into one ``resource_usage.csv`` per run — CPU and memory per container, per process name, per ~1 s tick (see :ref:`per-run-resource-usage`). No parameters. **Auto-injected** — see the note below.
- ``command``: Execute arbitrary commands or scripts. Requires ``script`` parameter, optional ``args`` parameter (list).
- ``compress``: Create a gzipped tarball (``<name>-<timestamp>.tar.gz``) for each campaign directory; runs on the host (no Docker). Optional ``output_dir`` (default: results directory), ``exclude_dirs`` (directory names to exclude, default ``['.cache']``), ``overwrite`` (if ``false``, skip when a tarball already exists; default ``false``).

.. note::

   **``run_log`` and ``resource_usage`` run for every campaign without being declared.**
   Both turn a *job*-level artifact into a per-*run* table, and a run whose output cannot be
   read afterwards cannot be explained — so they are appended after the rosbag conversions
   (which produce the ``/rosout`` and ``/clock`` data they read) rather than waiting to be
   asked for. Declare one explicitly only to change a parameter, which also keeps its own
   position in the order; opt out with ``--skip run_log`` / ``--skip resource_usage``.

.. note::

   The ``rosbags_*`` names above are handled by a single unified plugin,
   ``rosbags_process`` — the one that shows up in ``vast configuration plugins``
   and the ``list_plugins`` MCP tool. When several ``rosbags_*`` commands appear
   in a config, they are transparently batched into one ``rosbags_process`` call
   so each rosbag is read only once. You can keep using the individual
   ``rosbags_*`` names (they remain valid), or write ``rosbags_process`` directly
   with a list of handler ``type`` entries when you need finer control:

   .. code-block:: yaml

      postprocessing:
        - rosbags_process:
            plugins:
              - type: tf_to_csv
                frames: [base_link]
              - type: to_csv
                topics: [/cmd_vel, /odom]

See :ref:`extending-postprocessing` for how to add custom postprocessing plugins.

.. _videos-table:

Recording a camera, and watching it back
""""""""""""""""""""""""""""""""""""""""

Putting a camera in a run view takes three things, and they live in three different parts of a
campaign — so here they are together. A reader who finds only one of them gets an empty panel:

.. code-block:: yaml

   # 1. the scenario records the image topic          (in the .osc, not the .vast)
   #    bag_record(['/static_camera/image/compressed', ...])

   # 2. postprocessing turns the recorded frames into a video, and registers it
   results_processing:
     postprocessing:
     - rosbags_to_webm:
         topic: /static_camera/image/compressed

   # 3. the run view plays it. A bare `- camera:` is enough when the run has one video.
   visualization:
     results:
       run_view:
         panels:
         - camera:
             title: Monitor camera
             position: {anchor: center, width: 560, height: 560}

Validation catches the common half-step: a ``camera`` panel with no step that produces a video
is refused before the campaign runs, rather than showing an empty panel after the compute is
spent.

**The** ``videos`` **table** is what joins steps 2 and 3. One row per recording, in the run's
``videos.csv``:

.. list-table::
   :header-rows: 1
   :widths: 12 88

   * - Column
     - Meaning
   * - ``topic``
     - The recorded topic, and how a panel or tool picks between several cameras.
   * - ``file``
     - The video, relative to the run directory.
   * - ``t_start``, ``t_end``
     - First and last frame, in **seconds on the run's timeline** — the same clock every other
       table's timestamps use (see :ref:`one clock per run <run-clock>`).
   * - ``fps``, ``frames``
     - The rate written into the container, and how many frames it holds.

``t_start`` is the load-bearing one. The encode re-times frames onto a constant rate and drops
the bag stamps, so the file alone cannot say when its first frame was — and a camera that came
up ten seconds into a trial would otherwise replay as though it had run from the start.

This is a **contract, not** ``rosbags_to_webm``'s **private file**. That step is the first
producer, not the owner: anything that puts a video in a run directory may write the same row —
another postprocessing step, a simulator that renders its own, a script of your own — and the
camera panel and ``get_camera_frame`` then work for it unchanged. Without that, the only way to
reach the panel would be "record a ``CompressedImage`` through a rosbag", which is a
ROS-shaped assumption in a feature that has no reason to carry one.

publication
^^^^^^^^^^^

**Type:** List of strings or dictionaries (plugin commands)

**Required:** No

Defines publication plugins that package or distribute the results directory after
postprocessing.  Each entry is either a plugin name (string) or a dictionary with
the plugin name as key and plugin-specific parameters as value.

Publication plugins are executed by ``vast results publish`` and operate on the
full results directory (parent of campaign directories).

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           include_filter:
           - "*.csv"
           - "/_config/*"
           exclude_filter:
           - "*.pyc"
           omit_hidden: true
           destination: archives/

**Built-in Publication Plugins:**

- ``zip``: Create a zip archive for every campaign directory under the
  results directory.  Optional parameters:

  - ``filename``: Template for the zip filename.  Supports ``{key}``
    placeholders resolved from the ``.vast`` file's ``metadata:`` section and
    the built-in ``{timestamp}`` placeholder (extracted from the campaign
    directory name, e.g. ``campaign-2026-03-05-121530`` → ``2026-03-05-121530``).
    Example: ``my_dataset_{robot_id}_{timestamp}.zip``.
    If omitted, the default name ``<campaign-dir-name>.zip`` is used.
    A descriptive error listing all available placeholders is raised when an
    unknown placeholder is referenced.
  - ``include_filter``: List of glob patterns.  Only matching files are included.
    Patterns starting with ``/`` are anchored to the campaign root; patterns without
    ``/`` match on the basename only; other patterns are matched against the full
    relative path.  If omitted, all files are candidates.
  - ``exclude_filter``: List of glob patterns.  Matching files are excluded regardless
    of ``include_filter``.
  - ``destination``: Directory where zip files are written.  Relative paths are
    resolved from the results directory.  Defaults to the results directory itself.
  - ``overwrite``: Controls behavior when the output zip file already exists.
    ``true`` always overwrites silently; ``false`` always skips silently.
    Omit (or leave unset) to be prompted interactively — the default answer is
    *yes* (overwrite).  Passing ``--force`` / ``-f`` on the CLI is equivalent
    to setting ``overwrite: true`` for every plugin.
  - ``omit_hidden``: When ``true``, directory components whose names start
    with ``_`` are stripped from the file paths *inside* the archive.
    The files are still selected by ``include_filter`` / ``exclude_filter``
    using their original on-disk paths; only the stored archive member path
    is affected.

    For example, a file stored on disk as
    ``campaign-2026-03-05-163338/_config/my_file.yaml`` is placed in the
    zip as ``campaign-2026-03-05-163338/my_file.yaml``.
    Defaults to ``false``.
  - ``metadata``: Optional dict of metadata fields to merge into the
    ``metadata:`` section of the campaign's ``metadata.yaml`` before
    including it in the archive.  Typical fields are ``title`` and
    ``description``.  The merged ``metadata.yaml`` is always written
    regardless of ``include_filter`` / ``exclude_filter``.

Multiple ``zip`` entries may be defined to produce different archives from the
same campaign:

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           filename: my_dataset_{robot_id}_{timestamp}.zip
           include_filter: ["*.csv"]
           destination: csv-archives/
           overwrite: false    # skip if archive already exists
       - zip:
           filename: my_dataset_{robot_id}_{timestamp}_videos.zip
           include_filter: ["*.webm"]
           destination: video-archives/

See :ref:`extending-publication` for how to add custom publication plugins.

metadata_processing
^^^^^^^^^^^^^^^^^^^^

**Type:** List of strings or dictionaries (plugin commands)

**Required:** No

Defines metadata processing plugins that run after generic metadata generation.

.. code-block:: yaml

   results_processing:
     metadata_processing:
       - my_plugin
       - my_plugin:
           param1: value1


.. _visualization-section:

Visualization Section
---------------------

The ``visualization`` section declares everything the web UI draws for this campaign, and it
is **shaped like the UI**: one key per place a declaration appears, so reading it says *where*
each block shows up.

.. code-block:: yaml

   visualization:
     config:                       # the Config tab
       panels: [...]               #   its third column, per generated configuration
                                   #   (see :ref:`config-view` for the panel reference)
     results:                      # the Results tab
       run_view:
         panels: [...]             #   the replay panels (the playback bar comes for free)
         timeline: {...}           #   which table defines the playback clock
       explorer:
         notebooks: [...]          #   analysis notebooks, one tab each
       data_browser:
         plots: [...]              #   campaign-scoped declared plots

.. note::

   This replaced a flat ``visualization.panels`` beside a separate top-level ``evaluation:``
   block (``evaluation.visualization`` for the notebooks, ``evaluation.plots`` for the plots).
   The ``evaluation:`` key is **gone** — it is not accepted, and a ``.vast`` still using it is
   refused by name. See :ref:`the key map <visualization-key-map>` in the web UI page.

results.explorer.notebooks
^^^^^^^^^^^^^^^^^^^^^^^^^^

**Type:** List of dictionaries

**Required:** No

Defines analysis notebooks for the Results Explorer. Each entry creates a tab, executed
server-side against the ``DATA_DIR`` contract for the selected tree node.

Each dictionary can have a custom name and four reserved keys for different analysis scopes:

- ``run``: Path to Jupyter notebook for analyzing a single run
- ``config``: Path to Jupyter notebook for analyzing all runs of a configuration
- ``batch``: Path to Jupyter notebook for analyzing one batch of a search (the
  configurations a strategy proposed in a single ask/tell round)
- ``campaign``: Path to Jupyter notebook for analyzing all runs in a campaign

.. code-block:: yaml

   visualization:
     results:
       explorer:
         notebooks:
         - Analysis:
             run: analysis/analysis_run.ipynb
             config: analysis/analysis_config.ipynb
             batch: analysis/analysis_batch.ipynb
             campaign: analysis/analysis_campaign.ipynb
         - Performance:
             run: analysis/performance_run.ipynb
             config: analysis/performance_config.ipynb

**Notebook requirements:**

Each notebook must include the following placeholder line:

.. code-block:: python

   DATA_DIR = ''

The RoboVAST GUI automatically replaces this with the path to the selected run
directory when executing the notebook.

``batch`` notebooks only appear for searches (a batch tree node exists only in
search mode, in both front-ends). Because the search results layout is flat — every
batch's configurations live directly under the campaign root — ``DATA_DIR`` alone cannot
identify the batch, so a batch notebook receives the campaign root as its ``DATA_DIR``
plus an injected ``BATCH`` variable (the batch index), and selects its configurations from
``campaign.db``:

.. code-block:: python

   DATA_DIR = ''
   BATCH = None    # injected: the index of the selected batch


Complete Example
----------------

Here's a complete example showing all major configuration options:

.. code-block:: yaml

   version: 3
   configuration:
   - name: parameter-sweep
     scenario_file: scenario.osc
     variations:
     - ParameterVariationList:
         name: velocity
         values: [1.0, 2.0, 3.0]
     - ParameterVariationDistributionUniform:
         name: obstacle_count
         num_variations: 5
         min: 1
         max: 10
         type: int
         seed: 42
   - name: baseline
     scenario_file: scenario.osc
     parameters:
     - velocity: 2.0
     - obstacle_count: 5
   execution:
     containers:
       scenario:
         image: ghcr.io/cps-test-lab/robovast:latest
         resources:
           cpu: 4
           memory: 8Gi
       sut:
         image: ghcr.io/cps-test-lab/robovast:latest
         resources:
           cpu: 3
           memory: 4Gi
     runs: 20
     pre_command: /config/files/prepare_test.sh
     post_command: /config/files/post_command.sh
     run_as_user: 1000
     run_files:
     - "**/files/*"
     - "**/models/*.sdf"
     env:
     - RMW_IMPLEMENTATION: rmw_cyclonedds_cpp
   results_processing:
     postprocessing:
     - rosbags_tf_to_csv:
        frames: [base_link]
     - rosbags_to_csv
     - rosbags_to_webm
   visualization:
     results:
       explorer:
         notebooks:
         - Analysis:
             run: analysis/analysis_run.ipynb
             config: analysis/analysis_config.ipynb
             campaign: analysis/analysis_campaign.ipynb
