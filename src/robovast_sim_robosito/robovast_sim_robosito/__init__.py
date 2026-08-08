# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The robosito simulator backend for RoboVAST.

**Its own distribution, deliberately.** RoboVAST must not *require* a simulator: the
framework is what makes any simulator runnable, and a hard dependency on one would
invert that. So this ships as the ``robosito`` extra rather than as part of
``robovast`` -- ``pip install robovast`` names no simulator, ``pip install
robovast[robosito]`` adds this backend's entry point, and a third-party backend arrives
the same way through its own package.

It is in the default service/controller image because it is cheap enough to be: strings
and container specs, whose only dependency (``pydantic``) RoboVAST already has.

What it removes from a campaign: the GL/apt block, the ``mujoco`` pin, the hand-ordered
``rst_*`` wheel list, ``MUJOCO_GL`` selection, ``ENABLE_X11``, the record/capture
variables, and the choice of how to start the simulator at all. A ``.vast`` says which
simulator and which config, and nothing else::

    execution:
      mode: ros2
      containers:
        simulation: {backend: robosito, config: worlds/depot.yaml}

**This module must import without robosito installed.** It runs in the RoboVAST service
process, which has no reason to carry MuJoCo -- so everything here is strings and
container specs, and the one operation that genuinely needs robosito (working out which
files a world is made of) runs *in robosito's own image*.
"""

from robovast_sim_robosito.backend import RobositoBackend

__all__ = ["RobositoBackend"]
