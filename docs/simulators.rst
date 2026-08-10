.. _simulators:

Simulators
==========

RoboVAST does not know what a simulator is. It knows a *shape*: something that runs in a
container, needs an environment, and is either stepped by scenario-execution or runs on
its own. A **backend** fills that shape in, so a campaign names a simulator instead of
assembling one:

.. code-block:: yaml

   execution:
     mode: ros2
     containers:
       simulation:
         backend: robosito              # RoboVAST's: which entry point
         config: worlds/depot.yaml      # robosito's own key
       sut:
         image: ghcr.io/cps-test-lab/robovast:latest
         system_packages: [ros-jazzy-navigation2]
     scenario_file: scenario.osc

Everything a campaign used to restate by hand — the GL libraries, the simulator's
packages, ``ENABLE_X11``, the recording variables, and how the simulator is started at
all — comes from the backend. What is left is which simulator, and which config.

The one thing to know first
---------------------------

**The container the scenario runs in is not necessarily the one under test.** A campaign
declares every container under :ref:`execution.containers <config-containers>`, and three
names have a defined meaning:

===============  =========================================================================
``scenario``     Runs scenario-execution. Every campaign has one.
``simulation``   The simulator.
``sut``          The system under test.
===============  =========================================================================

How many *actual* containers back those three depends on the campaign, and nothing that
addresses one has to know:

.. code-block:: text

   exec_in_container(container="simulation")   # always "where the simulator lives"
   remote("ipc:///ipc/sut")                    # always the system under test

Two shapes
----------

A backend serves one or both, and the campaign's ``execution.mode`` decides which is
used — the shape is *derived*, not a second key to keep consistent with the first.

**Stepped** (``mode: base``)
   scenario-execution owns the loop and calls ``step()``, so the simulator runs **in the
   scenario's process** — ``simulation`` and ``scenario`` resolve to one container. Time
   advances only when the behaviour tree ticks, which makes a run exactly reproducible.
   This is the shape a trial with no ROS in it wants.

**ROS** (``mode: ros2``)
   The simulator runs on its own, publishes ``/clock``, and the scenario observes it over
   ROS — so it gets its own container. No ``SimulationInterface`` is involved, which is
   why a simulator that has none fits here unchanged.

``mode: auto`` is refused when a backend is declared: ``auto`` is resolved *inside* the
container by testing whether ``ros2`` is on ``PATH``, so the same ``.vast`` would get a
different topology in a different image, silently. A backend needs the answer before the
campaign is composed.

Driving the system under test
-----------------------------

A container that declares no ``command`` runs a ``scenario_execution_server``, and a
scenario reaches it with the ``remote()`` modifier — no new actions, and the endpoint is
a constant because the role name is:

.. code-block:: text

   ros_launch('nav2_bringup', 'bringup_launch.py', [...]) with:
       remote("ipc:///ipc/sut")

``/ipc`` is the one address that is identical on both lanes — Compose mounts a tmpfs
volume there, Kubernetes an ``emptyDir`` — so a scenario needs no lane-dependent branch.

.. warning::

   **Do not reach for ``osc.docker``.** Its ``docker_exec`` / ``docker_put`` take a
   ``container:`` parameter and look like the right tool, but they drive the Docker
   daemon: they need a socket mount and do not exist on the Kubernetes lane. A campaign
   built on them works locally and fails in-cluster. Anything that must reach the SUT —
   including writing a file inside it at run time — goes through ``remote()``.

**Only node-free actions can be ``remote()``-modified.** The server passes exactly three
kwargs to an action (``logger``, ``output_dir``, ``tick_period``), so an action that reads
``kwargs['node']`` or ``kwargs['simulation']`` fails at run time with ``KeyError``:

==========================================  ==================================================
Runs remotely                               Stays in the scenario container
==========================================  ==================================================
``ros_launch``, ``ros_run``,                every ROS action — ``check_data``,
``run_process``, ``log``                    ``service_call``, ``bag_record``, ``init_nav2``,
                                            ``nav_to_pose``, ``assert_*`` — and anything
                                            touching the simulation
==========================================  ==================================================

That split is the right boundary rather than a limitation: the scenario *observes and
drives the trial*, and only process management of the SUT crosses. Containers share a
network namespace, so a scenario-side ``check_data('/scan')`` sees the SUT's topics
anyway.

