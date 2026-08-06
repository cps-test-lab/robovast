.. _configuration:

Configuration
=============

This page documents all available parameters in the ``.vast`` configuration file format. The configuration file is written in YAML and defines all aspects of the RoboVAST workflow.

File Structure
--------------

A ``.vast`` configuration file has the following top-level structure:

.. code-block:: yaml

   version: 1
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

Specifies the version of the configuration file format. Currently, only version ``1`` is supported.

.. code-block:: yaml

   version: 1


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

The packages are installed into a ``.robovast_plugins/`` directory (next to your
``.vast`` for a workspace, or inside the campaign for a re-run), together with their
dependencies, and put on ``sys.path`` before the variations are composed — so the
variation type names resolve — and again before postprocessing runs, so postprocessing
plugins and their dependencies resolve (including on a re-run in a fresh process after
a service restart). The installed directory travels with the campaign into the
cluster; the execution pods do not install or clone anything.

.. note::

   **Private git repositories.** A ``git+https`` URL to a private repository needs
   credentials. For the ``robovast-service`` / MCP flow, provide a GitHub token at
   deployment time — set ``ROBOVAST_GIT_TOKEN`` (or ``GITHUB_TOKEN`` / ``GH_TOKEN``)
   in your project's ``.env`` file (or the environment) when you run
   ``vast exec cluster setup``; it is stored as a Kubernetes Secret and the service
   uses it to authenticate the clone. Alternatively, upload a pre-built wheel into
   the project and reference it by its workspace-relative path (form 3 above) — this
   needs no credentials at all.

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

.. code-block:: yaml

   configuration:
   - name: test-variations
     variations:
     - ParameterVariationList:
         name: speed
         values:
         - 1.0
         - 2.0
         - 3.0
     - ParameterVariationList:
         name: distance
         values:
         - 5.0
         - 10.0

This example creates 3 × 2 = 6 run configurations.

.. note::

   You cannot specify both ``parameters`` and ``variations`` for the same scenario. Use ``parameters`` for fixed values or ``variations`` for parameter sweeps.


Execution Section
-----------------

The ``execution`` section specifies how and where tests are executed.

image
^^^^^

**Type:** String (Docker image reference)

**Required:** Yes

Docker container image to use for execution. Can be a public image or a private registry image.

.. code-block:: yaml

   execution:
     image: ghcr.io/cps-test-lab/robovast:latest

It may also be a symbolic ``build:<tag>`` reference to an image produced by the
:ref:`build section <config-build-section>` (built on demand, wherever the
backend runs). See below.

.. _config-build-section:

build (optional)
----------------

Most iteration needs **no** image build: the scenario, ``run_files`` and config
are delivered to the container at runtime. A ``build:`` section is only needed
when the experiment must **bake code or system packages into the image** — e.g. a
new or updated Python package (a new robot in ``sim_suite_mobile``), or an Ubuntu
(apt) dependency. When present, set ``execution.image: build:<tag>``; the service
builds the image on demand and resolves that symbolic reference to the real
(registry-qualified) image — you never handle a registry reference or credentials.

.. code-block:: yaml

   build:
     # base_image is optional: omit for the deployment's default base, or give a
     # published base *alias* (never a registry URL — you need no registry knowledge).
     system_packages:                       # apt-get install (Ubuntu deps)
       - ros-jazzy-nav2-smac-planner
     python_packages:                       # same vocabulary as top-level `plugins:` ...
       - packages/sim_suite_mobile          #   • a source dir relative to this .vast (pip install -e)
       - shapely>=2.0                        #   • an index pin
       - my_ext @ git+https://github.com/org/repo@v1   # • a git URL
       - ./plugins/my_ext-1.0-py3-none-any.whl          # • an uploaded workspace wheel
     tag: sim-suite-mobile                  # a bare name; the registry prefix is added server-side
   execution:
     image: build:sim-suite-mobile          # symbolic → the built image

