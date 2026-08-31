# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What roqsim tells RoboVAST about itself."""

from __future__ import annotations

import json
import os
import shlex
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict

from robovast.common.execution import MEMBER_ROQSIM, family_image_ref
from robovast.common.simulators import (CONFIG_MOUNT, SCENARIO_CONTAINER, SHAPE_ROS, SHAPE_STEPPED,
                                        SIM_OVERRIDES_MOUNT, SIM_QUERY_OVERRIDES_MOUNT,
                                        SIMULATION_CONTAINER, ContainerQuery, SimulatorBackend,
                                        shape_for, simulator_image)
from robovast.common.variation.container_runner import ContainerSpec

#: The ``SimulationInterface`` scenario-execution steps.
ADAPTER = "roqsim.scenario_adapter:MujocoSim"

#: The MuJoCo state recording each run writes, relative to its output directory. Named once
#: and read twice — :meth:`RoqsimBackend.env` asks for it, :meth:`run_state_file` tells the
#: service where to find it — so the request and the lookup cannot drift apart.
_RECORD_FILE = "run.npz"


def _is_package_ref(config: str) -> bool:
    """``roqsim_scenes:depot`` names a packaged world; anything else is a file."""
    return ":" in config and not config.startswith((".", "/"))


def _config_in_container(config: str) -> str:
    """Where the simulator will find the config once the job is running.

    A path in the ``.vast`` is relative to the ``.vast``, which is a directory that does
    not exist in the container: RoboVAST mounts the campaign's ``run_files`` under
    ``/config``. Passing the authored path through unchanged makes the simulator look
    beside its own working directory and fail with "world config does not exist" -- after
    the image pull and the pod schedule, so the cost is a whole cell.

    A package ref is left alone: it travels inside the image and has no path at all.
    """
    if _is_package_ref(config):
        return config
    if config.startswith("/"):
        return config
    return f"{CONFIG_MOUNT}/{config.lstrip('./')}"


def _extends_a_campaign_file(config: str, vast_dir: str) -> bool:
    """Whether this world inherits from another file the CAMPAIGN owns.

    Reading one top-level key is not resolving the chain -- it is deciding whether the chain
    has to be resolved, which is the difference between a cheap answer and a container run.
    A parent that is a package ref, or absent entirely, means the campaign's one file is the
    whole of what it owns.

    Resolved against *vast_dir*, never against the working directory. Opening the authored
    path as given made this answer depend on where the caller stood: from the campaign's own
    directory the file was found and the chain was resolved, from anywhere else the open
    failed and the branch below read that as "no parent" -- so the same campaign both did and
    did not ask the simulator, and only one of those staged the parent.

    Unreadable, or not a mapping: no, because such a file is not a world the simulator can
    open either -- it fails with its own error, and paying for a container to describe it
    would only make that failure slower. The question here is narrow on purpose: does the
    chain need resolving, not is this world any good.
    """
    path = config if os.path.isabs(config) else os.path.join(vast_dir or ".", config)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(raw, dict):
        return False
    parent = raw.get("extends")
    return isinstance(parent, str) and not _is_package_ref(parent)


class RoqsimConfig(BaseModel):
    """roqsim's own keys in the ``simulation`` container block."""

    model_config = ConfigDict(extra="forbid")

    #: The roqsim config: a world YAML path beside the ``.vast``, or a package ref
    #: (``roqsim_scenes:depot``). Called ``config`` rather than ``world`` because the file
    #: is roqsim's whole configuration -- physics, plugins, robot, sensors, and its
    #: ``extends`` chain -- and "world" understates what a campaign is selecting.
    config: str
    #: Parts of the world to change before it is compiled, as a nested mapping mirroring
    #: the world YAML with components addressed by name -- exactly :func:`roqsim.apply_overrides`'
    #: input, and exactly what ``roqsim sim --set`` builds from a dotlist.
    #:
    #: This is what makes a world *variable* without one YAML per cell: a campaign sweeping
    #: a floorplan dimension or a prop's mass writes ``sim: components.floorplan.size`` and
    #: lands here. It travels as a file rather than as ``--set`` flags because the values are
    #: structured, and because a file is something the results keep and a human can replay.
    #:
    #: Override semantics, not ``extends``: a child world's ``components`` are *appended* after
    #: the parent's, so an inherited plugin can only be changed by disabling and re-adding
    #: it. ``apply_overrides`` resolves a plugin by name and deep-merges, which is what a
    #: campaign varying one value of one plugin actually means.
    overrides: Optional[dict] = None
    #: The ``SimulationInterface`` scenario-execution steps, as ``module:Class``.
    #: Defaults to roqsim's generic adapter, which is what an ordinary campaign wants.
    #:
    #: A campaign overrides it to name its own factors. scenario-execution forwards a
    #: scenario parameter only when the concrete ``reset()`` declares it *by name* --
    #: ``_build_reset_kwargs`` skips ``**kwargs`` outright -- so a sweep over
    #: experiment-specific parameters needs a ``reset()`` that names them. Putting those
    #: names in the generic adapter would mean roqsim learning one experiment's
    #: factors, so the subclass belongs to the experiment, next to the plugins it drives.
    #: Stepped shape only: with the ROS shape there is no in-process interface at all.
    adapter: Optional[str] = None


