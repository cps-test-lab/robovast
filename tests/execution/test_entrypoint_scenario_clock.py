# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The entrypoint starts the scenario runner on the clock ``SCENARIO_USE_SIM_TIME`` names.

The runner block is cut out of the *shipped* script and run through a real bash with
``run_scenario`` replaced by a stub that prints its argv: what matters is the command line
the runner receives in each shape, and no assertion on the script's text shows that.
"""

import os
import subprocess
import textwrap

import pytest

from robovast.common.execution import render_entrypoint

ROS_RUNNER = ["ros2", "run", "scenario_execution_ros", "scenario_execution_ros"]
SIM_TIME = ["--ros-args", "-p", "use_sim_time:=true"]


def _runner_block(cluster: bool) -> str:
    rendered = render_entrypoint(cluster=cluster)
    start = rendered.index('    SCENARIO_FILE="${SCENARIO_FILE:-scenario.osc}"')
    end = rendered.index("\nfi", rendered.index("run_scenario ${RUNNER_CMD}", start))
    return textwrap.dedent(rendered[start:end])


def _run(env: dict, cluster: bool = True, ros2_on_path: bool = True, tmp_path=None):
    path = os.environ["PATH"]
    if ros2_on_path:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "ros2").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bindir / "ros2").chmod(0o755)
        path = f"{bindir}:{path}"
    script = "\n".join([
        "set -e", 'log() { echo "LOG $*"; }',
        'run_scenario() { printf "ARG %s\\n" "$@"; }',
        _runner_block(cluster)])
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False,
        env={"PATH": path, "SCENARIO_OUTPUT_DIR": "/out",
             "SCENARIO_PARAMETER_FILE": "/nonexistent/scenario.config",
             "SCENARIO_EXECUTION_PARAMETERS": "-t", "BT_LOG": "false", **env})


def _argv(out) -> list:
    assert out.returncode == 0, out.stdout + out.stderr
    return [line[4:] for line in out.stdout.splitlines() if line.startswith("ARG ")]


@pytest.mark.parametrize("cluster", [False, True])
def test_the_ros_shape_on_sim_time_passes_use_sim_time_last(tmp_path, cluster):
    """Last, because --ros-args takes every argument after it."""
    argv = _argv(_run({"SCENARIO_MODE": "ros2", "SCENARIO_USE_SIM_TIME": "true"},
                      cluster=cluster, tmp_path=tmp_path))
    assert argv[:4] == ROS_RUNNER
    assert argv[-4:] == ["-t"] + SIM_TIME


@pytest.mark.parametrize("env", [{"SCENARIO_MODE": "ros2", "SCENARIO_USE_SIM_TIME": "false"},
                                 {"SCENARIO_MODE": "ros2"}])
def test_the_ros_shape_on_wall_time_passes_nothing(tmp_path, env):
    argv = _argv(_run(env, tmp_path=tmp_path))
    assert argv[:4] == ROS_RUNNER
    assert "--ros-args" not in argv
    assert argv[-1] == "-t"


def test_the_stepped_shape_runs_the_base_runner_without_ros_time(tmp_path):
    argv = _argv(_run({"SCENARIO_MODE": "base", "SCENARIO_USE_SIM_TIME": "false",
                       "SIMULATION": "pkg.mod:Sim"}, tmp_path=tmp_path))
    assert argv[:4] == ["ros2", "run", "scenario_execution", "scenario_execution"]
    assert ["--simulation", "pkg.mod:Sim"] == argv[argv.index("--simulation"):][:2]
    assert "--ros-args" not in argv


@pytest.mark.parametrize("mode,ros2_on_path", [("base", True), ("auto", False)])
def test_sim_time_on_a_runner_without_ros_time_fails_the_run(tmp_path, mode, ros2_on_path):
    out = _run({"SCENARIO_MODE": mode, "SCENARIO_USE_SIM_TIME": "true"},
               ros2_on_path=ros2_on_path, tmp_path=tmp_path)
    assert out.returncode == 1
    assert "SCENARIO_USE_SIM_TIME=true needs the ROS runner" in out.stdout
    assert "ARG " not in out.stdout


def test_an_unreadable_value_fails_the_run(tmp_path):
    out = _run({"SCENARIO_MODE": "ros2", "SCENARIO_USE_SIM_TIME": "yes"}, tmp_path=tmp_path)
    assert out.returncode == 1
    assert "SCENARIO_USE_SIM_TIME must be true or false, not 'yes'" in out.stdout