The list above is **one install group**: pip resolves all of it in a single pass, so the
order does not matter — a local wheel depending on a sibling resolves against the sibling
rather than against the index. Nest to choose the layer boundaries instead:

.. code-block:: yaml

   python_packages:
     - mujoco>=3.0                          # a bare spec is a group of one
     - [wheels/assets-0.1.0-…whl,           # one group: installed together,
        wheels/robots-0.1.0-…whl]           #   order inside it is irrelevant
     - wheels/my_nodes-0.1.0-…whl           # its own layer, rebuilt on every code change

Each group is one ``pip install`` and one image layer. Order *of* groups is install order
— a later group may depend on an earlier one — and is what the layer cache keys on.

Where the packages land
^^^^^^^^^^^^^^^^^^^^^^^

``python_packages`` installs into a **virtualenv at** ``/usr/local``, created by the
generated Dockerfile. That prefix is where pip on a Debian base already installed, so
``PATH``, ``share/ament_index`` and ``AMENT_PREFIX_PATH`` are unchanged; only the python
package directory moves (``site-packages`` rather than ``dist-packages``), and a one-line
``robovast_venv.pth`` hands it back to ``/usr/bin/python3`` — the interpreter ``ros2
launch`` starts nodes with — ahead of Debian's own packages.

The venv is what makes a dependency upgrade possible at all. Debian-packaged
distributions (``numpy``, ``scipy``, ``opencv`` …) carry no ``RECORD`` file, so pip
cannot uninstall them; before the venv, any resolver decision that wanted to *replace*
one killed the build minutes in with ``Cannot uninstall numpy 1.26.4, RECORD file not
found``. Inside a venv pip leaves what is outside it alone (``Not uninstalling numpy at
/usr/lib/python3/dist-packages, outside environment /usr/local``) and installs its own
copy, which the path order then shadows.

One constraint is applied on top, from the base image itself: **numpy<2**, because the
base's ``python3-transforms3d`` is built against the 1.x API and raises
``np.maximum_sctype was removed in the NumPy 2.0 release`` under numpy 2. It steers the
resolver rather than blocking it (``scipy>=1.13`` lands on a 1.x-compatible scipy), and a
requirement that truly needs numpy 2 gets an honest resolver conflict instead of a
runtime break. A project needing otherwise sets its own ``build.base_image``.

One way a ``build:`` fails that the schema cannot catch
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``validate_project`` checks that every entry is *resolvable*, not that the resulting
image will build. This is the failure that actually happens, and it costs a full apt+pip
cycle before it surfaces.

**An ``ament_python`` package installed as a wheel has no ament libexec dir.**
``python_packages`` installs with pip, which puts console scripts in
``/usr/local/bin``. ``launch_ros``'s ``Node(package=..., executable=...)`` and
``ros2 run`` both resolve executables through the package's ament libexec directory,
which a wheel never creates::

    package 'my_pkg' found at '/usr/local', but libexec directory
    '/usr/local/lib/my_pkg' does not exist

Launch files and ``share/`` data still resolve normally — only executables break, so
this surfaces at *launch* time. Start your own nodes with ``ExecuteProcess`` running
``python3 -m my_pkg.my_node`` (append ``--ros-args`` for parameters); that works under
both a wheel and a colcon install. Nodes from properly colcon-built packages
(``rviz2``, ``robot_state_publisher``, ``nav2_*``) are unaffected.

``AMENT_PREFIX_PATH`` needs no attention: the generated Dockerfile sets it to the prefix
it installs into, so ``ros2 launch <pkg> ...`` finds a pip-installed ament package's
``share/`` without the project saying anything. (A ``.vast`` that still sets
``AMENT_PREFIX_PATH: "/usr/local"`` by hand is a harmless no-op.)

The build is **idempotent** (content-addressed): rebuild only happens when the
``build:`` section or a referenced source changes. It runs **wherever the backend
runs** — a local ``docker buildx`` for a local ``vast serve``, an in-cluster
BuildKit Job on the cluster — so the same ``.vast`` works everywhere. Trigger it
explicitly with the ``build_experiment_image`` MCP tool / ``vast image build``, or
implicitly: ``start_campaign`` (re)builds a ``build:<tag>`` image as its first
step. See :doc:`mcp` and :doc:`cluster_execution`.

