# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The entrypoints keep a container's output *live*, not merely complete.

Both scripts tee stdout into the job's log file, and both wrap that tee in ``stdbuf -oL``
-- which unbuffers TEE, not the workload. The workload's stdout is a pipe, so libc block-
buffers it at the source in 4-8 KB chunks and tee cannot flush what it was never given.
The log panel then goes quiet for a minute and dumps a wall of text: output that is
technically complete and useless to watch.

The main container was spared this only by accident, because the RoboVAST image sets
PYTHONUNBUFFERED. A SIDECAR is explicitly allowed to be a vanilla image -- that is the
whole claim of the ROS shape, "point it at any nav2 image and it works" -- so it inherits
nothing, and the promise that its image can be stock is exactly what breaks its liveness.

These read the shipped scripts. The existing entrypoint tests pin the *wiring* (which
script runs, with what argv) and never look at the body, so nothing else would notice
these lines being dropped.
"""

from importlib.resources import files

import pytest

SCRIPTS = ("entrypoint.sh", "secondary_entrypoint.sh")


def _script(name: str) -> str:
    return files("robovast.execution.data").joinpath(name).read_text()


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_entrypoint_unbuffers_python(name):
    """Covers `roqsim sim` and every Python ROS node, whatever image they run in."""
    assert "export PYTHONUNBUFFERED=1" in _script(name)


def test_a_sidecars_workload_runs_line_buffered():
    """The other half of a ROS stack is C++, which PYTHONUNBUFFERED does not reach.

    stdbuf's LD_PRELOAD is inherited, so a `ros2 launch` here reaches the nodes it
    spawns. It must wrap the WORKLOAD -- wrapping tee is what the bug was.
    """
    body = _script("secondary_entrypoint.sh")
    assert 'stdbuf -oL -eL "$@" &' in body, \
        "run_child must launch the workload line-buffered"


@pytest.mark.parametrize("name", SCRIPTS)
def test_stdbuf_is_still_a_required_tool(name):
    """Both scripts check their tools up front and fail loudly. The unbuffering above
    depends on stdbuf being there, so that check is what keeps it from silently
    degrading to block-buffered output in an image that lacks it."""
    assert "stdbuf" in _script(name)