class RoqsimBackend(SimulatorBackend):
    """roqsim, in both shapes.

    Stepped (``mode: base``) and ROS (``mode: ros2``) differ in exactly two ways: where
    the simulator runs, and how it is told which config to load. Everything else --
    headless, the GL backend, the run capture -- is the same because it has one correct
    value for any campaign.
    """

    CONFIG_CLASS = RoqsimConfig
    SUPPORTED_SHAPES = (SHAPE_STEPPED, SHAPE_ROS)
    #: A bare ``sim:`` path is a path into the world. ``sim: config`` still selects the
    #: world file, because a bare backend key wins; a world key colliding with one is
    #: reached by spelling this root out.
    DOTTED_ROOT = "overrides"

    #: Where roqsim's worlds and models come from. Naming these belongs HERE and nowhere else:
    #: this distribution exists to be the one place that knows roqsim, so core stays free of
    #: simulator names while a campaign's results still record which asset provider supplied
    #: them. It matters because some providers are private -- a campaign using one is
    #: reproducible only by someone who can obtain that code, and a published dataset has to
    #: name it and its commit rather than depending on something nobody can identify.
    ASSET_ENTRY_POINT_GROUPS = ("roqsim.models", "roqsim.worlds", "roqsim.plugins")

    def containers(self, cfg, execution: dict) -> dict:
        # Both shapes name the SAME family member, symbolically: only that image carries
        # roqsim *and* the RoboVAST contract (the org.robovast.compat-version label,
        # scenario-execution, the /out mount). Not roqsim's own published image: that one
        # has the simulator but not the contract, so the runner rejects it -- and nothing
        # publishes that tag anyway.
        #
        # Symbolic, not resolved here: which project and tag it comes from is a property
        # of the campaign, and this runs before one exists.
        image = family_image_ref(MEMBER_ROQSIM)
        if shape_for(execution.get("mode", "auto")) == SHAPE_ROS:
            # Its own container, running roqsim's ordinary CLI. Not a RoboVAST-specific
            # entry point: the same command debugs the world by hand, so there is no
            # second way the simulator can be started.
            #
            # No transport flags: which topics a world speaks, under which namespace, and
            # whether it serves a control plane are the WORLD's to declare. A campaign
            # runner configuring a simulator's middleware would be reaching a layer down,
            # and headless/pacing are the only two the deployment owns.
            command = ["roqsim", "sim", _config_in_container(cfg.config),
                       "--headless", "--pacing", "realtime"]
            if cfg.overrides:
                # The file spelling of --set. Not the flags themselves: a campaign's
                # overrides are a nested tree, and flattening one onto argv loses it to
                # quoting, keeps it out of the results, and leaves nobody able to replay
                # the cell. RoboVAST mounts the document; this only names where.
                command += ["--override", SIM_OVERRIDES_MOUNT]
            return {SIMULATION_CONTAINER: {"image": image, "command": command}}
        # Stepped: scenario-execution calls step(), so the simulator is in its process
        # and the two roles are one container.
        return {SCENARIO_CONTAINER: {"image": image}}

    def simulation_ref(self, cfg, execution: dict) -> Optional[str]:
        return cfg.adapter or ADAPTER

    def env(self, cfg, execution: dict) -> dict:
        """What has one correct value for any campaign, so nobody should have to write it.

        Not a "session" block in the world YAML and not keys in the ``.vast``: a campaign
        is always headless, always wants the capture its 3D run view replays, and always
        wants a GL backend that works on the node it landed on. There is nothing to
        decide, so there is nothing to declare.
        """
        env = {
            # An in-container Xvfb would shadow a bind-mounted host X socket, and a
            # campaign has no window either way.
            "ENABLE_X11": "false",
            # The run's ground truth, and the capture the scene3d panel replays. Both
            # are written on a clean stop only; a run killed by a timeout leaves neither.
            "ROQSIM_RECORD": _RECORD_FILE,
            "ROQSIM_CAPTURE_EXPORT_DIR": "capture",
            # The pose series, streamed per sample beside the recording as
            # `run.sim_poses.csv` -> the `sim_poses` table. Unlike the two above it
            # survives a kill, because every row is flushed as it is taken.
            #
            # Always on, for two reasons a campaign never has to weigh. It is the only
            # pose data a STEPPED run produces at all: with no ROS there is no rosbag, so
            # nothing derives a `poses` table afterwards. And where there IS a rosbag it
            # is the honest one — world-frame poses on exact sim time, with velocities
            # read from the solver instead of differenced over rosbag arrival times,
            # which are quantized by the /clock grid and jittered by delivery.
            "ROQSIM_SIM_POSES": "1",
            # Timestamp roqsim's own log lines, so they can be placed on the run's clock like
            # every other producer's. roqsim defaults to `INFO roqsim.engine: msg` because that is
            # what belongs in a terminal, where `roqsim sim` is one command a person is
            # watching -- and roqsim is published standalone, so a campaign is no reason
            # to make that worse. Here the reader is the merged run log rather than a
            # person, and a line with no timestamp cannot be ordered against anything.
            #
            # Measured on a three-container run before this: five roqsim lines (the drawn seed,
            # the recording summary) carried no time and folded into the entrypoint line
            # above them instead of standing as their own events.
            "ROQSIM_LOG_FORMAT": "stamped",
        }
        # MUJOCO_GL is deliberately absent. Which backend works is a property of the
        # machine the simulator lands on, and this code runs on the *service host* -- a
        # different machine whenever a campaign is dispatched. roqsim picks it at
        # import instead (roqsim.gl.select_offscreen_gl), which is what finally
        # retires the 22-line shell script three packages had each copied.
        if shape_for(execution.get("mode", "auto")) == SHAPE_STEPPED:
            # In-process: no command line to put the config on, so the adapter reads it
            # from here. The scenario stays simulator-agnostic either way -- it never
            # learns that this simulator has a thing called a world.
            #
            # Through `_config_in_container`, like the ROS shape. Passed raw, a stepped
            # campaign whose world is a relative path has the simulator look beside its own
            # working directory instead of at the mount -- the exact failure that function
            # exists to prevent, and it must not be avoided on one path only.
            env["ROQSIM_WORLD"] = _config_in_container(cfg.config)
            if cfg.overrides:
                # The same document the ROS shape passes with --override, reached the way
                # everything else is in this shape: there is no command line here.
                env["ROQSIM_WORLD_OVERRIDES"] = SIM_OVERRIDES_MOUNT
        return env

    def sim_document(self, cfg, execution: dict):
        """The overrides, which is the half of the config that is a *document*.

        The world itself stays on argv -- it is one token, and ``roqsim sim <world>`` is how a
        person runs this simulator. What cannot go there is the override tree, so that is
        what gets a file. Written per job by RoboVAST, mounted at
        :data:`~robovast.common.simulators.SIM_OVERRIDES_MOUNT`, and read by ``--override``.
        """
        del execution
        return cfg.overrides or None

    def produces_run_capture(self, cfg, execution: dict) -> bool:
        return True

    def default_panels(self, cfg, execution: dict) -> list:
        """The 3D scene, always -- for the same reason :meth:`env` supplies the capture.

        Two artifacts drive the panel and both resolve themselves: the *scene* (geometry) is
        compiled by the service on first open, inside the simulator's own pinned image, and
        cached by world identity; the *run capture* (motion) records the world reference and
        its overrides and addresses that geometry by name, so a world that later gains an arm
        or a walker replays without anyone editing a ``.vast``. The capture path defaults to
        ``capture/capture.json``.

        Since a roqsim campaign always records that capture (:meth:`produces_run_capture`),
        every such campaign can replay its runs in 3D -- so the panel is contributed rather
        than declared, and a campaign that wants it elsewhere on screen still says so itself.
        """
        return [{"scene3d": {}}]

    def input_files(self, cfg, execution: dict, vast_dir: str):
        """Everything the world is made of -- asked of the image that can answer it.

        A world is not one file. It is the YAML, whatever it ``extends``, the MJCF that chain
        settles on, and the meshes and colliders that MJCF names, all referenced by paths
        relative to each other. Returning just ``cfg.config`` staged the YAML and nothing
        else, so a world extending another **campaign** file failed in the container on a
        parent that never travelled -- after the image pull and the pod schedule.

        Enumerating the chain needs roqsim, which this module must not import (it is loaded
        in the long-lived service process). So it returns the question instead: ``roqsim scenes
        inputs`` run in roqsim's own image, which is also the image that will run the
        campaign, so the answer describes exactly what that run will open.

        A package ref (``roqsim_scenes:depot``) still needs nothing, and says so without a
        container: the files arrive installed.
        """
        if _is_package_ref(cfg.config):
            return []
        if not _extends_a_campaign_file(cfg.config, vast_dir):
            # The common case, and it needs no container: a world that extends nothing, or
            # extends a PACKAGED world, is complete in the one file the campaign owns. Asking
            # an image would make every ordinary campaign's composition depend on pulling a
            # multi-gigabyte simulator -- a cost paid by `validate_project` and
            # `preview_configurations` too, neither of which runs anything.
            return [cfg.config]
        return ContainerQuery(
            # The campaign's own image, for the same reason describe_query uses it: what a
            # world extends is resolved by what is installed.
            ContainerSpec(image=simulator_image(execution, self.containers(cfg, execution))),
            # Through ``_config_in_container``, like every other command this backend sends:
            # the container is given the campaign's files at ``/config``, and the authored
            # path is relative to the ``.vast``. Passed raw, roqsim looked for the world
            # beside its own working directory and said it did not exist.
            ["roqsim", "scenes", "inputs", _config_in_container(cfg.config)])

    def describe_query(self, cfg, execution: dict, *, entities: bool = False,
                       targets: str = ""):
        """``roqsim scenes describe``, in the image this campaign runs.

        What makes the ``sim`` channel checkable: a campaign writes
        ``plugins.floorplan.floor.friction`` and nothing here can tell whether that plugin is in
        the world without resolving its ``extends`` chain, which needs the simulator. Asked of
        the image that will run the campaign, so the answer describes the world that will load.
        """
        command = ["roqsim", "scenes", "describe", _config_in_container(cfg.config)]
        if entities:
            # Costs a model build, so it is asked for only when a campaign names entities.
            command.append("--entities")
        if targets:
            # Same cost, same reason it is opt-in: naming what a run may override means
            # compiling the model. The glob is the caller's, and it is what keeps the answer
            # small -- a mobile-manipulator world has hundreds of geoms.
            command += ["--overridable", targets]
        # Described WITH this configuration's overrides, the same file spelling the run uses
        # (:meth:`sim_document`). Which entities a world compiles depends on its plugins' config,
        # so a campaign whose obstacles come from its own overrides compiles them only with those
        # applied: asked without them, the answer is about a different world, and the entity check
        # read that as a working campaign naming entities that do not exist.
        document = self.sim_document(cfg, execution)
        if document:
            command += ["--override", SIM_QUERY_OVERRIDES_MOUNT]
        return ContainerQuery(
            ContainerSpec(image=simulator_image(execution, self.containers(cfg, execution))),
            command,
            {SIM_QUERY_OVERRIDES_MOUNT: document} if document else None)


    def scene_export(self, cfg, execution: dict, *, world: str, max_tex_dim: int,
                     overrides: dict, overrides_file: Optional[str] = None) -> str:
        """``roqsim-export-web``, roqsim's own exporter, run in roqsim's own image.

        *world* is passed through as the capture recorded it -- a package ref, or the
        ``/config/...`` path a campaign file had in the job, which RoboVAST reproduces for
        the build so the world resolves what it references.

        Overrides go in through ``--override``, THE SAME FILE SPELLING THE RUN USES, and for
        the same reason (see :meth:`containers`): a campaign's overrides are a nested tree,
        and argv cannot carry one. Flattening them onto ``--set`` worked until a campaign
        varied something structured -- a list of obstacle instances -- which reached the
        exporter as ``KeyError: '"pos"'``, because a list of mappings is not a dotlist
        value. It fails only when the run view is opened, so it reads as "this campaign has
        no 3D geometry" rather than as a quoting bug.

        ``--set`` is still what a person types by hand; it is simply not how a campaign's
        recorded overrides travel.
        """
        override_arg = f" --override {overrides_file}" if overrides_file else ""
        return (f"roqsim-export-web --world {world}{override_arg} --out {{out}} "
                f"--max-tex-dim {int(max_tex_dim)} --manifest {{out}}/.generated.json")

    def run_state_file(self, cfg, execution: dict) -> str:
        """The recording :meth:`env` asks every run to write.

        Both sides read :data:`_RECORD_FILE`, so the name that is *requested* and the name that
        is later *looked for* cannot drift apart.
        """
        del cfg, execution
        return _RECORD_FILE

    def health_command(self, cfg, execution: dict, *, run_dir: str) -> str:
        """``roqsim health``, judging the records this run is streaming as it goes.

        Cheap by construction, which is what lets the service poll it: the three checks read the
        tail of the clock and pose records, and the ``state`` block in the same reply is the last
        sample of those same reads. Nothing here folds a whole file.

        Deliberately no ``--robot``: ``roqsim health`` resolves which bodies are robots from the
        run's own entity roster, so the motion check runs without this backend naming anything
        per world -- and a run with no roster says so in the reply's ``skipped``, which is the
        honest answer rather than a silent pass. ``--robot`` remains an override there; passing
        one from here would be RoboVAST guessing at a world it does not read.
        """
        del cfg, execution
        return f"roqsim health --json {shlex.quote(run_dir)}"

    def simulation_screenshot(self, cfg, execution: dict, *, state: str,
                              at=None, view=None, focus=None, camera=None,
                              size: str = "960x720") -> str:
        """``roqsim render``, replaying the run's own recording from a chosen viewpoint.

        No world argument: ``roqsim render``'s target is optional with ``--state``, because a
        recording *names the world it was made from*. That is what makes this work for a
        campaign whose world is a package ref and one whose world is a campaign file alike --
        the recording answers, rather than RoboVAST having to reconstruct the reference.

        RoboVAST's four view keys map one-to-one onto roqsim's ``sim.view`` names, which is why
        those four were the ones chosen. So the whole roqsim-specific surface here is which
        binary and how it spells its flags; a second simulator implements the same hook and
        the same tool starts answering for it.

        Every value is quoted: the return is a *string* put through ``shlex.split``, and
        ``lookat=1,2,0`` has to survive as one word -- the same trap :func:`_set_arg`'s
        docstring records for vector-valued overrides.
        """
        parts = ["roqsim", "render", "--state", state,
                 "--out", "{out}/frame.png", "--size", shlex.quote(str(size))]
        if at is not None:
            parts += ["--at", repr(float(at))]
        if camera:
            # Owns its own pose, so roqsim refuses it together with --view/--focus; the caller
            # is refused earlier, with a message about the camera rather than about argv.
            parts += ["--camera", shlex.quote(str(camera))]
        else:
            if view:
                parts += ["--view"] + [shlex.quote(f"{k}={v}") for k, v in sorted(view.items())]
            if focus:
                parts += ["--focus"] + [shlex.quote(str(f)) for f in focus]
        return " ".join(parts)


def _set_arg(key: str, value) -> str:
    """One ``--set key=value`` argument that survives ``shlex.split`` as a single word.

    QUOTED, and serialized without spaces, because the command is a STRING the generator
    runs through ``shlex.split``. A vector-valued override -- ``components.parcel.pos: [11.8,
    4.55, 0.762]``, i.e. exactly what a campaign that sweeps a position records -- renders as
    ``[11.8, 4.55, 0.762]`` and was torn into three argv words at those spaces, so
    ``roqsim-export-web`` got ``--set components.parcel.pos=[11.8,`` plus two stray arguments and
    exited 2. The failure surfaces only when somebody opens the run view, and only for a
    world whose overrides contain a list, so it reads as "this campaign has no 3D geometry"
    rather than as a quoting bug.
    """
    return shlex.quote(f"{key}={json.dumps(value, separators=(',', ':'))}")


def _flatten(value, prefix=""):
    """Nested override dict -> ``[(dotted.key, leaf), ...]``, the dotlist ``--set`` accepts."""
    for name, item in sorted((value or {}).items()):
        path = f"{prefix}{name}"
        if isinstance(item, dict):
            yield from _flatten(item, f"{path}.")
        else:
            yield path, item