On a cluster the build additionally needs a container registry to push to, which is a
deployment setting rather than a project one. It does **not** need any particular object
storage mode: it stages its context in the storage the deployment already uses (a
dedicated ``robovast-image-builds`` bucket where campaigns get their own buckets), so
enabling builds never changes where campaign results live. The staged context is scratch
and is removed once the build ends, so the credentials the service uses need delete
permission under the ``image-builds/`` prefix — see
:ref:`Where the build context is staged <cluster-build-context-staging>`.

.. _config-build-caching:

Caching, and how the grouping affects it
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Two independent caches apply, and the second one is worth authoring for.

*Whole image.* The cache key covers the base image, the apt list (order-insensitive),
each ``python_packages`` entry and how the entries are grouped — for a workspace wheel
by its **logical zip content** (member names, CRCs, sizes), not its bytes. Rebuilding a
wheel from unchanged sources therefore does **not** trigger an image rebuild, even though
the regenerated file differs byte-for-byte (pip stamps zip members with their source
mtimes, so a branch switch or a fresh clone rewrites all of them). Nothing outside
``build:`` is part of the key: editing the ``.vast``, the scenario or a run file never
rebuilds the image.

*Layers.* Every entry is copied in its **own** ``COPY`` layer, and every install *group*
is one ``pip install`` layer, in the order listed — so a change inside group *k* rebuilds
that group's install and everything after it, and never re-copies its siblings. Group so
that what changes often comes **last**:

.. code-block:: yaml

   python_packages:
     - mujoco>=3.0                       # index pin: no context, never invalidated
     - [wheels/assets-0.1.0-...whl,      # ~100 MB of meshes/textures, changes rarely
        wheels/robots-0.1.0-...whl]
     - wheels/my_nodes-0.1.0-...whl      # a few KB of code, changes constantly

With that grouping an edit to ``my_nodes`` reuses the asset layers instead of
reinstalling them. Dependencies no longer constrain the *entries* — a group is resolved
in one pip pass, so its members can be in any order — only the groups, since a group may
depend on one installed before it. Grouping is a caching decision; leaving the list flat
is always correct. pip's download cache is a BuildKit cache mount, so even a rebuilt
layer does not re-download from the index.

On the cluster each build runs in a fresh BuildKit pod, so layer reuse there comes
from a registry-backed cache (``<prefix>/<tag>:buildcache``) rather than a local one;
see :doc:`cluster_execution`.

The ``build:`` section is distinct from the top-level ``plugins:`` field:
``plugins:`` installs *variation-type* packages into the **composer** (before
config generation); ``build:`` bakes code/apt into the **scenario container**
(before execution). They share the ``python_packages`` vocabulary but scope
different environments.

runs
^^^^

**Type:** Integer

**Required:** Yes (unless specified in CLI)

Number of times to execute each run configuration. Multiple runs allow for statistical analysis of results.

.. code-block:: yaml

   execution:
     runs: 20

bt_log
^^^^^^

**Type:** Boolean

**Required:** No (default: ``true``)

Record how the scenario's behaviour tree progressed, as ``behaviors.jsonl`` in each run
directory. ``scenario_execution`` writes the file itself (via ``--bt-log``), so unlike the
rosbag route this replaced it also works for ``mode: base`` runs, and the snapshots topic
no longer has to be recorded into the bag.

**This happens on every run unless you turn it off.** A run whose tree state was not recorded
cannot be explained after the fact, and the file is small — tens to a few hundred KB, beside a
rosbag measured in MB. Set it ``false`` to opt a campaign out.

