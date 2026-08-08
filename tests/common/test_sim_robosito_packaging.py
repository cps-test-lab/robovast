# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``robosito`` extra's packaging, which nothing else notices when it breaks.

Deliberately its own file. ``test_simulators.py`` drives the backend *API* through a
stub, on the rule that RoboVAST's own suite never imports a simulator; what is under
test here is the opposite thing -- that the shipped ``robovast-sim-robosito``
distribution registers its entry point and that loading it still imports no simulator.

Why it is worth a test at all: a wrong ``packages =``, a dropped path dependency, or an
extra missing from the image's ``poetry install`` produces nothing locally and then
"Unknown robovast.simulators plugin 'robosito'" at campaign start, far from the cause.
"""

import subprocess  # nosec B404 - fixed argv, no shell
import sys
from importlib.metadata import entry_points

import pytest

from robovast.common.simulators import (SHAPE_ROS, SHAPE_STEPPED,
                                        SIMULATOR_GROUP, SimulatorBackend,
                                        resolve_backend)

pytestmark = pytest.mark.skipif(
    "robosito" not in {ep.name for ep in entry_points().select(group=SIMULATOR_GROUP)},
    reason="the 'robosito' extra is not installed (pip install 'robovast[robosito]')")


def test_the_extra_registers_the_backend_entry_point():
    ep = {e.name: e for e in entry_points().select(group=SIMULATOR_GROUP)}["robosito"]
    assert ep.value == "robovast_sim_robosito.backend:RobositoBackend"


def test_the_entry_point_resolves_to_a_backend_serving_both_shapes():
    backend = resolve_backend("robosito")
    assert isinstance(backend, SimulatorBackend)
    # Both, and this is the campaign-visible contract: `mode: ros2` gives the simulator
    # its own container, `mode: base` folds it into the scenario's.
    assert set(backend.SUPPORTED_SHAPES) == {SHAPE_ROS, SHAPE_STEPPED}


def test_importing_the_backend_pulls_in_no_simulator():
    """The non-negotiable rule for a backend, checked rather than asserted in prose.

    It is imported in the long-lived service process and in the controller image, which
    carry no MuJoCo -- so this is what makes the extra affordable there. Run in a fresh
    interpreter because this one may have imported ``mujoco`` for unrelated reasons.
    """
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c",
         "import robovast_sim_robosito, sys; "
         "assert 'mujoco' not in sys.modules, sorted(sys.modules)"],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
