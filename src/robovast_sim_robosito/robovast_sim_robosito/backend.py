# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What robosito tells RoboVAST about itself."""

from __future__ import annotations

import os
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
    #: Publish ``/tf`` and ``/tf_static`` under this namespace (topics only, frames
    #: unchanged). What Nav2's standard ``/tf -> tf`` remap expects.
    tf_namespace: Optional[str] = None
    #: Also serve the ``simulation_interfaces`` control plane, which a scenario's
    #: ``osc.sim`` actions are clients of. Off unless a scenario touches entities.
    sim_control: bool = False
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
            command = ["rst", "sim", _config_in_container(cfg.config), "--ros",
                       "--headless", "--pacing", "realtime"]
            if cfg.tf_namespace:
                command += ["--tf-namespace", cfg.tf_namespace]
            if cfg.sim_control:
                command.append("--sim-control")
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
            "ROBOSITO_RECORD": "run.npz",
            "ROBOSITO_CAPTURE_EXPORT_DIR": "capture",
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