The file is ingested into the ``behaviors`` table of ``data.db`` — one row per behaviour
status change, plus a full snapshot of the tree at ``timestamp`` 0 so branches that never
executed are still present. Each row carries ``parent_id`` and ``child_index`` (structure),
``tip_id`` (which leaf determined an ancestor's status) and ``osc_file``/``osc_line``
(where in the scenario source the behaviour came from). The Run view's scenario-tree panel
reads this table. See the scenario-execution documentation for the file format.

.. code-block:: yaml

   execution:
     bt_log: false     # only to opt out; recording is the default

.. note::

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

timeout
^^^^^^^

**Type:** Integer (seconds)

**Required:** No

**Applies to:** Both execution lanes.

Maximum wall-clock time (in seconds) allowed for a single run. Because ``timeout`` is
*per run* and one unit of work may pack several runs (see ``runs_per_job``), both lanes
enforce ``timeout * runs_per_job``.

- **Local (Docker Compose):** each compose step is wrapped in ``timeout``, which
  SIGTERMs ``docker compose`` — the same shutdown Ctrl+C triggers, so the scenario gets
  a chance to finish writing its results — and SIGKILLs it 30s later if it ignores that.
  A step killed this way tears its stack down and counts as a **failed** run, so a
  truncated batch cannot pass as a shorter successful one.
- **Cluster (Kubernetes):** sets ``activeDeadlineSeconds`` on the Job spec so Kubernetes
  force-terminates the Job (marking it ``DeadlineExceeded``) when the deadline expires.

If omitted (or ``null``), cluster runs fall back to a **default of 1 hour per run**
(``activeDeadlineSeconds = 3600 * runs_per_job``) so a hung Job is always eventually
killed rather than hanging the campaign indefinitely. Set ``timeout`` explicitly to
override this default. **Local runs have no such fallback**: with no ``timeout`` declared
they remain unbounded, because enforcing a limit the user set is a different decision
from inventing one they did not.

A Job hard-killed on its deadline is logged with ``HARD-KILLED by activeDeadlineSeconds`` in the service log for later analysis.

.. code-block:: yaml

   execution:
     timeout: 3600   # 1 hour per run

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
           rst-export-web --world {inputs[0]} --out {out}
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

resources
^^^^^^^^^

**Type:** Dictionary

**Required:** No

**Applies to:** Local and cluster execution

CPU and memory limits for the main (primary) container. Used by Docker Compose for local runs and by Kubernetes for cluster runs. These values are also exposed as ``AVAILABLE_CPUS`` and ``AVAILABLE_MEM`` environment variables inside the container.

.. code-block:: yaml

   execution:
     resources:
       cpu: 6
       memory: 8Gi

**Available fields:**

- ``cpu`` (Optional): Number of CPU cores (integer), or a per-cluster list
- ``memory`` (Optional): Memory limit (e.g., ``8Gi``, ``4096Mi``), or a per-cluster list

**Per-cluster resource values** are supported when multiple clusters need
different allocations.  See :ref:`cluster-execution` for the full syntax.

.. code-block:: yaml

   execution:
     resources:
       cpu:
         - gcp-c4: 4     # 4 CPUs on the gcp-c4 cluster
         - local:   8     # 8 CPUs when running locally

secondary_containers
^^^^^^^^^^^^^^^^^^^^

**Type:** List of container definitions

**Required:** No

**Applies to:** Local and cluster execution

Additional containers that run alongside the main ``robovast`` container in the same pod (Kubernetes) or Docker Compose stack (local). Use this to run separate processes such as the navigation stack or simulation in dedicated containers, each with its own CPU and memory allocation. All containers share the same network namespace and can communicate via localhost.

Each entry is either a container name (string) or a dictionary with the container name as key and optional ``resources`` as value. All secondary containers use the same Docker image as the main container.

.. code-block:: yaml

   execution:
     resources:
       cpu: 2
     secondary_containers:
     - nav:
         resources:
           cpu: 3
           memory: 4Gi
     - simulation:
         resources:
           cpu: 5
           memory: 8Gi
           gpu: 1

**Per-container resources:**

- ``cpu`` (Optional): Number of CPU cores for this container, or a per-cluster list
- ``memory`` (Optional): Memory limit (e.g., ``4Gi``, ``4096Mi``), or a per-cluster list
- ``gpu`` (Optional): Number of GPUs (enables NVIDIA runtime when set)

Per-cluster lists follow the same syntax as the main ``resources`` field.
See :ref:`cluster-execution` for details.

.. note::

   Secondary containers run the ``secondary_entrypoint.sh`` script and receive ``CONTAINER_NAME`` and ``ROS_LOG_DIR`` environment variables. Ensure your scenario or entrypoint logic handles multiple containers appropriately.

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

Settings applied to the RoboVAST control pod (the pod that orchestrates
the campaign — uploading configs, monitoring jobs, collecting results).

kubernetes.control.node_labels
'''''''''''''''''''''''''''''''

**Type:** Dictionary (label key-value pairs)

**Required:** No

Node selector labels added to the control pod's ``nodeSelector``.
Use this to schedule the orchestration workload on a separate, lighter
node pool so that it does not compete with simulation jobs for resources.

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

- ``rosbags_tf_to_csv``: Convert ROS TF transformations to a ``poses`` CSV, map-relative, one row per frame per sample. ``frames`` is a list of child frame names, or ``all`` for every child frame that resolves against ``map``. ``all`` is for the web :ref:`Run view <run-view>`'s 3D panel, which animates one scene body per frame: a world with people or movable props has a frame per skeleton bone and per prop, and listing them is per-world busywork that adding a prop silently invalidates. ``require`` (list) names the frames that **must** be present — a required frame yielding nothing fails the step, which is how a ground-truth frame missing from one simulator's bags gets caught instead of silently analysed as absent. An explicit ``frames`` list requires itself, so ``frames: all`` is normally paired with ``require``. Note that a frame latched once on ``/tf_static`` before the dynamic chain to ``map`` exists never resolves and so is not written (a body welded in a scene is already baked at that pose, so this costs a viewer nothing).
- ``rosbags_nav2bt_to_csv``: Convert nav2's behavior-tree log (``/behavior_tree_log``, ``nav2_msgs/msg/BehaviorTreeLog``) to a ``nav2_behavior_tree`` CSV — one row per status transition (``timestamp, node_name, uid, previous_status, current_status, event_timestamp``).
  ``uid`` is nav2's per-node id, the only column separating two nodes that share a ``node_name`` (an unnamed ``RecoveryNode`` appears once per instance in a default tree); it is empty for pre-Jazzy message definitions. ``timestamp`` is the bag receive time, i.e. the same clock as every other table (see :ref:`one clock per run <run-clock>`); nav2 stamps its own events from a wall clock even under ``use_sim_time``, so that stamp is kept separately as ``event_timestamp`` rather than used to key the table. The log has no tree topology; pair it with the ``robovast_nav`` plugin's ``nav2_bt_tree`` command (below) to reconstruct the tree. No parameters. Requires ``/behavior_tree_log`` in the scenario's ``bag_record(...)``.
- ``nav2_bt_tree`` (requires the ``robovast_nav`` package): Reconstruct nav2's behavior tree by parsing the BT XML nav2 ran and joining it with the ``nav2_behavior_tree`` transitions, writing a ``nav2_behaviors`` CSV in the same schema as the ``behaviors`` table (so the Run view's tree panel renders it). Required ``bt_xml`` parameter (path, relative to the config dir, to the BT XML — must match ``bt_navigator``'s ``default_nav_to_pose_bt_xml``). List it **after** ``rosbags_nav2bt_to_csv``.
- ``rosbags_to_csv``: Extract a specific set of ROS topics from rosbags to separate CSV files. Required ``topics`` parameter (list of topic names to extract). For each topic one CSV file per bag is written next to the bag, named ``<bag>_<topic>.csv``. (Not for occupancy grids — use ``rosbags_costmap_to_csv``, which stores grids compactly instead of one column per cell.)
- ``rosbags_costmap_to_csv``: Store ``nav_msgs/msg/OccupancyGrid`` frames (nav2 costmaps, the static map) compactly and losslessly for the web :ref:`Run view <run-view>`. Required ``topics`` parameter (list of grid topics, e.g. ``[/map, /global_costmap/costmap, /local_costmap/costmap]``). Writes one ``costmaps`` table row per message: the pose/geometry metadata (resolution, width, height, origin) plus the int8 cells zlib-compressed and base64-encoded. The map's extent in meters is ``width×resolution`` by ``height×resolution``.
- ``rosbags_to_webm``: Convert a ``sensor_msgs/msg/CompressedImage`` topic from ROS bags to WebM video files (VP9 codec). Optional ``topic`` parameter (compressed image topic name, default ``/camera/image_raw/compressed``) and ``fps`` parameter (fallback frame rate when timestamps are unavailable, default ``30``).
- ``rosbags_action_to_csv``: Extract ROS2 action feedback and status messages to two CSV files (``<filename_prefix>_feedback.csv`` and ``<filename_prefix>_status.csv``). Reads ``/<action>/_action/feedback`` and ``/<action>/_action/status`` topics. Nested data is flattened to columns. Required ``action`` parameter (action name, e.g. ``navigate_to_pose``). Optional ``filename_prefix`` parameter (default: ``action_<action>``).
- ``rosbags_rosout_to_csv``: Extract ROS log messages from the ``/rosout`` topic in ROS bags to a CSV file. Optional ``skip_levels`` parameter (list of log levels to skip, e.g. ``[ERROR, FATAL]``).
- ``command``: Execute arbitrary commands or scripts. Requires ``script`` parameter, optional ``args`` parameter (list).
- ``compress``: Create a gzipped tarball (``<name>-<timestamp>.tar.gz``) for each campaign directory; runs on the host (no Docker). Optional ``output_dir`` (default: results directory), ``exclude_dirs`` (directory names to exclude, default ``['.cache']``), ``overwrite`` (if ``false``, skip when a tarball already exists; default ``false``).

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


Evaluation Section
------------------

The ``evaluation`` section defines how run results should be visualized and evaluated.

visualization
^^^^^^^^^^^^^

**Type:** List of dictionaries

**Required:** No

Defines evaluation notebooks for visualization in the evaluation GUI. Each entry creates a tab in the GUI.

Each dictionary can have a custom name and four reserved keys for different evaluation scopes:

- ``run``: Path to Jupyter notebook for analyzing a single run
- ``config``: Path to Jupyter notebook for analyzing all runs of a configuration
- ``batch``: Path to Jupyter notebook for analyzing one batch of a search (the
  configurations a strategy proposed in a single ask/tell round)
- ``campaign``: Path to Jupyter notebook for analyzing all runs in a campaign

.. code-block:: yaml

   evaluation:
     visualization:
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
search mode). Because the search results layout is flat — every batch's
configurations live directly under the campaign root — ``DATA_DIR`` alone cannot
identify the batch, so a batch notebook also receives an injected ``BATCH``
variable (the batch index) and selects its configurations from
``campaign.db``:

.. code-block:: python

   DATA_DIR = ''
   BATCH = None    # injected: the index of the selected batch


Complete Example
----------------

Here's a complete example showing all major configuration options:

.. code-block:: yaml

   version: 1
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
     image: ghcr.io/cps-test-lab/robovast:latest
     runs: 20
     resources:
       cpu: 4
       memory: 8Gi
     secondary_containers:
     - nav:
         resources:
           cpu: 3
           memory: 4Gi
     pre_command: /config/files/prepare_test.sh
     post_command: /config/files/post_command.sh
     run_as_user: 1000
     bt_log: true
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
   evaluation:
     visualization:
     - Analysis:
         run: analysis/analysis_run.ipynb
         config: analysis/analysis_config.ipynb
         campaign: analysis/analysis_campaign.ipynb
