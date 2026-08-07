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
a ``module:ATTR`` ref or a ``.vast``-relative file — the last is the escape hatch when the
service environment does not have the package installed.

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

Two rules that are not negotiable
`````````````````````````````````

**A backend must import without its simulator installed.** It is imported in the
long-lived service process, which has no reason to carry a MuJoCo or an Isaac runtime.
Declare strings and container specs; if something genuinely needs the simulator — such as
working out which files a world is made of — return a ``ContainerSpec`` and let it run
*inside the simulator's own image*, which also means the answer comes from the very image
that will run the campaign.

**A backend must be installed in the RoboVAST service environment.** The image and
environment hooks run before a campaign's ``plugins:`` are installed, so a backend cannot
arrive that way. On a cluster this means the service image needs it — or the campaign uses
the file-ref form above. Nothing in the ``.vast`` shows this, which is why it is stated
here.

The robosito backend
--------------------

Ships as root-level glue in the ``rrp`` repo (``robovast_sim_robosito``) rather than
inside either project: RoboVAST must not name a simulator and robosito must not name a
campaign runner, so the coupling lives in the repo that already depends on both.

It serves **both** shapes:

- ``mode: ros2`` — a ``simulation`` container running ``rst sim <config> --ros --headless``
  from robosito's own published image. Nothing a campaign owns contains robosito, so the
  GL packages, the ``mujoco`` pin and the ``rst_*`` package list leave the ``.vast``
  entirely.
- ``mode: base`` — the combined ``robovast_robosito`` image, because a stepped simulator
  shares the scenario's process. Built by ``container/robovast/build.sh --image robosito``.

Its own keys are ``config`` (a world YAML beside the ``.vast``, or a package ref such as
``rst_scenes:depot``), ``tf_namespace`` and ``sim_control``. It is ``config`` rather than
``world`` because the file is robosito's whole configuration — physics, plugins, robot,
sensors and its ``extends`` chain — and "world" understates what a campaign selects.

Transport is **not** in that file. A checked-in world stays ROS-free so ``rst sim`` can run
it where the bridge is not installed; the backend asks robosito to append it at load time
instead.

Developing against a working tree
`````````````````````````````````

The combined image is built from a git pin, which is robosito as *pushed* — not your
working tree. Set ``ROBOVAST_ROBOSITO_SRC`` to a local checkout and the CLI stages it into
the campaign's own image build, so an edit is picked up on the next run. It works on both
lanes (the cluster stages the context to its build bucket) and it keeps provenance honest:
the image digest still describes exactly what ran.
