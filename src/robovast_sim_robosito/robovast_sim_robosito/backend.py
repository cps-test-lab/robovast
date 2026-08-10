# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What robosito tells RoboVAST about itself."""

from __future__ import annotations

import json
import os
import shlex
from typing import Optional

from pydantic import BaseModel, ConfigDict
from robovast.common.simulators import (SCENARIO_CONTAINER, SHAPE_ROS,
                                        SHAPE_STEPPED, SIMULATION_CONTAINER,
                                        SimulatorBackend, shape_for)

#: The image robosito runs in when it has a container of its own -- robosito's **own**
#: published image, not something a campaign builds. It carries the GL libraries,
#: ``mujoco`` and every ``rst_*`` package, which is exactly why the ROS shape needs no
#: build at all: nothing a campaign owns contains robosito.
DEFAULT_SIM_IMAGE = os.environ.get(
    "ROBOSITO_IMAGE", "ghcr.io/cps-test-lab/rst-ros:jazzy")

#: The image the *scenario* runs in when robosito is stepped in-process. Here the two
#: roles are one container, so it must carry both the RoboVAST contract
#: (``/etc/robovast_compat_version``, scenario-execution, the ``/out`` mount) and
#: robosito -- which is the one thing robosito's own image does not have.
DEFAULT_COMBINED_IMAGE = os.environ.get(
    "ROBOVAST_ROBOSITO_IMAGE", "ghcr.io/cps-test-lab/robovast-robosito:jazzy")

#: The ``SimulationInterface`` scenario-execution steps.
ADAPTER = "rst.scenario_adapter:MujocoSim"

#: Where RoboVAST mounts a campaign's ``run_files``, in every container of the job.
_CONFIG_MOUNT = "/config"

#: The MuJoCo state recording each run writes, relative to its output directory. Named once
#: and read twice — :meth:`RobositoBackend.env` asks for it, :meth:`run_state_file` tells the
#: service where to find it — so the request and the lookup cannot drift apart.
_RECORD_FILE = "run.npz"


def _is_package_ref(config: str) -> bool:
    """``rst_scenes:depot`` names a packaged world; anything else is a file."""
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
    return f"{_CONFIG_MOUNT}/{config.lstrip('./')}"


class RobositoConfig(BaseModel):
    """robosito's own keys in the ``simulation`` container block."""

    model_config = ConfigDict(extra="forbid")

    #: The robosito config: a world YAML path beside the ``.vast``, or a package ref
    #: (``rst_scenes:depot``). Called ``config`` rather than ``world`` because the file
    #: is robosito's whole configuration -- physics, plugins, robot, sensors, and its
    #: ``extends`` chain -- and "world" understates what a campaign is selecting.
    config: str
    #: The ``SimulationInterface`` scenario-execution steps, as ``module:Class``.
    #: Defaults to robosito's generic adapter, which is what an ordinary campaign wants.
    #:
    #: A campaign overrides it to name its own factors. scenario-execution forwards a
    #: scenario parameter only when the concrete ``reset()`` declares it *by name* --
    #: ``_build_reset_kwargs`` skips ``**kwargs`` outright -- so a sweep over
    #: experiment-specific parameters needs a ``reset()`` that names them. Putting those
    #: names in the generic adapter would mean robosito learning one experiment's
    #: factors, so the subclass belongs to the experiment, next to the plugins it drives.
    #: Stepped shape only: with the ROS shape there is no in-process interface at all.
    adapter: Optional[str] = None


