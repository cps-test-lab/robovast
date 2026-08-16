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


def test_the_backend_asks_rst_to_stamp_its_log_lines():
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


def test_a_campaign_can_still_ask_for_plain_rst_logs():
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
