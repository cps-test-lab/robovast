# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``tap_command`` hook: what each shape answers, and what a backend may say instead.

The ROS shape's tap is the shape's, not a simulator's: the simulation container speaks ROS
whatever runs in it, so ``ros2 topic echo`` is the base class's answer there and a campaign
with no backend at all gets it too. The stepped shape has no process to ask. roqsim says
``None`` in both, because its recording is already the live view.
"""

import shlex

import pytest

from robovast.common.simulators import (SHAPE_ROS, SHAPE_STEPPED, SimulatorBackend,
                                        ros_tap_command, tap_command)


class _Silent(SimulatorBackend):
    """A backend with a tool of its own that answers nothing to a tap."""
    SUPPORTED_SHAPES = (SHAPE_STEPPED, SHAPE_ROS)

    def tap_command(self, cfg, execution, *, run_dir, selection):
        return None


class _Own(SimulatorBackend):
    """A backend naming its own following command, reading the run's records."""

    def tap_command(self, cfg, execution, *, run_dir, selection):
        return ["mysim", "follow", run_dir, *selection]


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    import robovast.common.simulators as mod
    backends = {"silent": _Silent, "own": _Own}
    monkeypatch.setattr(mod, "resolve_backend", lambda name, base_dir="": backends[name]())


def _execution(mode, **sim):
    return {"mode": mode, "runs": 1, "containers": {"simulation": {**sim}}}


# -- the ROS command --------------------------------------------------------------------------


def test_one_topic_is_echoed_directly_and_unbuffered():
    argv = ros_tap_command(["/odom"])
    assert argv == ["env", "PYTHONUNBUFFERED=1", "ros2", "topic", "echo", "/odom"]


def test_no_selection_lists_the_topics_once():
    assert ros_tap_command([]) == ["ros2", "topic", "list"]
    assert ros_tap_command(None) == ["ros2", "topic", "list"]


def test_csv_is_a_flag_not_a_topic():
    assert ros_tap_command(["csv", "/odom"]) == \
        ["env", "PYTHONUNBUFFERED=1", "ros2", "topic", "echo", "--csv", "/odom"]
    assert ros_tap_command(["csv"]) == ["ros2", "topic", "list"]


def test_several_topics_run_under_one_shell_each_line_tagged():
    """One process to start and one to stop, and a reader can tell the streams apart."""
    argv = ros_tap_command(["/a", "/b"])
    assert argv[:4] == ["env", "PYTHONUNBUFFERED=1", "/bin/bash", "-c"]
    script = argv[4]
    assert "ros2 topic echo /a |" in script and "ros2 topic echo /b |" in script
    assert script.count("printf") == 2 and script.endswith(" & wait")
    assert "printf '%s %s\\n' /a " in script and "printf '%s %s\\n' /b " in script


def test_a_topic_name_with_whitespace_is_refused():
    with pytest.raises(ValueError, match="no whitespace"):
        ros_tap_command(["/a b"])


def test_a_topic_name_is_never_reparsed_by_a_shell():
    argv = ros_tap_command(["/a;rm", "/b"])
    assert shlex.split(argv[4].split(" | ")[0]) == ["ros2", "topic", "echo", "/a;rm"]


# -- the shapes' answers -------------------------------------------------------------------


def test_the_ros_shape_answers_with_the_ros_command_whatever_the_simulator():
    argv = SimulatorBackend().tap_command(None, _execution("ros2"), run_dir="/out/c/0",
                                          selection=["/odom"])
    assert argv == ros_tap_command(["/odom"])


def test_the_stepped_shape_has_no_tap():
    assert SimulatorBackend().tap_command(None, _execution("base"), run_dir="/out/c/0",
                                          selection=["/odom"]) is None


def test_a_campaign_without_a_backend_gets_the_shapes_answer():
    assert tap_command(_execution("ros2", image="gz:1"), run_dir="/out", selection=[]) == \
        ["ros2", "topic", "list"]
    assert tap_command(_execution("base", image="x:1"), run_dir="/out", selection=[]) is None


def test_a_backend_may_decline_the_shapes_answer():
    assert tap_command(_execution("ros2", backend="silent"), run_dir="/out",
                       selection=["/odom"]) is None


def test_a_backend_may_name_its_own_command():
    assert tap_command(_execution("base", backend="own"), run_dir="/out/c/0",
                       selection=["pose"]) == ["mysim", "follow", "/out/c/0", "pose"]


def test_roqsim_has_no_tap_in_either_shape():
    """Its recording is the live view, and its CLI has no following command."""
    pytest.importorskip("robovast_sim_roqsim")
    from robovast_sim_roqsim.backend import RoqsimBackend
    backend = RoqsimBackend()
    for mode in ("base", "ros2"):
        assert backend.tap_command(None, _execution(mode), run_dir="/out",
                                   selection=["/odom"]) is None
    assert "chunk-flushed" in backend.tap_command.__doc__