Two further notes on a remote ``ros_launch``: use an absolute ``/config/...`` path (with
an empty ``package_name`` it resolves against the *client's* scenario directory, evaluated
on the *server's* filesystem), and the SUT image must carry the server and the action
plugins used remotely — building it ``FROM`` the RoboVAST base gets both.

Writing a backend
-----------------

Register it in the ``robovast.simulators`` entry-point group, or point at it directly with
a ``.vast``-relative ``<file>.py:<Class>`` ref — the escape hatch when the service
environment does not have the package installed.

.. code-block:: python

   from robovast.common.simulators import SHAPE_ROS, SimulatorBackend

   class GazeboBackend(SimulatorBackend):
       CONFIG_CLASS = GazeboConfig           # its own keys, e.g. `sdf`, `gz_version`
       SUPPORTED_SHAPES = (SHAPE_ROS,)       # no SimulationInterface, so ROS only

       def containers(self, cfg, execution):
           return {"simulation": {"image": f"gz:{cfg.gz_version}",
                                  "command": ["gz", "sim", "-s", "-r", cfg.sdf]}}

Hooks, all optional except as noted:

``CONFIG_CLASS`` / ``SUPPORTED_SHAPES``
   A pydantic model for the backend's own keys, and which shapes it serves. An
   unsupported shape is refused at validation time, naming what *is* supported.
``containers(cfg, execution)``
   Container blocks it contributes, merged **underneath** what the campaign declared, so
   an author always wins.
``simulation_ref(cfg, execution)``
   ``module:Class`` of the ``SimulationInterface`` — stepped shape only, and never called
   otherwise.
``env(cfg, execution)``
   Environment the simulator reads. A campaign's own ``execution.env`` wins over it.
``input_files(cfg, execution)``
   What must travel with the campaign. Return a ``ContainerSpec`` when working it out
   needs the simulator itself.
``produces_run_capture(cfg, execution)``
   Whether runs write the capture a ``scene3d`` panel replays.
``scene_export(cfg, execution, *, world, max_tex_dim, overrides)``
   Command that compiles a world into a web scene descriptor, or ``None``.
``run_state_file(cfg, execution)``
   The run-relative recording a screenshot is rendered from, or ``None``. Whatever the
   backend arranged in ``env`` to be written — one name, so the request and the later
   lookup cannot drift apart.
``simulation_screenshot(cfg, execution, *, state, at, view, focus, camera, size)``
   Command that re-renders **one moment of one run** from a chosen viewpoint, or ``None``.

Showing a run: two questions, two hooks
```````````````````````````````````````

``scene_export`` rebuilds the *geometry* once per world, cached and shared by every run that
used it. ``simulation_screenshot`` renders *one moment of one run* from a viewpoint the caller
picks, which needs the simulator itself — nothing else can put the world back into the state a
recording captured. Both run in the campaign's own pinned image, both return a **command
string** put through ``shlex.split`` with ``{out}`` for the output directory.

``None`` is a normal answer to either, and Gazebo gives it to both: RoboVAST launches it, and a
simulator RoboVAST merely launches cannot be asked to re-render anything. Callers report that
as a capability this campaign's simulator lacks, not as a missing tool.

One asymmetry is deliberate. ``scene_export``'s ``overrides`` are passed through opaquely,
because their serialization really is the simulator's own convention — but the **camera
vocabulary is RoboVAST's**: ``lookat``, ``distance``, ``azimuth``, ``elevation``, plus ``focus``
(frame on a named entity) and ``camera`` (a camera the world defines, which owns its pose and so
excludes the other three). Those four describe an orbit camera in any simulator, and owning them
is what lets one tool description enumerate what is valid instead of sending a caller to read a
simulator's own docs. Unknown keys are rejected, naming the valid ones. A backend may accept
more; it documents that itself.

Quote every value: the return is a *string*, and a vector like ``lookat=1,2,0`` has to survive
``shlex.split`` as one word.

Two rules that are not negotiable
`````````````````````````````````

**A backend must import without its simulator installed.** It is imported in the
long-lived service process, which has no reason to carry a MuJoCo or an Isaac runtime.
Declare strings and container specs; if something genuinely needs the simulator — such as
working out which files a world is made of — return a ``ContainerSpec`` and let it run
*inside the simulator's own image*, which also means the answer comes from the very image
that will run the campaign.

**A backend must be resolvable where the campaign is COMPOSED**, which is not always
where you typed the command: a local run composes in your own environment, but ``vast
serve --attach`` holds only a tunnel and the in-cluster service composes in *its* image.
Three ways to satisfy that, in the order you should reach for them:

1. **Installed in the service environment** — the normal answer, and the only one that
   costs a campaign nothing. ``robosito`` is there by default (see below).
2. **Shipped with the campaign** in ``plugins:``. Installed into ``.robovast_plugins/``
   and put on ``sys.path`` before the build specs are extracted, so a backend really can
   arrive this way; it just means a wheel per campaign.
3. **A ``.vast``-relative file ref**, for a backend not packaged at all.

Nothing in the ``.vast`` shows which of the three is in play, which is why it is stated
here.

The robosito backend
--------------------

Ships as its own distribution (``robovast-sim-robosito``, ``src/robovast_sim_robosito``)
behind the ``robosito`` extra, rather than as part of RoboVAST: the framework must not
*require* a simulator, and robosito must not name a campaign runner, so the coupling is a
third package that depends on neither. ``pip install robovast`` therefore names no
simulator; ``pip install 'robovast[robosito]'`` adds this backend's entry point.

The **service/controller image installs that extra by default**, which is what lets a
campaign say ``backend: robosito`` on a cluster without shipping anything in ``plugins:``.
It is affordable there precisely because the backend is container specs and strings whose
only dependency RoboVAST already has — no MuJoCo enters that image. The simulator itself
lives in the images named below, which the backend only *refers to*.

It serves **both** shapes:

- ``mode: ros2`` — a ``simulation`` container running ``rst sim <config> --ros --headless``
  from robosito's own published image. Nothing a campaign owns contains robosito, so the
  GL packages, the ``mujoco`` pin and the ``rst_*`` package list leave the ``.vast``
  entirely.
- ``mode: base`` — the combined ``robovast_robosito`` image, because a stepped simulator
  shares the scenario's process. Built by ``container/robovast/build.sh --image robosito``.

Its own keys are ``config`` (a world YAML beside the ``.vast``, or a package ref such as
``rst_scenes:depot``) and ``adapter``. It is ``config`` rather than ``world`` because the
file is robosito's whole configuration — physics, plugins, robot, sensors and its
``extends`` chain — and "world" understates what a campaign selects.

.. _sim-channel:

Varying the simulator
---------------------

**A world belongs to a configuration.** The ``simulation`` block above is the campaign-wide
*default*; each configuration resolves its own block over it, and that is what reaches the
run. Not per campaign, and not per run: repetitions of a configuration share a world, which
is what makes them repetitions.

A ``.vast`` reaches those keys through the ``sim:`` channel -- the sibling of ``scenario:``
(see :ref:`variations <config-variation-destination>`):

.. code-block:: yaml

   configuration:
   - name: rooms
     variations:
     - ParameterVariationList:
         sim: config                            # swap the world outright
         values: [world/depot.yaml, world/warehouse.yaml]
     - ParameterVariationDistributionUniform:
         sim: plugins.floorplan.floor.friction  # or vary a value inside it
         min: 0.6
         max: 1.4
     - ParameterVariationList:
         scenario: goal_pose                    # the other channel, unchanged
         values: [...]

**A bare backend key is that key; anything else is a path into the world.** ``sim: config``
selects the world file because ``config`` is one of robosito's keys, while
``sim: plugins.floorplan.floor.friction`` lands under the backend's declared ``DOTTED_ROOT``
(``overrides`` for robosito) -- so the prefix that would say nothing is not written. The
explicit spelling ``sim: overrides.plugins....`` stays valid, and is how a world key that
collided with a backend key would be reached.

**Where a factor lands is decided by when the simulator can still act on it.** MuJoCo does
not recompile mid-run, and robosito's ``simulation_interfaces`` serves no ``SpawnEntity``:
*which* entities exist is settled when the model compiles, and a scenario only moves and
observes them. So the boundary between the two channels is the compile -- not "the world"
versus "the trial", since a world is not static during a run either.

One consequence worth knowing: a count expressed as the *number of plugin entries* cannot be
an override, because ``apply_overrides`` resolves a plugin by name and refuses one matching
nothing. A plugin whose config value is a **list of instances** turns that into an ordinary
override of one value.

What each job gets
``````````````````

Two artifacts, mirroring what the scenario channel already writes:

===============================================  ===========================================
``<campaign>/<config>/_config/sim.config``       the **record** -- the whole resolved block
``<campaign>/_transient/job-<idx>.sim.yaml``     the **input** -- the overrides, mounted at
                                                 ``/config/sim.overrides.yaml``
===============================================  ===========================================

The world stays on argv, so a job's command names it directly::

   rst sim /config/world/depot_nav2.yaml --headless --pacing realtime \
       --override /config/sim.overrides.yaml

The record is not mounted. It is there so that what the simulator was given sits next to the
configuration it belongs to, and so its two values are the arguments that replay the cell by
hand. Everything else about that container -- image, resources, packages -- stays
campaign-level: only that one argv token and that one file differ between jobs.

What is checked before anything runs
`````````````````````````````````````

A ``sim:`` destination is checked against the **backend's** schema at composition -- an unknown
key, or a dotted path whose first segment is also a backend key, is refused there.

What a backend cannot answer is whether ``plugins.floorplan.size`` addresses a plugin *this
world* has: that needs the world's ``extends`` chain resolved, which needs the simulator. A
backend may therefore offer a ``describe_query`` -- a command RoboVAST runs **in the simulator's
own image**, whose one line of JSON names the plugins the world defines. Every override in the
campaign is checked against it, once per distinct block:

.. code-block:: text

   sim override targets no plugin in this world: floorplna.
   The world has: ceiling, floorplan, lidar, spawn_robot

Only the *plugin key* is verified. A key inside a plugin's config that the world leaves at its
default is legitimately absent from what the simulator reports, so refusing it would reject a
correct campaign; a plugin key matching nothing is unambiguous and is what ``apply_overrides``
refuses at load time -- previously after the image pull and the pod schedule.

Two things keep the cost proportionate. A campaign that overrides nothing is not checked, and a
backend offering no ``describe_query`` -- or an environment with no container runner -- is not
checked either. In both cases the campaign behaves exactly as it did before: wrong overrides are
still refused, just later and more expensively.

**Entities the trial drives must be entities the world compiled.** Nothing creates one at run
time -- rst does not recompile mid-run and ``simulation_interfaces`` serves no ``SpawnEntity``
-- so a scenario naming ``obstacle_9`` against a world with four obstacles fails on a service
call, mid-trial. Which parameters name entities is not guessed: the scenario file declares it
(``static_objects: list of spawn_entity``), and the values of those parameters carry
``entity_name``. Checked against the same description, which then also reports the entities the
world compiles -- asked for only when a campaign names any, since answering it means building
the model.

In practice a variation writing both channels keeps them consistent by construction: the
obstacles the trial drives and the ones the world compiles come from one placement, with one
set of names. The check is what catches a hand-authored mismatch.

The same seam answers the staging question. ``input_files`` may also return a query, which is
how a world that ``extends`` **another campaign file** stages its whole chain; a world extending
a *packaged* one, or nothing, is complete in the single file the campaign owns and says so
without starting a container.

**Packing groups by the resolved block.** A job's containers start once and are not restarted
between packed work items, so one job runs one compiled model; ``runs_per_job > 1`` therefore
chunks *within* work items that agree on their simulator settings. A campaign whose
configurations share a world -- every campaign before this existed -- packs exactly as it
always did.

**Transport is the world's, not the campaign's.** RoboVAST passes no middleware flags at
all: which topics a world speaks, under which namespace (``ros2_bridge``, whose config
carries ``tf_namespace``), and whether it serves the ``simulation_interfaces`` control
plane a scenario's ``osc.sim`` actions are clients of, are plugins declared in the world
YAML. A campaign runner configuring a simulator's middleware would be reaching a layer
down; ``--headless`` and ``--pacing`` are the only two the deployment owns.

So a world a ``mode: ros2`` campaign runs **must** declare its own ``ros2_bridge``, plus
``sim_interfaces`` if the scenario touches entities. Opening such a world by hand where no
bridge is installed is ``rst sim <world> --no-communication``, which strips the transport
plugins, so declaring them costs the world nothing.

Developing against a working tree
`````````````````````````````````

The combined image is built from a git pin, which is robosito as *pushed* — not your
working tree. Set ``ROBOVAST_ROBOSITO_SRC`` to a local checkout and the CLI stages it into
the campaign's own image build, so an edit is picked up on the next run. It works on both
lanes (the cluster stages the context to its build bucket) and it keeps provenance honest:
the image digest still describes exactly what ran.
