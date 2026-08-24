# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Simulator backends: what RoboVAST knows about a simulator, which is a *shape*.

A campaign names a backend in its ``simulation`` container and the backend fills in what
would otherwise be restated by hand in every ``.vast`` -- the image, the packages it
needs, the environment it reads, how it is started. RoboVAST never names a simulator:
backends are resolved from the ``robovast.simulators`` entry-point group, exactly as
variation types and panel types are.

**Two shapes**, and a backend declares which it serves:

``stepped``
    scenario-execution owns the loop and calls ``step()`` -- so the simulator runs
    *in the scenario's process*, and the two roles resolve to one container. Exactly
    reproducible, because time advances only when the behaviour tree ticks.

``ros``
    the simulator runs on its own, publishes ``/clock``, and the scenario observes it
    over ROS -- so it gets its own container. No ``SimulationInterface`` is involved,
    which is why a simulator that has none (Gazebo, Isaac) fits here unchanged.

**A backend must import without its simulator installed.** It is imported in the
long-lived service process, which has no reason to carry a MuJoCo or an Isaac runtime;
it declares strings and container specs, and anything needing the simulator itself runs
*in the simulator's image* (see :meth:`SimulatorBackend.input_files`).
"""

from __future__ import annotations

from typing import Optional

from robovast.common.config import (ALWAYS_ON_PANELS, SCENARIO_CONTAINER, SIMULATION_CONTAINER,
                                    SUT_CONTAINER, flatten_panel_shorthand)

#: Entry-point group backends register in.
SIMULATOR_GROUP = "robovast.simulators"

#: The simulator is stepped in-process by scenario-execution (``mode: base``).
SHAPE_STEPPED = "stepped"
#: The simulator runs on its own and publishes ``/clock`` (``mode: ros2``).
SHAPE_ROS = "ros"

#: Where RoboVAST mounts a campaign's ``run_files``, in every container of the job. Named
#: here rather than in each backend because it is RoboVAST's layout, not a simulator's --
#: and a backend that guessed it wrong would fail after the image pull and the pod
#: schedule, i.e. at the cost of a whole cell.
CONFIG_MOUNT = "/config"

#: Where RoboVAST mounts the job's resolved simulator overrides, in every container of the
#: job. A constant rather than a per-backend choice for the same reason ``/ipc`` is one: a
#: backend building a command has to name it, and a lane writing the file has to agree.
SIM_OVERRIDES_MOUNT = f"{CONFIG_MOUNT}/sim.overrides.yaml"

#: Where a *query's* overrides document is mounted -- a describe question, not a run. Outside
#: ``CONFIG_MOUNT`` because the campaign tree mounts there and an input nested inside another
#: input's mount has to be copied rather than bound; ``/aux`` because on the cluster a fixed
#: mount is an emptyDir the aux Pod declares, and only ``AUX_MOUNTABLE_PATHS`` are declared. A
#: constant for the same reason :data:`SIM_OVERRIDES_MOUNT` is one: the backend names it on argv
#: and the caller mounts the file there.
SIM_QUERY_OVERRIDES_MOUNT = "/aux/sim.overrides.yaml"

#: Name of the per-configuration record of what the simulator was given, written beside
#: ``scenario.config`` in ``<campaign>/<config>/_config/``. A record, not an input: what
#: the run reads is :data:`SIM_OVERRIDES_MOUNT` plus the world on argv.
SIM_CONFIG_FILE = "sim.config"


def shape_for(mode: str) -> str:
    """Which shape a campaign's ``execution.mode`` implies.

    ``mode`` already answers "does the trial speak ROS?", and that is the same question
    as "can the simulator run in its own container?" -- so the shape is derived rather
    than being a second key an author has to keep consistent with the first.
    """
    return SHAPE_ROS if mode == "ros2" else SHAPE_STEPPED


class SimulatorBackend:
    """What a simulator tells RoboVAST about itself.

    Every hook receives the validated ``simulation`` container block (``cfg``, its own
    :attr:`CONFIG_CLASS` instance) and the resolved ``execution`` mapping, so a backend
    can key on ``mode`` -- which is the only thing that differs between the two shapes
    for most simulators.

    Defaults are the "nothing to add" answers, so a backend implements only what it
    actually has to say.
    """

    #: A pydantic model for this backend's own keys. RoboVAST validates ``backend`` and
    #: hands the rest here, so adding a key to a simulator is not a RoboVAST change.
    CONFIG_CLASS = None

    #: Which of :data:`SHAPE_STEPPED` / :data:`SHAPE_ROS` this backend serves. A request
    #: for another is refused at validation time, naming what is supported -- the
    #: "capability is declared by the plugin, never listed in core" rule.
    SUPPORTED_SHAPES: tuple = (SHAPE_STEPPED, SHAPE_ROS)

    #: Which of this backend's keys a bare dotted ``sim:`` path lands under, or ``None``.
    #:
    #: A campaign varying a simulator almost always varies something *inside* the world
    #: rather than swapping the world itself, so ``sim: plugins.floorplan.size`` should not
    #: have to carry a fixed ``overrides.`` prefix that says nothing. A backend names the
    #: key that prefix would have been. A **bare backend key always wins** -- ``sim: config``
    #: is the world, and a world key that happens to share a backend key's name is reached
    #: by spelling the root out (``overrides.config``), so there is no ambiguity to resolve
    #: and no precedence to remember. With ``None`` there is no short form at all and every
    #: ``sim:`` value must name one of this backend's keys.
    DOTTED_ROOT: Optional[str] = None

    #: Entry-point groups whose *providers* supplied this campaign's assets, as strings.
    #:
    #: A simulator's worlds and models often come from separate distributions resolved by
    #: entry point -- some of them private. A campaign that used them is only reproducible by
    #: someone who can obtain that code, so its results have to record which distribution and
    #: at which commit; without it a published dataset silently depends on something nobody
    #: can name. RoboVAST enumerates whatever a backend lists here and reads each provider's
    #: version and (for a VCS install) its resolved commit, so **core still names no
    #: simulator and no asset repository** -- the backend names its own groups, exactly as it
    #: already declares images and environment strings. Empty means "nothing beyond this
    #: distribution itself", which is the honest answer for a simulator with no asset plugins.
    ASSET_ENTRY_POINT_GROUPS: tuple = ()

    def sim_document(self, cfg, execution: dict) -> Optional[dict]:
        """The part of *cfg* that travels as a file rather than on the command line.

        Written per job to :data:`SIM_OVERRIDES_MOUNT` and read by whatever
        :meth:`containers` puts in argv. It exists because the values a campaign varies are
        structured -- a nested override tree, not a scalar -- and serialising that onto a
        command line loses it to quoting, hides it from the results, and gives a human no
        way to replay the cell.

        ``None`` -- the default -- means this backend has no file form and everything it
        needs is already on argv; no file is written and nothing is mounted.
        """
        return None

    def containers(self, cfg, execution: dict) -> dict:
        """Container blocks this backend contributes, keyed by container name.

        Merged *underneath* what the campaign declared, so an author always wins. This
        is where a backend supplies its default image, and -- in the ``ros`` shape --
        the command that starts the simulator in its own container.
        """
        return {}

    def simulation_ref(self, cfg, execution: dict) -> Optional[str]:
        """``module:Class`` of the ``SimulationInterface``, for the stepped shape.

        ``None`` in the ``ros`` shape, and for any simulator that has no such interface
        at all -- the hook is simply not called there.
        """
        return None

    def env(self, cfg, execution: dict) -> dict:
        """Environment the simulator reads, merged into every container.

        A campaign's own ``execution.env`` wins over this: these are defaults a backend
        knows, not decisions it takes away.
        """
        return {}

    def describe_query(self, cfg, execution: dict, *, entities: bool = False,
                       targets: str = "") -> Optional["ContainerQuery"]:
        """A query describing what this world *provides*, or ``None``.

        The counterpart to :meth:`input_files`, which describes what it needs. RoboVAST uses
        it to check a campaign's overrides before any compute is spent, so the reply shape is
        RoboVAST's contract::

            {"plugins": [{"address": "robot.lidar", "paths": ["components.robot.lidar.rays"]}],
             "addresses": ["robot", "robot.lidar"]}

        Only ``addresses`` is checked, and it is the set that simulator's own resolution accepts.
        A *path* a world leaves at its default is legitimately absent from ``paths``, so an
        unlisted one is unverifiable rather than wrong -- but an address matching nothing is
        refused at load time, which is the error worth catching before an image pull.

        An address is itself a path (``robot.lidar``), not a single name: a component may live
        inside another, and a sensor a model's manifest supplies -- the thing a campaign most often
        wants to sweep -- is only reachable that way.

        *entities* asks for ``{"entities": [...]}`` as well -- the names the world compiles.
        It is separate because answering it means building the model, which a caller only
        checking override targets should not pay for.

        *targets* asks the simulator to also report, for the objects whose names match that
        glob, which of its parameters a run may **override** and what they are now -- the
        question a caller *holding* an override has to answer before it can write one. Like
        *entities* it costs a model build, and like *entities* the vocabulary is the
        simulator's: RoboVAST only requires the shape ``{"overridable": {"fields": [...],
        "targets": {...}}}``, where a field row says what it does and how it can silently do
        nothing. A backend with no such notion reports nothing there.

        **Run it in the image the campaign runs**, not in a default one. Which world a ref
        resolves to is a property of what is *installed*, so an experiment shipping its own
        world package (``python_packages``) has worlds that exist in its built image and
        nowhere else -- described against a base image, that ref simply does not resolve, and
        the check silently passes for exactly the campaigns most in need of it.

        ``None`` -- the default -- means this backend cannot describe a world, and its
        campaigns are simply not pre-checked.
        """
        return None

    def input_files(self, cfg, execution: dict, vast_dir: str) -> list:
        """Files the simulator needs that the campaign owns, relative to the ``.vast``.

        *vast_dir* is the directory the ``.vast`` lives in, and it is required because the
        paths in *cfg* are relative to it and to nothing else. A backend that reads one --
        to decide whether a world inherits from another campaign file, say -- must resolve
        it against this, never against the process's working directory: composition runs
        from the CLI's cwd, from a service worker, and from an isolated subprocess, and a
        backend that guessed made the same campaign answer differently in each.

        Typically a world declared as a path rather than a package ref. A packaged world
        travels inside the image and needs nothing here, which is the default.

        RoboVAST adds these to the campaign's ``run_files``, so each is mounted at
        ``/config/<path>`` where the simulator opens it, archived into
        ``<campaign>/_config/`` where the run view rebuilds geometry from it, and hashed
        into the configuration identity -- a changed world is a changed experiment.

        Declared here rather than written by the campaign because a ``.vast`` naming its
        world under ``config:`` and again under ``run_files:`` states one fact twice, and
        forgetting the second fails far from the cause: the simulator cannot open a path
        that was never mounted.

        Only the files the campaign itself owns. A world that ``extends`` another
        *campaign* file cannot be followed here -- enumerating that needs the simulator,
        which must not be imported in this process -- so a backend that can state the
        question returns a :class:`ContainerQuery` for it instead.
        """
        return []

    def produces_run_capture(self, cfg, execution: dict) -> bool:
        """Whether runs write the capture a ``scene3d`` panel replays.

        Replaces sniffing a campaign's wheel names for ``roqsim``: a capability question
        the simulator can answer, asked of whichever simulator is actually configured.
        """
        return False

    def default_panels(self, cfg, execution: dict) -> list:
        """Run-view panels this backend contributes, as ``{<type>: <props>}`` entries.

        The same reasoning as :meth:`env`: a campaign whose runs record a capture always wants
        the panel that replays it, so there is nothing for the ``.vast`` to decide and nothing
        it should have to write. A backend that produces no such artifact returns ``[]`` --
        Gazebo has no scene-descriptor export, so it has no 3D panel to offer and must not
        claim one.

        What *every* campaign gets regardless of simulator is
        :data:`~robovast.common.config.ALWAYS_ON_PANELS`, not this: a backend contributes only
        what it alone knows.

        Contributed *defaults*: a campaign that declares the same panel type itself keeps its
        own entry, exactly as ``execution.env`` wins over :meth:`env`. A backend supplies what
        it knows, not decisions it takes away.
        """
        return []

    def scene_export(self, cfg, execution: dict, *, world: str, max_tex_dim: int,
                     overrides: dict, overrides_file: Optional[str] = None) -> Optional[str]:
        """Command that compiles *world* into a web scene descriptor, or ``None``.

        Returned as a **string**, run through ``shlex.split`` by the ``shell`` input
        generator, with ``{out}`` for the output directory. It runs in the campaign's own
        simulator image, so it may name that image's tools.

        Companion to :meth:`produces_run_capture`: one says a run records the motion a
        ``scene3d`` panel replays, this one says the geometry it is replayed against can be
        rebuilt. ``None`` -- the default -- means this backend has no exporter, which is a
        normal answer: Gazebo has none.

        The descriptor *format* is RoboVAST's (``scene.json`` + ``scene.bin``, and a
        ``.generated.json`` manifest; see ``docs/run_capture.rst``), so a second backend
        implements against it rather than inventing one. What belongs here is only the
        command: which tool, and how it spells its arguments -- ``overrides`` in particular,
        whose serialization is the simulator's own convention.

        ``overrides_file`` is the same mapping already written as YAML and staged where the
        command can read it, at that absolute in-container path. Prefer it: argv is the wrong
        channel for a nested tree, and flattening one loses it to quoting -- a list of
        obstacle instances reached an exporter as ``KeyError: '"pos"'``, which surfaces only
        when somebody opens the run view. ``None`` when there are no overrides.
        """
        return None

    def health_command(self, cfg, execution: dict, *, run_dir: str) -> Optional[str]:
        """Command that reports whether a **live** run is healthy, and where everything is.

        The other side of :meth:`simulation_screenshot`: that renders a moment of a *finished*
        recording, this asks a run that is still going what state it is in. Only the simulator
        can answer either, and only it knows what its own records are called -- which is why the
        command is named here rather than assembled by the service from a second copy of those
        names.

        Expected to print JSON on stdout and to be cheap enough to poll: the service runs it
        while a campaign is live, so a command that folds a whole file is the wrong shape for
        this hook.

        ``None`` -- the default -- is a normal answer, and the same kind Gazebo gives to
        :meth:`scene_export`: a simulator RoboVAST merely launches cannot be asked how it is
        doing. The caller reports that as a capability this campaign's simulator lacks, never as
        a healthy run.

        *run_dir* is where this run's records are **inside the container** (``/out/<config>/<run>``
        on both lanes). Returned as a **string** run through ``shlex.split``, as
        :meth:`simulation_screenshot` is, so a backend writes the command it would type.
        """
        return None

    def run_state_file(self, cfg, execution: dict) -> Optional[str]:
        """The run-relative recording :meth:`simulation_screenshot` renders from, or ``None``.

        A backend that sets up a recording (roqsim asks for one in :meth:`env`) is the only
        thing that knows what the file is called, so it says so here rather than the service
        keeping a second copy of that name. ``None`` -- the default -- goes with a backend that
        records nothing, which is also one that cannot re-render.
        """
        return None

    def simulation_screenshot(self, cfg, execution: dict, *, state: str,
                              at: Optional[float], view: dict, focus: list,
                              camera: Optional[str], size: str) -> Optional[str]:
        """Command that renders one frame of a recorded run, or ``None``.

        The other half of what a simulator can be asked to show. :meth:`scene_export` rebuilds
        the *geometry* once per world; this renders **one moment of one run, from a viewpoint
        the caller picks** -- which needs the simulator itself, because nothing else can put
        the world back into the state the recording captured.

        ``None``, the default, is a normal answer and the same one Gazebo gives to
        :meth:`scene_export`: a simulator RoboVAST merely launches cannot be asked to re-render
        anything. The caller reports that as a capability this campaign's simulator lacks.

        Returned as a **string** run through ``shlex.split``, with ``{out}`` for the output
        directory (write the image as ``{out}/frame.png``) and *state* already spelled as the
        generator's input placeholder -- so it can be dropped into the command verbatim.

        Args:
            state: The run's recording, as a placeholder the input generator substitutes.
            at: Simulated seconds to render, or ``None`` for the recording's last sample.
            view: Camera pose. **RoboVAST's vocabulary, not the backend's** --
                :data:`VIEW_KEYS`, already validated. Unlike :meth:`scene_export`'s
                ``overrides``, whose serialization really is the simulator's own convention, a
                viewpoint is the same idea everywhere and a caller should not have to learn one
                spelling per simulator. Map these onto whatever the tool calls them.
            focus: Entity or body names to frame on, letting the simulator choose the angle.
            camera: A camera the world itself defines. It owns its pose, so callers are
                refused if they combine it with *view*/*focus* before reaching here.
            size: ``WxH``.
        """
        return None


#: The camera pose a screenshot may ask for, in RoboVAST's terms. Deliberately small and
#: deliberately generic: these four describe an orbit camera in any simulator, which is what
#: lets one tool description enumerate what is valid instead of sending a caller to a
#: simulator's own docs. A backend may accept more and documents that itself.
VIEW_KEYS: frozenset = frozenset({"lookat", "distance", "azimuth", "elevation"})


def parse_view(pairs) -> dict:
    """``["azimuth=90", "lookat=1,2,0"]`` -> ``{"azimuth": "90", "lookat": "1,2,0"}``.

    Rejects an unknown key by **naming the valid ones**, because the caller here is usually a
    model reading an error message, and "unknown key" without the vocabulary is a guess-again.
    Values stay strings: what a backend does with ``1,2,0`` is its own business.
    """
    out: dict = {}
    for pair in pairs or []:
        key, sep, value = str(pair).partition("=")
        key = key.strip()
        if not sep:
            raise ValueError(
                f"view entry {pair!r} is not key=value; the keys are "
                f"{', '.join(sorted(VIEW_KEYS))}")
        if key not in VIEW_KEYS:
            raise ValueError(
                f"unknown view key {key!r}; the keys are {', '.join(sorted(VIEW_KEYS))}")
        out[key] = value.strip()
    return out


def run_state_filename(execution: dict, base_dir: str = "") -> Optional[str]:
    """The configured backend's :meth:`SimulatorBackend.run_state_file`, or ``None``."""
    if not (name := backend_name(execution or {})):
        return None
    backend = resolve_backend(name, base_dir)
    block = ((execution.get("containers") or {}).get(SIMULATION_CONTAINER) or {})
    return backend.run_state_file(_validated_cfg(backend, block, name), execution)


def health_command(execution: dict, *, run_dir: str, base_dir: str = "") -> Optional[str]:
    """The configured backend's :meth:`SimulatorBackend.health_command`, or ``None``.

    Same seam as :func:`simulation_screenshot_command`, and ``None`` means the same kind of
    thing: this campaign's simulator does not report its own health, rather than the run being
    fine. **Core names no simulator here** -- the backend names its own command, exactly as it
    already names its images and its recording.
    """
    if not (name := backend_name(execution or {})):
        return None
    backend = resolve_backend(name, base_dir)
    block = ((execution.get("containers") or {}).get(SIMULATION_CONTAINER) or {})
    cfg = _validated_cfg(backend, block, name)
    return backend.health_command(cfg, execution, run_dir=run_dir)


def simulation_screenshot_command(execution: dict, *, state: str, at: Optional[float],
                                  view: dict, focus: list, camera: Optional[str],
                                  size: str, base_dir: str = "") -> Optional[str]:
    """The configured backend's :meth:`SimulatorBackend.simulation_screenshot`, or ``None``.

    Same seam as :func:`scene_export_command`, and ``None`` means the same kind of thing:
    this campaign's simulator does not render views, rather than a tool being missing.
    """
    if not (name := backend_name(execution or {})):
        return None
    backend = resolve_backend(name, base_dir)
    block = ((execution.get("containers") or {}).get(SIMULATION_CONTAINER) or {})
    cfg = _validated_cfg(backend, block, name)
    return backend.simulation_screenshot(cfg, execution, state=state, at=at, view=view,
                                         focus=focus, camera=camera, size=size)


def scene_export_command(execution: dict, *, world: str, max_tex_dim: int,
                         overrides: dict, base_dir: str = "",
                         overrides_file: Optional[str] = None) -> Optional[str]:
    """The configured backend's :meth:`SimulatorBackend.scene_export`, or ``None``.

    The seam that keeps one simulator's exporter out of the service: what a campaign's
    geometry is built with is the backend's answer, and the backend is one the campaign
    already names. Unresolvable or silent backend -> ``None``, which the caller reports as
    "this campaign's simulator builds no geometry" rather than as a missing tool.

    The frozen config is validated like any other -- a campaign whose ``.vast`` names a key
    the backend has since retired does not render, and says which key. Geometry is rebuilt
    with today's code, so a config today's code cannot read is not one to guess at.
    """
    if not (name := backend_name(execution or {})):
        return None
    backend = resolve_backend(name, base_dir)
    block = ((execution.get("containers") or {}).get(SIMULATION_CONTAINER) or {})
    cfg = _validated_cfg(backend, block, name)
    return backend.scene_export(cfg, execution, world=world, max_tex_dim=max_tex_dim,
                                overrides=overrides, overrides_file=overrides_file)


def merge_default_panels(raw_panels: list, execution: dict, base_dir: str = "") -> list:
    """*raw_panels* with the contributed panels prepended: the always-on set plus the
    configured backend's own.

    Two sources, one rule. :data:`~robovast.common.config.ALWAYS_ON_PANELS` is what no campaign
    can do without (the ``playback`` transport); a backend adds what it alone knows it always
    records (roqsim's ``scene3d``). Merged here, where the service reads the list, rather than in
    the web UI -- doing it there would mean the run view showing a panel the served list does not
    contain.

    Precedence follows ``execution.env``'s documented rule: a campaign that declares the type
    itself keeps its own entry, at its own position and with its own props. Contributed panels go
    *first* because ``playback`` docks flush against the bottom edge (the first ``bottom`` bar in
    the list takes the edge) and ``scene3d`` is the full-bleed base layer the others float over --
    which is also the order a ``.vast`` wrote them in by hand.

    A backend that cannot be resolved contributes nothing rather than failing: the panel list is
    read to *show* a campaign, including one whose backend package is not installed here. The
    always-on set stands either way -- an unresolvable backend must not take the transport bar
    with it.
    """
    contributed = list(ALWAYS_ON_PANELS)
    if name := backend_name(execution or {}):
        try:
            backend = resolve_backend(name, base_dir)
            cfg = ((execution.get("containers") or {}).get("simulation") or {})
            contributed += backend.default_panels(cfg, execution) or []
        except Exception:  # pylint: disable=broad-except
            pass

    # Through `flatten_panel_shorthand` rather than a walk of its own, so "which type is this
    # entry" cannot come to mean one thing here and another where the list is validated or served.
    declared = {flatten_panel_shorthand(entry).get("type") for entry in raw_panels
                if isinstance(entry, (str, dict))}
    extra = [p for p in contributed
             if flatten_panel_shorthand(p).get("type") not in declared]
    return extra + list(raw_panels)


def resolve_backend(name: str, base_dir: str = "") -> SimulatorBackend:
    """Load a backend by entry-point name, or by ``.vast``-relative ``<file>.py:<Class>``.

    The file form is the escape hatch for a campaign whose service environment does not
    have the backend installed: a descriptor next to the ``.vast`` works without any
    deployment step. Same resolution as variation plugins (``plugin_ref.load_ref``), so
    there is one spelling to learn.
    """
    from robovast.common.plugin_ref import load_ref
    loaded = load_ref(name, SIMULATOR_GROUP, base_dir)
    backend = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(backend, SimulatorBackend):
        raise ValueError(
            f"simulator backend '{name}' is a {type(backend).__name__}, not a "
            "SimulatorBackend; it must subclass "
            "robovast.common.simulators.SimulatorBackend")
    return backend


def backend_name(execution: dict) -> Optional[str]:
    """The backend a campaign's ``execution`` names, if any."""
    containers = execution.get("containers") or {}
    block = containers.get(SIMULATION_CONTAINER)
    return (block or {}).get("backend") if isinstance(block, dict) else None


def apply_backend(execution: dict, base_dir: str = "") -> dict:
    """Return *execution* with its backend's contributions merged in.

    Called once, where the raw ``execution`` mapping is turned into what the lanes read,
    so every consumer downstream -- the container plan, the image builds, the env --
    sees one already-complete picture rather than each re-asking the backend.

    The campaign always wins: a backend supplies defaults for keys the author left out,
    and never overrides one they set.
    """
    name = backend_name(execution)
    if not name:
        return execution
    backend = resolve_backend(name, base_dir)
    shape = shape_for(execution.get("mode", "auto"))
    if shape not in backend.SUPPORTED_SHAPES:
        raise ValueError(
            f"simulator backend '{name}' does not support the "
            f"{'ROS' if shape == SHAPE_ROS else 'stepped'} shape that execution.mode "
            f"'{execution.get('mode')}' implies; it supports: "
            + ", ".join(sorted(backend.SUPPORTED_SHAPES)))

    containers = {name_: dict(block or {})
                  for name_, block in (execution.get("containers") or {}).items()}
    cfg = _validated_cfg(backend, containers.get(SIMULATION_CONTAINER) or {}, name)

    if shape == SHAPE_STEPPED:
        # A stepped simulator IS the scenario container -- so collapse the two blocks
        # here, where the shape is known, rather than leaving the container planner to
        # infer it. Container-level keys move to ``scenario``; ``simulation`` keeps only
        # ``backend`` and the backend's own keys, which is what marks it as folded.
        #
        # The author's keys move *first*, so a backend default cannot outrank an image
        # set on the ``simulation`` block -- the block they would naturally reach for,
        # since that is where they named the backend in the first place.
        sim_block = containers.get(SIMULATION_CONTAINER) or {}
        scenario_block = containers.setdefault(SCENARIO_CONTAINER, {})
        for key in _CONTAINER_KEYS:
            if sim_block.get(key) is not None:
                _set_if_unset(scenario_block, key, sim_block.pop(key))

    for target, defaults in backend.containers(cfg, execution).items():
        block = containers.setdefault(target, {})
        for key, value in (defaults or {}).items():
            _set_if_unset(block, key, value)

    result = dict(execution)
    result["containers"] = containers
    if shape == SHAPE_STEPPED and not result.get("simulation"):
        ref = backend.simulation_ref(cfg, execution)
        if ref:
            result["simulation"] = ref
    contributed = backend.env(cfg, execution)
    if contributed:
        result["_backend_env"] = contributed
    return result


class ContainerQuery:
    """A question only the simulator can answer, and where to run it.

    :meth:`SimulatorBackend.input_files` may return one of these instead of a list. RoboVAST
    runs *command* in *spec*'s image, reads **one line of JSON** from its stdout, and uses the
    answer -- so the backend states the question without importing the simulator, and the
    answer comes from the very image that will run the campaign.

    The reply shape is RoboVAST's contract rather than any simulator's, so a second backend
    satisfies it without anyone editing this file::

        {"packaged": false, "inputs": ["/abs/path/one", "/abs/path/two"]}

    ``packaged: true`` means the files arrive with something already installed in the image
    and nothing has to travel. ``inputs`` are absolute paths **as the container sees them**;
    the runner mounts the campaign's directory at its own path, so anything under it is a file
    the campaign owns and everything else came with the image.

    *documents* is ``{container path: mapping}`` the query needs mounted as YAML -- an override
    tree, which argv cannot carry. It travels on the query rather than being re-derived by the
    caller, because the path the command names and the path the file is mounted at have to be the
    same string, and two places deciding it separately is a mismatch nothing detects until the
    simulator reports a file it cannot find.
    """

    __slots__ = ("spec", "command", "documents")

    def __init__(self, spec, command, documents=None):
        self.spec = spec
        self.command = list(command)
        self.documents = dict(documents or {})


#: Sentinel kept out of the public surface; see ``sim_override_paths``.
DOTTED_ROOT_UNSET = object()


#: The keys a simulator's override document holds its component list under. ``plugins`` is roqsim's
#: former spelling of ``components``, still accepted there and so still possible here.
_COMPONENT_ROOTS = ("components", "plugins")


def sim_override_paths(backend: SimulatorBackend, block: dict) -> set:
    """The component paths a resolved ``sim`` block's overrides address, as segment tuples.

    ``overrides.components.robot.lidar.rays`` -> ``("robot", "lidar", "rays")``. The whole path,
    because a component's ADDRESS is a path too (``robot.lidar``) and is not a fixed number of
    segments -- the first one alone told you nothing once a sensor could live inside a robot.
    A caller pairs these with the addresses a simulator publishes; see :func:`unknown_override_paths`.

    Dotted keys are split, because an override document may spell one address either way and both
    mean the same assignment where it is applied.
    """
    root = getattr(backend, "DOTTED_ROOT", None)
    if not root:
        return set()
    tree = (block or {}).get(root) or {}
    for key in _COMPONENT_ROOTS:
        node = tree.get(key)
        if isinstance(node, dict):
            return {tuple(p) for p in _leaf_paths(node)}
    return set()


def _leaf_paths(node, prefix=()):
    """Every root-to-leaf path through *node*, with dotted keys split into segments."""
    if isinstance(node, dict) and node:
        for key, value in node.items():
            yield from _leaf_paths(value, prefix + tuple(str(key).split(".")))
        return
    yield prefix


def unknown_override_paths(paths: set, addresses) -> list:
    """Which of *paths* no published address accounts for.

    An address accounts for a path when its segments are a PREFIX of it: everything after the
    address is a path into that component's config, and a key absent from the world may still be
    valid (a plugin accepts keys its world leaves at the default). So the check is on the half a
    simulator refuses outright -- the address -- and nothing more.

    That deliberately mirrors the acceptance set of the simulator's own resolution rather than being
    stricter than it. A path under a real address is accepted here even if the next segment names no
    component, because the simulator will accept it too: it is then a config key, and the two are not
    distinguishable from either side. Being stricter would turn a working campaign into a refused
    one, which is worse than the mistake it would catch.
    """
    known = [tuple(a.split(".")) for a in addresses]
    return sorted(
        ".".join(p) for p in paths if not any(p[: len(a)] == a for a in known if a)
    )

def simulator_image(execution: dict, declared: Optional[dict] = None) -> str:
    """The image this execution's simulator runs in: the campaign's, else the backend's own.

    One place, and it has to be, because :func:`apply_backend` applies the same precedence to the
    *run* -- the author's ``image:`` on the block outranks a backend default -- and a second copy
    would be free to disagree with it. A backend asking the simulator a question
    (:meth:`SimulatorBackend.describe_query`, :meth:`~SimulatorBackend.input_files`) passes its own
    ``containers()`` as *declared* and gets the answer for free, rather than re-deriving which
    container the simulator runs in for this shape.

    Getting it wrong is the silent kind of wrong: a world ref that resolves only in the campaign's
    built image does not resolve in a default one, so the simulator answers nothing and whatever
    the answer was for quietly does not happen.
    """
    order = ([SIMULATION_CONTAINER, SCENARIO_CONTAINER]
             if shape_for((execution or {}).get("mode", "auto")) == SHAPE_ROS
             else [SCENARIO_CONTAINER, SIMULATION_CONTAINER])
    for source in ((execution or {}).get("containers") or {}, declared or {}):
        for name in order:
            image = ((source.get(name) or {}).get("image") or "").strip()
            if image:
                return image
    return ""


def campaign_sim_block(execution: dict) -> dict:
    """The backend's own keys as the ``.vast`` declared them -- the campaign default.

    Container-level keys (``image``, ``resources``, ...) are RoboVAST's and stay
    campaign-level; what is left describes the simulator and is what a configuration may
    overlay.
    """
    block = ((execution.get("containers") or {}).get(SIMULATION_CONTAINER) or {})
    if not isinstance(block, dict):
        return {}
    return {k: v for k, v in block.items()
            if k not in _ROBOVAST_KEYS and v is not None}


def flatten_sim_block(block, prefix: str = "") -> dict:
    """Nested ``sim:`` mapping -> ``{dotted path: leaf}``.

    A configuration entry carries exactly one shape for this channel -- a flat mapping of
    destination to value -- whether the value was authored as a nested ``sim:`` block or
    written by a variation as a dotted path. Two shapes on one key is how a reader ends up
    guessing which one they are looking at.
    """
    out = {}
    for key, value in (block or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten_sim_block(value, f"{path}."))
        else:
            out[path] = value
    return out


def backend_own_keys(backend: SimulatorBackend) -> Optional[set]:
    """The key names a backend's ``CONFIG_CLASS`` declares, or ``None`` if it has none."""
    model = getattr(backend, "CONFIG_CLASS", None)
    fields = getattr(model, "model_fields", None) if model is not None else None
    return set(fields) if fields else None


def resolve_sim_path(backend: SimulatorBackend, path: str, name: str = "") -> tuple:
    """A ``sim:`` destination -> the key path it addresses in the backend's block.

    A bare backend key is that key; anything else lands under the backend's
    :attr:`~SimulatorBackend.DOTTED_ROOT`. A backend that declares no root has no short
    form, so an unrecognized first segment is refused naming the keys that exist -- the
    alternative being a campaign that composes cleanly and fails after the image pull.
    """
    parts = tuple(p for p in str(path).split(".") if p)
    if not parts:
        raise ValueError(
            f"simulator backend '{name}': a sim: destination cannot be empty")
    own = backend_own_keys(backend)
    if own is None or parts[0] in own:
        return parts
    root = getattr(backend, "DOTTED_ROOT", None)
    if root is None:
        raise ValueError(
            f"'{path}' is not a key of simulator backend '{name}'; its keys are: "
            + ", ".join(sorted(own)))
    if root not in own:
        raise ValueError(
            f"simulator backend '{name}' declares DOTTED_ROOT '{root}', which is not one "
            "of its own keys: " + ", ".join(sorted(own)))
    return (root,) + parts


def _deep_set(target: dict, path: tuple, value) -> None:
    """Set *value* at *path*, creating the intermediate mappings it names."""
    node = target
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _namespace_sim_value(value, deploy_paths: set, prefix: str):
    """Rewrite a value naming a per-config generated file to its path in the container.

    The same test the scenario channel uses (:func:`~robovast.common.execution._namespace_file_params`):
    a value is a file *because it equals a staged deploy path*, not because anyone declared
    the key file-valued -- so no backend has to say which of its keys hold paths.

    **Absolute**, unlike the scenario channel's, because a scenario resolves a file
    parameter against its own directory while a simulator is a separate process with an
    unrelated working directory.
    """
    if isinstance(value, dict):
        return {k: _namespace_sim_value(v, deploy_paths, prefix)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_namespace_sim_value(v, deploy_paths, prefix) for v in value]
    if isinstance(value, str) and value in deploy_paths:
        return f"{prefix}/{value}"
    return value


def merge_sim_block(execution: dict, sim_values=None, base_dir: str = "", *,
                    deploy_paths=None, config_name: str = "") -> dict:
    """Campaign default + one configuration's ``sim`` values -> one validated block.

    *sim_values* is the flat ``{dotted destination: value}`` a configuration carries. The
    campaign's own ``simulation`` block is flattened the same way, so the two merge as one
    kind of thing and a per-config value simply wins.

    Returns a plain mapping -- the resolved block, recorded on the configuration and later
    written to ``sim.config``. Raises naming the backend if the result is not valid for it.
    """
    name = backend_name(execution or {})
    if not name:
        return {}
    backend = resolve_backend(name, base_dir)

    merged: dict = {}
    for path, value in flatten_sim_block(campaign_sim_block(execution)).items():
        _deep_set(merged, resolve_sim_path(backend, path, name), value)
    for path, value in (sim_values or {}).items():
        _deep_set(merged, resolve_sim_path(backend, path, name), value)

    if deploy_paths:
        merged = _namespace_sim_value(
            merged, set(deploy_paths), f"{CONFIG_MOUNT}/{config_name}")

    cfg = _validated_cfg(backend, merged, name)
    dump = getattr(cfg, "model_dump", None)
    return dump(exclude_none=True) if dump else dict(cfg)


def sim_input_files(execution: dict, block: dict, base_dir: str = "",
                    run_query=None) -> list:
    """:meth:`SimulatorBackend.input_files` for one resolved ``sim`` block.

    Asked once per **distinct** block rather than once per campaign, because a campaign
    that varies its world has several and every one has to travel: a world that is not
    staged is a run that cannot start, discovered after the image pull.
    """
    name = backend_name(execution or {})
    if not name:
        return []
    backend = resolve_backend(name, base_dir)
    cfg = _validated_cfg(backend, dict(block or {}), name)
    declared = backend.input_files(cfg, execution, base_dir)
    if isinstance(declared, ContainerQuery):
        # Answering needs the simulator's image. Composition passes *run_query* because it
        # owns the runner factory; validation and previews do not, and start no containers --
        # they report nothing rather than a partial list, since a partial list of what must
        # travel reads as a complete one.
        return list(run_query(declared) or []) if run_query else []
    return [str(p) for p in (declared or [])]


def sim_job_overlay(execution: dict, block: dict, base_dir: str = "") -> dict:
    """What one job's resolved ``sim`` block contributes: ``{command, env, document}``.

    :func:`apply_backend`'s per-job twin, and deliberately the *same* hooks: the command and
    the environment come from :meth:`~SimulatorBackend.containers` and
    :meth:`~SimulatorBackend.env` invoked with this job's block, so a backend cannot answer
    one thing at composition and another at dispatch.

    ``document`` is what the lane writes to :data:`SIM_OVERRIDES_MOUNT`, or ``None``.
    """
    empty = {"command": None, "env": {}, "document": None}
    name = backend_name(execution or {})
    if not name:
        return empty
    backend = resolve_backend(name, base_dir)
    cfg = _validated_cfg(backend, dict(block or {}), name)
    contributed = backend.containers(cfg, execution) or {}
    sim_container = contributed.get(SIMULATION_CONTAINER) or {}
    return {
        "command": sim_container.get("command"),
        "env": backend.env(cfg, execution) or {},
        "document": backend.sim_document(cfg, execution),
    }


def _set_if_unset(block: dict, key: str, value) -> None:
    """Fill ``key`` when the block does not really carry a value for it.

    Not ``setdefault``: an unset optional field survives ``model_dump()`` as an explicit
    ``None``, so the key is *present* and ``setdefault`` declines -- and every backend
    default is silently dropped. Only callers holding a validated config hit that (the
    run path passes the raw YAML, where an unset key is simply absent), which is how a
    build could be planned against ``image: None`` while the run used the backend's
    image. Treating ``None`` as "unset" makes both callers agree.
    """
    if block.get(key) is None:
        block[key] = value


def _validated_cfg(backend: SimulatorBackend, block: dict, name: str):
    """Validate the backend's own keys against its ``CONFIG_CLASS``.

    Without one the block is handed over as-is: a backend with no keys of its own should
    not have to declare an empty model to say so.

    ``None`` values are dropped, matching :func:`sim_overlay_keys`. A container model field
    the author left unset arrives here as an explicit ``None``, and a CONFIG_CLASS forbidding
    extras cannot tell that from a key someone typed -- so a field added to the container
    model would break every campaign of every backend until its name reached
    :data:`_ROBOVAST_KEYS`. Dropping unset values makes that a naming slip rather than an
    outage.
    """
    own = {k: v for k, v in block.items() if k not in _ROBOVAST_KEYS and v is not None}
    if backend.CONFIG_CLASS is None:
        return own
    try:
        return backend.CONFIG_CLASS(**own)
    except Exception as err:  # noqa: BLE001 - re-raised naming the backend
        raise ValueError(
            f"execution.containers.{SIMULATION_CONTAINER} is not valid for simulator "
            f"backend '{name}': {err}") from None


#: Keys that describe the *container* rather than the simulator -- what moves to the
#: scenario block when a stepped simulator folds into it.
#:
#: ``provenance`` belongs here because it describes the ``image``: in the stepped shape the
#: image moves to the scenario block, and a statement of where that image came from that
#: stayed behind would describe a container that no longer names one. Leaving it out also
#: made it a *simulator* key, which is worse than untidy -- a backend CONFIG_CLASS forbids
#: extras, so every roqsim campaign was rejected the moment the field existed.
_CONTAINER_KEYS = ("image", "command", "resources", "system_packages", "python_packages",
                   "provenance")

#: Keys of the ``simulation`` block that belong to RoboVAST, not to the backend. A
#: backend's CONFIG_CLASS forbids extras, so offering it these would reject every
#: campaign that set one.
_ROBOVAST_KEYS = frozenset({"backend", *_CONTAINER_KEYS})

__all__ = [
    "CONFIG_MOUNT",
    "SHAPE_ROS",
    "SHAPE_STEPPED",
    "SIMULATOR_GROUP",
    "SIM_CONFIG_FILE",
    "SIM_OVERRIDES_MOUNT",
    "SCENARIO_CONTAINER",
    "SIMULATION_CONTAINER",
    "SUT_CONTAINER",
    "SimulatorBackend",
    "apply_backend",
    "backend_name",
    "backend_own_keys",
    "campaign_sim_block",
    "flatten_sim_block",
    "merge_sim_block",
    "resolve_backend",
    "resolve_sim_path",
    "shape_for",
    "sim_job_overlay",
]
