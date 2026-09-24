# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``roqsim`` extra's packaging, which nothing else notices when it breaks.

Deliberately its own file. ``test_simulators.py`` drives the backend *API* through a
stub, on the rule that RoboVAST's own suite never imports a simulator; what is under
test here is the opposite thing -- that the shipped ``robovast-sim-roqsim``
distribution registers its entry point and that loading it still imports no simulator.

Why it is worth a test at all: a wrong ``packages =``, a dropped path dependency, or an
extra missing from the image's ``poetry install`` produces nothing locally and then
"Unknown robovast.simulators plugin 'roqsim'" at campaign start, far from the cause.
"""

import subprocess  # nosec B404 - fixed argv, no shell
import sys
from importlib.metadata import entry_points

import pytest

from robovast.common.simulators import (SHAPE_ROS, SHAPE_STEPPED, SIMULATOR_GROUP, SimulatorBackend,
                                        resolve_backend)

pytestmark = pytest.mark.skipif(
    "roqsim" not in {ep.name for ep in entry_points().select(group=SIMULATOR_GROUP)},
    reason="the 'roqsim' extra is not installed (pip install 'robovast[roqsim]')")


def test_the_extra_registers_the_backend_entry_point():
    ep = {e.name: e for e in entry_points().select(group=SIMULATOR_GROUP)}["roqsim"]
    assert ep.value == "robovast_sim_roqsim.backend:RoqsimBackend"


def test_the_entry_point_resolves_to_a_backend_serving_both_shapes():
    backend = resolve_backend("roqsim")
    assert isinstance(backend, SimulatorBackend)
    # Both, and this is the campaign-visible contract: `mode: ros2` gives the simulator
    # its own container, `mode: base` folds it into the scenario's.
    assert set(backend.SUPPORTED_SHAPES) == {SHAPE_ROS, SHAPE_STEPPED}


def test_the_backend_asks_roqsim_to_stamp_its_log_lines():
    """roqsim's CLI prints `INFO roqsim.engine: msg` by default, on purpose: standalone it is a
    command a person watches, and roqsim is published on its own. In a campaign the reader
    is the merged run log, where a line with no timestamp cannot be ordered against anything.

    Measured on a three-container run before this: five roqsim lines (the drawn seed, the
    recording summary) had no time of their own and folded into the entrypoint line above
    them rather than standing as their own events. The opt-in existed; nothing set it.
    """
    from robovast.common.execution import sidecar_backend_env
    from robovast.common.simulators import apply_backend

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "roqsim",
                                               "config": "pkg:world"}}}
    applied = apply_backend(dict(execution))
    assert applied["_backend_env"]["ROQSIM_LOG_FORMAT"] == "stamped"
    # Through the plumbing too, since `roqsim sim` runs in the simulation *sidecar* and the
    # main container's env cannot reach it.
    assert sidecar_backend_env(applied, "simulation")["ROQSIM_LOG_FORMAT"] == "stamped"


def test_a_campaign_can_still_ask_for_plain_roqsim_logs():
    """The backend supplies a default, not a decision -- the same precedence every other key
    here follows, so a project that wants roqsim's terminal format keeps saying so."""
    from robovast.common.execution import sidecar_backend_env

    execution = {"mode": "ros2", "_backend_env": {"ROQSIM_LOG_FORMAT": "stamped"},
                 "env": [{"ROQSIM_LOG_FORMAT": "plain"}]}
    assert "ROQSIM_LOG_FORMAT" not in sidecar_backend_env(execution, "simulation")


def test_importing_the_backend_pulls_in_no_simulator():
    """The non-negotiable rule for a backend, checked rather than asserted in prose.

    It is imported in the long-lived service process and in the controller image, which
    carry no MuJoCo -- so this is what makes the extra affordable there. Run in a fresh
    interpreter because this one may have imported ``mujoco`` for unrelated reasons.
    """
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c",
         "import robovast_sim_roqsim, sys; "
         "assert 'mujoco' not in sys.modules, sorted(sys.modules)"],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_a_sim_destination_that_addresses_no_part_of_a_world_is_refused():
    """A factor whose destination names nothing fails while the campaign is composed.

    Such a destination merges into the block as an ``overrides`` root roqsim ignores, so every
    cell of the sweep would run the identical world -- a factor with no effect, which reads as
    a result rather than as a mistake and which no amount of repetition reveals.
    """
    from robovast.common.simulators import merge_sim_block

    execution = {"containers": {"simulation": {"backend": "roqsim",
                                               "config": "world/world.yaml"}}}
    with pytest.raises(ValueError, match="box_offset_y"):
        merge_sim_block(execution, {"box_offset_y": 0.03})

    merged = merge_sim_block(execution, {"components.workpiece.pose.position.y": 0.03})
    assert merged["overrides"]["components"]["workpiece"]["pose"]["position"]["y"] == 0.03


def test_the_backend_asks_for_one_mcap_recording_and_nothing_it_no_longer_reads():
    """The recording is one MCAP under the run's ``roqsim_bag/``, asked for by the same name
    ``run_state_file`` later looks for; no pose CSV and no capture export directory beside it,
    since the MCAP is both."""
    from robovast.common.simulators import apply_backend, run_state_filename

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "roqsim", "config": "pkg:world"}}}
    env = apply_backend(dict(execution))["_backend_env"]
    assert env["ROQSIM_RECORD"] == "roqsim_bag/roqsim.mcap"
    assert run_state_filename(apply_backend(dict(execution))) == env["ROQSIM_RECORD"]
    for gone in ("ROQSIM_SIM_POSES", "ROQSIM_CAPTURE_EXPORT_DIR"):
        assert gone not in env
    # No block: the simulator's own defaults, and no knob stated.
    for knob in ("ROQSIM_CAPTURE_FPS", "ROQSIM_RECORD_TRACKS", "ROQSIM_RECORD_EXCLUDE"):
        assert knob not in env


def test_the_recording_block_becomes_the_simulators_knobs():
    from robovast.common.config import recording_config
    from robovast.common.execution import sidecar_backend_env
    from robovast.common.simulators import apply_backend

    execution = {"mode": "ros2",
                 "containers": {"simulation": {"backend": "roqsim", "config": "pkg:world"}}}
    recording = recording_config({"roqsim": {"rate_hz": 25, "tracks": ["robot/**", "box/*"],
                                             "exclude": ["robot/wheel_*"]}})
    applied = apply_backend(dict(execution), recording=recording)
    env = sidecar_backend_env(applied, "simulation")
    assert env["ROQSIM_CAPTURE_FPS"] == "25"
    assert env["ROQSIM_RECORD_TRACKS"] == "robot/**,box/*"
    assert env["ROQSIM_RECORD_EXCLUDE"] == "robot/wheel_*"
    # Only what the block sets: `tracks: all` is the simulator's default, not a variable.
    partial = apply_backend(dict(execution), recording=recording_config({"roqsim": {"rate_hz": 12.5}}))
    assert partial["_backend_env"]["ROQSIM_CAPTURE_FPS"] == "12.5"
    assert "ROQSIM_RECORD_TRACKS" not in partial["_backend_env"]
    # A block with no roqsim section says nothing to roqsim.
    ros_only = apply_backend(dict(execution), recording=recording_config({"ros2": {"use_sim_time": True}}))
    assert "ROQSIM_CAPTURE_FPS" not in ros_only["_backend_env"]