class RobositoBackend(SimulatorBackend):
    """robosito, in both shapes.

    Stepped (``mode: base``) and ROS (``mode: ros2``) differ in exactly two ways: where
    the simulator runs, and how it is told which config to load. Everything else --
    headless, the GL backend, the run capture -- is the same because it has one correct
    value for any campaign.
    """

    CONFIG_CLASS = RobositoConfig
    SUPPORTED_SHAPES = (SHAPE_STEPPED, SHAPE_ROS)

    def containers(self, cfg, execution: dict) -> dict:
        if shape_for(execution.get("mode", "auto")) == SHAPE_ROS:
            # Its own container, running robosito's ordinary CLI. Not a RoboVAST-specific
            # entry point: the same command debugs the world by hand, so there is no
            # second way the simulator can be started.
            #
            # No transport flags: which topics a world speaks, under which namespace, and
            # whether it serves a control plane are the WORLD's to declare. A campaign
            # runner configuring a simulator's middleware would be reaching a layer down,
            # and headless/pacing are the only two the deployment owns.
            command = ["rst", "sim", _config_in_container(cfg.config),
                       "--headless", "--pacing", "realtime"]
            return {SIMULATION_CONTAINER: {"image": DEFAULT_SIM_IMAGE,
                                           "command": command}}
        # Stepped: scenario-execution calls step(), so the simulator is in its process
        # and the two roles are one container.
        return {SCENARIO_CONTAINER: {"image": DEFAULT_COMBINED_IMAGE}}

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
            "ROBOSITO_RECORD": _RECORD_FILE,
            "ROBOSITO_CAPTURE_EXPORT_DIR": "capture",
            # The pose series, streamed per sample beside the recording as
            # `run.sim_poses.csv` -> the `sim_poses` table. Unlike the two above it
            # survives a kill, because every row is flushed as it is taken.
            #
            # Always on, for two reasons a campaign never has to weigh. It is the only
            # pose data a STEPPED run produces at all: with no ROS there is no rosbag, so
            # nothing derives a `poses` table afterwards. And where there IS a rosbag it
            # is the honest one — world-frame poses on exact sim time, with velocities
            # read from the solver instead of differenced over rosbag arrival times,
            # which are quantised by the /clock grid and jittered by delivery.
            "ROBOSITO_SIM_POSES": "1",
            # Timestamp rst's own log lines, so they can be placed on the run's clock like
            # every other producer's. rst defaults to `INFO rst.engine: msg` because that is
            # what belongs in a terminal, where `rst sim` is one command a person is
            # watching -- and robosito is published standalone, so a campaign is no reason
            # to make that worse. Here the reader is the merged run log rather than a
            # person, and a line with no timestamp cannot be ordered against anything.
            #
            # Measured on a three-container run before this: five rst lines (the drawn seed,
            # the recording summary) carried no time and folded into the entrypoint line
            # above them instead of standing as their own events.
            "RST_LOG_FORMAT": "stamped",
        }
        # MUJOCO_GL is deliberately absent. Which backend works is a property of the
        # machine the simulator lands on, and this code runs on the *service host* -- a
        # different machine whenever a campaign is dispatched. robosito picks it at
        # startup instead (rst.viewer.select_offscreen_gl), which is what finally
        # retires the 22-line shell script three packages had each copied.
        if shape_for(execution.get("mode", "auto")) == SHAPE_STEPPED:
            # In-process: no command line to put the config on, so the adapter reads it
            # from here. The scenario stays simulator-agnostic either way -- it never
            # learns that this simulator has a thing called a world.
            env["ROBOSITO_WORLD"] = cfg.config
        return env

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

        Since a robosito campaign always records that capture (:meth:`produces_run_capture`),
        every such campaign can replay its runs in 3D -- so the panel is contributed rather
        than declared, and a campaign that wants it elsewhere on screen still says so itself.
        """
        return [{"scene3d": {}}]

    def input_files(self, cfg, execution: dict) -> list:
        """The world, when the campaign owns it -- nothing when it is packaged.

        A path in ``config:`` names a file beside the ``.vast``, which has to travel with
        the campaign or the simulator has nothing to open. Returning it here is what puts
        it in ``run_files`` without the campaign naming it twice.

        A package ref (``rst_scenes:depot``) travels inside the image and needs nothing.
        """
        if _is_package_ref(cfg.config):
            return []
        return [cfg.config]

    def scene_export(self, cfg, execution: dict, *, world: str, max_tex_dim: int,
                     overrides: dict) -> str:
        """``rst-export-web``, robosito's own exporter, run in robosito's own image.

        *world* is passed through as the capture recorded it -- a package ref, or the
        ``/config/...`` path a campaign file had in the job, which RoboVAST reproduces for
        the build so the world resolves what it references.
        """
        sets = " ".join(f"--set {_set_arg(k, v)}" for k, v in _flatten(overrides))
        return (f"rst-export-web --world {world} {sets} --out {{out}} "
                f"--max-tex-dim {int(max_tex_dim)} --manifest {{out}}/.generated.json")

    def run_state_file(self, cfg, execution: dict) -> str:
        """The recording :meth:`env` asks every run to write.

        Both sides read :data:`_RECORD_FILE`, so the name that is *requested* and the name that
        is later *looked for* cannot drift apart.
        """
        del cfg, execution
        return _RECORD_FILE

    def simulation_screenshot(self, cfg, execution: dict, *, state: str,
                              at=None, view=None, focus=None, camera=None,
                              size: str = "960x720") -> str:
        """``rst render``, replaying the run's own recording from a chosen viewpoint.

        No world argument: ``rst render``'s target is optional with ``--state``, because a
        recording *names the world it was made from*. That is what makes this work for a
        campaign whose world is a package ref and one whose world is a campaign file alike --
        the recording answers, rather than RoboVAST having to reconstruct the reference.

        RoboVAST's four view keys map one-to-one onto rst's ``sim.view`` names, which is why
        those four were the ones chosen. So the whole robosito-specific surface here is which
        binary and how it spells its flags; a second simulator implements the same hook and
        the same tool starts answering for it.

        Every value is quoted: the return is a *string* put through ``shlex.split``, and
        ``lookat=1,2,0`` has to survive as one word -- the same trap :func:`_set_arg`'s
        docstring records for vector-valued overrides.
        """
        parts = ["rst", "render", "--state", state,
                 "--out", "{out}/frame.png", "--size", shlex.quote(str(size))]
        if at is not None:
            parts += ["--at", repr(float(at))]
        if camera:
            # Owns its own pose, so rst refuses it together with --view/--focus; the caller
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
    runs through ``shlex.split``. A vector-valued override -- ``plugins.parcel.pos: [11.8,
    4.55, 0.762]``, i.e. exactly what a campaign that sweeps a position records -- renders as
    ``[11.8, 4.55, 0.762]`` and was torn into three argv words at those spaces, so
    ``rst-export-web`` got ``--set plugins.parcel.pos=[11.8,`` plus two stray arguments and
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
